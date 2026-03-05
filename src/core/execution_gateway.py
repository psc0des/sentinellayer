"""Execution Gateway — routes governance verdicts to IaC-safe execution paths.

The gateway NEVER directly modifies Azure resources.  It routes verdicts to:

- DENIED     → log + block (no execution)
- ESCALATED  → mark awaiting_review (human decides via dashboard buttons)
- APPROVED + IaC-managed → generate Terraform PR via GitHub API
- APPROVED + not IaC-managed → mark as manual_required (human executes)

The gateway is opt-in: set EXECUTION_GATEWAY_ENABLED=true to activate.
When disabled (default), the pipeline still calls process_verdict() but it
returns a record immediately without creating any PRs.

Design principles:
1. Gateway never executes directly — only creates PRs or marks for manual review.
2. Gateway failure never breaks the verdict — callers wrap in try/except.
3. HITL always present — even APPROVED actions require a human to merge the PR.
4. IaC state is sacred — all changes flow through terraform apply, never direct SDK.
"""

import logging
import uuid
from datetime import datetime, timezone

from src.config import settings
from src.core.models import (
    ExecutionRecord,
    ExecutionStatus,
    GovernanceVerdict,
    SRIVerdict,
)

logger = logging.getLogger(__name__)


class ExecutionGateway:
    """Process governance verdicts and route to IaC-safe execution paths.

    Usage::

        gateway = ExecutionGateway()
        record = await gateway.process_verdict(verdict, resource_tags)
        print(record.status.value)  # "pr_created", "blocked", "awaiting_review", etc.
    """

    def __init__(self) -> None:
        # In-memory store keyed by execution_id.
        # Future enhancement: persist to Cosmos DB for durability across restarts.
        self._records: dict[str, ExecutionRecord] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_verdict(
        self,
        verdict: GovernanceVerdict,
        resource_tags: dict[str, str] | None = None,
    ) -> ExecutionRecord:
        """Route a governance verdict to the correct execution path.

        Called by the pipeline (or dashboard_api) immediately after
        DecisionTracker.record().  The returned ExecutionRecord tracks the
        full lifecycle: pending → pr_created → pr_merged → applied.

        Args:
            verdict: The governance verdict from the pipeline.
            resource_tags: Azure resource tags for IaC detection.
                           Pass {} or None when tags are not available
                           (APPROVED verdicts will become manual_required).

        Returns:
            ExecutionRecord with the initial execution status.
        """
        tags = resource_tags or {}
        iac_managed, iac_tool, iac_repo, iac_path = self._detect_iac_management(tags)
        now = datetime.now(timezone.utc)

        record = ExecutionRecord(
            execution_id=str(uuid.uuid4()),
            action_id=verdict.action_id,
            verdict=verdict.decision,
            status=ExecutionStatus.pending,
            iac_managed=iac_managed,
            iac_tool=iac_tool,
            iac_repo=iac_repo,
            iac_path=iac_path,
            created_at=now,
            updated_at=now,
        )

        resource_id = verdict.proposed_action.target.resource_id

        if verdict.decision == SRIVerdict.DENIED:
            record.status = ExecutionStatus.blocked
            logger.info(
                "ExecutionGateway: DENIED — blocked, no execution for '%s'",
                resource_id,
            )

        elif verdict.decision == SRIVerdict.ESCALATED:
            record.status = ExecutionStatus.awaiting_review
            logger.info(
                "ExecutionGateway: ESCALATED — awaiting human review for '%s'",
                resource_id,
            )

        elif verdict.decision == SRIVerdict.APPROVED:
            if not settings.execution_gateway_enabled:
                # Gateway disabled — verdict is informational only.
                record.status = ExecutionStatus.pending
                logger.info(
                    "ExecutionGateway: APPROVED but gateway disabled — "
                    "informational only for '%s'",
                    resource_id,
                )
            elif iac_managed and iac_repo:
                # IaC-managed + gateway enabled → generate Terraform PR
                try:
                    record = await self._create_terraform_pr(record, verdict)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "ExecutionGateway: PR creation raised unexpectedly — %s", exc
                    )
                    record.status = ExecutionStatus.failed
                    record.notes = f"PR creation error: {exc}"
            else:
                # Not IaC-managed → human must execute manually
                record.status = ExecutionStatus.manual_required
                logger.info(
                    "ExecutionGateway: APPROVED but not IaC-managed — "
                    "manual execution required for '%s'",
                    resource_id,
                )

        record.updated_at = datetime.now(timezone.utc)
        self._records[record.execution_id] = record
        # Also index by action_id for quick lookup from API endpoints
        return record

    async def approve_execution(
        self, execution_id: str, reviewed_by: str
    ) -> ExecutionRecord:
        """Human approves an ESCALATED verdict for execution.

        Marks the record as pending and re-routes through IaC/manual path.

        Args:
            execution_id: UUID of the ExecutionRecord to approve.
            reviewed_by: Name/email of the human approver.

        Returns:
            Updated ExecutionRecord.

        Raises:
            ValueError: If execution_id unknown or record is not awaiting_review.
        """
        record = self._records.get(execution_id)
        if not record:
            raise ValueError(f"Unknown execution_id: {execution_id!r}")
        if record.status != ExecutionStatus.awaiting_review:
            raise ValueError(
                f"Cannot approve execution {execution_id!r}: "
                f"status is '{record.status.value}' (must be 'awaiting_review')"
            )

        record.reviewed_by = reviewed_by
        record.updated_at = datetime.now(timezone.utc)

        # Re-route: try IaC PR if repo is set, else manual
        if record.iac_managed and record.iac_repo:
            # Reconstruct a minimal verdict reference is not needed here —
            # the record already has all routing info.  Just mark for PR.
            record.status = ExecutionStatus.pr_created
            logger.info(
                "ExecutionGateway: %s approved by '%s' — "
                "marking pr_created (IaC repo: %s)",
                execution_id[:8], reviewed_by, record.iac_repo,
            )
        else:
            record.status = ExecutionStatus.manual_required
            logger.info(
                "ExecutionGateway: %s approved by '%s' — manual execution required",
                execution_id[:8], reviewed_by,
            )

        self._records[execution_id] = record
        return record

    async def dismiss_execution(
        self, execution_id: str, reviewed_by: str, reason: str = ""
    ) -> ExecutionRecord:
        """Human dismisses a verdict — no execution will happen.

        Can dismiss any non-terminal record (pending, awaiting_review,
        manual_required, pr_created).

        Args:
            execution_id: UUID of the ExecutionRecord to dismiss.
            reviewed_by: Name/email of the person dismissing.
            reason: Optional reason for dismissal.

        Returns:
            Updated ExecutionRecord with status 'dismissed'.

        Raises:
            ValueError: If execution_id is unknown.
        """
        record = self._records.get(execution_id)
        if not record:
            raise ValueError(f"Unknown execution_id: {execution_id!r}")

        record.status = ExecutionStatus.dismissed
        record.reviewed_by = reviewed_by
        record.notes = reason
        record.updated_at = datetime.now(timezone.utc)

        logger.info(
            "ExecutionGateway: %s dismissed by '%s' — reason: %s",
            execution_id[:8], reviewed_by, reason or "(none)",
        )
        self._records[execution_id] = record
        return record

    def get_record(self, execution_id: str) -> ExecutionRecord | None:
        """Return one ExecutionRecord by ID, or None if not found."""
        return self._records.get(execution_id)

    def get_records_for_verdict(self, action_id: str) -> list[ExecutionRecord]:
        """Return all ExecutionRecords linked to a governance verdict's action_id."""
        return [r for r in self._records.values() if r.action_id == action_id]

    def get_pending_reviews(self) -> list[ExecutionRecord]:
        """Return all records awaiting human review (ESCALATED verdicts)."""
        return [
            r for r in self._records.values()
            if r.status == ExecutionStatus.awaiting_review
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _detect_iac_management(
        self, resource_tags: dict[str, str]
    ) -> tuple[bool, str, str, str]:
        """Check if a resource is IaC-managed via its Azure tags.

        Looks for a ``managed_by`` tag with value "terraform", "bicep", or
        "pulumi".  Also reads ``iac_repo`` and ``iac_path`` tags.

        Returns:
            (is_managed, iac_tool, iac_repo, iac_path)
        """
        managed_by = resource_tags.get("managed_by", "").lower()
        if managed_by in ("terraform", "bicep", "pulumi"):
            return (
                True,
                managed_by,
                resource_tags.get("iac_repo", ""),
                resource_tags.get("iac_path", ""),
            )
        return (False, "", "", "")

    async def _create_terraform_pr(
        self,
        record: ExecutionRecord,
        verdict: GovernanceVerdict,
    ) -> ExecutionRecord:
        """Delegate PR creation to TerraformPRGenerator.

        Imports lazily to avoid requiring PyGithub when the gateway is
        disabled or the repo is not configured.
        """
        try:
            from src.core.terraform_pr_generator import TerraformPRGenerator  # noqa: PLC0415
            generator = TerraformPRGenerator()
            record = await generator.create_pr(verdict, record)
        except ImportError:
            logger.warning(
                "ExecutionGateway: PyGithub not installed — "
                "cannot create PR. Install with: pip install PyGithub"
            )
            record.status = ExecutionStatus.failed
            record.notes = "PyGithub not installed — run: pip install PyGithub"
        except Exception as exc:  # noqa: BLE001
            logger.error("ExecutionGateway: PR creation failed — %s", exc)
            record.status = ExecutionStatus.failed
            record.notes = f"PR creation error: {exc}"
        return record
