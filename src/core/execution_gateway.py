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
5. State is durable — records persisted to data/executions/ (JSON mock) / Cosmos.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.config import settings
from src.core.models import (
    ExecutionRecord,
    ExecutionStatus,
    GovernanceVerdict,
    SRIVerdict,
)

logger = logging.getLogger(__name__)

_DEFAULT_EXECUTIONS_DIR = (
    Path(__file__).parent.parent.parent / "data" / "executions"
)


class ExecutionGateway:
    """Process governance verdicts and route to IaC-safe execution paths.

    Records are persisted to ``data/executions/`` in mock mode (one JSON file
    per record) so they survive API restarts.

    Usage::

        gateway = ExecutionGateway()
        record = await gateway.process_verdict(verdict, resource_tags)
        print(record.status.value)  # "pr_created", "blocked", "awaiting_review", etc.
    """

    def __init__(self, executions_dir: Path | None = None) -> None:
        self._dir = executions_dir or _DEFAULT_EXECUTIONS_DIR
        # In-memory index for fast lookup; populated from disk on first use.
        self._records: dict[str, ExecutionRecord] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_verdict(
        self,
        verdict: GovernanceVerdict,
        resource_tags: dict[str, str] | None = None,
    ) -> ExecutionRecord:
        """Route a governance verdict to the correct execution path.

        Args:
            verdict: The governance verdict from the pipeline.
            resource_tags: Azure resource tags for IaC detection.
                           Pass the real tags from seed_resources.json /
                           Azure Resource Graph.  Empty dict → APPROVED
                           verdicts become manual_required.

        Returns:
            ExecutionRecord with the initial execution status.
        """
        self._ensure_loaded()
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
            verdict_snapshot=verdict.model_dump(mode="json"),
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
                record.status = ExecutionStatus.pending
                logger.info(
                    "ExecutionGateway: APPROVED but gateway disabled — "
                    "informational only for '%s'",
                    resource_id,
                )
            elif iac_managed and iac_repo:
                try:
                    record = await self._create_terraform_pr(record, verdict)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "ExecutionGateway: PR creation raised unexpectedly — %s", exc
                    )
                    record.status = ExecutionStatus.failed
                    record.notes = f"PR creation error: {exc}"
            else:
                record.status = ExecutionStatus.manual_required
                logger.info(
                    "ExecutionGateway: APPROVED but not IaC-managed — "
                    "manual execution required for '%s'",
                    resource_id,
                )

        record.updated_at = datetime.now(timezone.utc)
        self._save(record)
        return record

    async def approve_execution(
        self, execution_id: str, reviewed_by: str
    ) -> ExecutionRecord:
        """Human approves an ESCALATED verdict for execution.

        Rebuilds the original GovernanceVerdict from the stored snapshot
        and calls TerraformPRGenerator if the resource is IaC-managed.

        Args:
            execution_id: UUID of the ExecutionRecord to approve.
            reviewed_by: Name/email of the human approver.

        Returns:
            Updated ExecutionRecord.

        Raises:
            KeyError: If execution_id is unknown.
            ValueError: If record is not in awaiting_review state.
        """
        self._ensure_loaded()
        record = self._records.get(execution_id)
        if not record:
            raise KeyError(f"Execution record not found: {execution_id!r}")
        if record.status != ExecutionStatus.awaiting_review:
            raise ValueError(
                f"Cannot approve execution {execution_id!r}: "
                f"status is '{record.status.value}' (must be 'awaiting_review')"
            )

        record.reviewed_by = reviewed_by
        record.updated_at = datetime.now(timezone.utc)

        if record.iac_managed and record.iac_repo:
            # Reconstruct the GovernanceVerdict from the stored snapshot so we
            # can pass it to TerraformPRGenerator for real PR creation.
            verdict = self._reconstruct_verdict(record)
            if verdict is not None:
                try:
                    record = await self._create_terraform_pr(record, verdict)
                    logger.info(
                        "ExecutionGateway: %s approved by '%s' — PR created",
                        execution_id[:8], reviewed_by,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "ExecutionGateway: approve PR creation failed — %s", exc
                    )
                    record.status = ExecutionStatus.failed
                    record.notes = f"PR creation error on approve: {exc}"
            else:
                # Snapshot missing / corrupted — fall back to manual
                record.status = ExecutionStatus.manual_required
                record.notes = (
                    "Verdict snapshot not available — manual execution required"
                )
                logger.warning(
                    "ExecutionGateway: %s approved by '%s' — no verdict snapshot, "
                    "manual_required",
                    execution_id[:8], reviewed_by,
                )
        else:
            record.status = ExecutionStatus.manual_required
            logger.info(
                "ExecutionGateway: %s approved by '%s' — manual execution required",
                execution_id[:8], reviewed_by,
            )

        self._save(record)
        return record

    async def dismiss_execution(
        self, execution_id: str, reviewed_by: str, reason: str = ""
    ) -> ExecutionRecord:
        """Human dismisses a verdict — no execution will happen.

        Args:
            execution_id: UUID of the ExecutionRecord to dismiss.
            reviewed_by: Name/email of the person dismissing.
            reason: Optional reason for dismissal.

        Returns:
            Updated ExecutionRecord with status 'dismissed'.

        Raises:
            KeyError: If execution_id is unknown.
        """
        self._ensure_loaded()
        record = self._records.get(execution_id)
        if not record:
            raise KeyError(f"Execution record not found: {execution_id!r}")

        record.status = ExecutionStatus.dismissed
        record.reviewed_by = reviewed_by
        record.notes = reason
        record.updated_at = datetime.now(timezone.utc)

        logger.info(
            "ExecutionGateway: %s dismissed by '%s' — reason: %s",
            execution_id[:8], reviewed_by, reason or "(none)",
        )
        self._save(record)
        return record

    def get_record(self, execution_id: str) -> ExecutionRecord | None:
        """Return one ExecutionRecord by ID, or None if not found."""
        self._ensure_loaded()
        return self._records.get(execution_id)

    def get_records_for_verdict(self, action_id: str) -> list[ExecutionRecord]:
        """Return all ExecutionRecords linked to a governance verdict's action_id."""
        self._ensure_loaded()
        return [r for r in self._records.values() if r.action_id == action_id]

    def get_pending_reviews(self) -> list[ExecutionRecord]:
        """Return all records awaiting human review (ESCALATED verdicts)."""
        self._ensure_loaded()
        return [
            r for r in self._records.values()
            if r.status == ExecutionStatus.awaiting_review
        ]

    def get_unresolved_proposals(self) -> list[tuple["ProposedAction", ExecutionRecord]]:
        """Return (ProposedAction, ExecutionRecord) pairs for issues that are
        APPROVED but have no automated resolution path yet.

        Only ``manual_required`` records are returned — these are APPROVED verdicts
        where no IaC path was detected, so a human must fix the Azure resource
        manually.  The scan loop uses this to re-flag the same issues on every
        subsequent scan until the human either:
          - Fixes the resource in Azure and clicks **Dismiss** in the dashboard, OR
          - The agent naturally stops proposing it (underlying config changed).

        Excluded statuses:
          - ``pr_created``      — a PR is already open; no need to re-propose
          - ``awaiting_review`` — HITL buttons are already in the dashboard
          - ``blocked``         — deliberately denied; do not re-surface
          - ``dismissed``       — human acknowledged / resolved
          - ``applied``         — fix confirmed applied
          - ``failed``          — gateway error; let next scan handle fresh
        """
        from src.core.models import ProposedAction  # noqa: PLC0415 (avoid circular at module level)

        self._ensure_loaded()
        results = []
        for record in self._records.values():
            if record.status != ExecutionStatus.manual_required:
                continue
            snapshot = record.verdict_snapshot
            if not snapshot:
                continue
            action_data = snapshot.get("proposed_action")
            if not action_data:
                continue
            try:
                action = ProposedAction.model_validate(action_data)
                results.append((action, record))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ExecutionGateway: could not reconstruct ProposedAction "
                    "from verdict_snapshot for record %s — %s",
                    record.execution_id[:8], exc,
                )
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _detect_iac_management(
        self, resource_tags: dict[str, str]
    ) -> tuple[bool, str, str, str]:
        """Check if a resource is IaC-managed via its Azure tags.

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
        """Delegate PR creation to TerraformPRGenerator."""
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

    def _reconstruct_verdict(self, record: ExecutionRecord) -> GovernanceVerdict | None:
        """Rebuild a GovernanceVerdict from the stored snapshot dict.

        Returns None if the snapshot is missing or cannot be parsed.
        """
        snapshot = record.verdict_snapshot
        if not snapshot:
            return None
        try:
            return GovernanceVerdict.model_validate(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ExecutionGateway: could not reconstruct verdict from snapshot — %s", exc
            )
            return None

    # ------------------------------------------------------------------
    # Persistence helpers (JSON file store)
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load all persisted records from disk on first access."""
        if self._loaded:
            return
        self._loaded = True
        if not self._dir.exists():
            return
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rec = ExecutionRecord.model_validate(data)
                self._records[rec.execution_id] = rec
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ExecutionGateway: could not load record %s — %s", path.name, exc
                )

    def _save(self, record: ExecutionRecord) -> None:
        """Persist one record to disk and update the in-memory index."""
        self._records[record.execution_id] = record
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{record.execution_id}.json"
            path.write_text(
                record.model_dump_json(indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ExecutionGateway: could not persist record %s — %s",
                record.execution_id[:8], exc,
            )
