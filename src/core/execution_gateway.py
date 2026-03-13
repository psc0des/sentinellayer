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

Phase 28: Agent fix preview and execution now delegate to ExecutionAgent
(src/core/execution_agent.py), which uses GPT-4.1 to reason about HOW to
implement any approved action dynamically — replacing the old hardcoded switch.
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
    ProposedAction,
    SRIVerdict,
)

logger = logging.getLogger(__name__)

_DEFAULT_EXECUTIONS_DIR = (
    Path(__file__).parent.parent.parent / "data" / "executions"
)


def _parse_arm_id(resource_id: str) -> dict[str, str]:
    """Extract resource_group, name, provider, and resource_type from an ARM ID.

    ARM IDs look like:
        /subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{type}/{name}

    For short names (no slashes), returns the name as-is with empty fields.
    """
    parts = resource_id.strip("/").split("/")
    result: dict[str, str] = {
        "resource_group": "",
        "name": "",
        "provider": "",
        "resource_type": "",
        "full_id": resource_id,
    }
    if len(parts) >= 8:
        # Standard ARM path: subscriptions/X/resourceGroups/Y/providers/A/B/C
        result["resource_group"] = parts[3]
        result["provider"] = f"{parts[5]}/{parts[6]}"
        result["name"] = parts[7]
        result["resource_type"] = parts[6]
    elif len(parts) >= 2:
        result["name"] = parts[-1]
        result["resource_type"] = parts[-2] if len(parts) >= 2 else ""
    else:
        result["name"] = resource_id
    return result



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
            else:
                # Always route to manual_required so the human chooses how to
                # execute: Create Terraform PR, Fix using Agent, or Fix in Portal.
                # Never auto-create a PR — that decision belongs to the user.
                # (iac_managed / iac_repo are still stored on the record so the
                #  "Create Terraform PR" button knows a PR is possible.)
                action_key = verdict.proposed_action.action_type.value
                existing = next(
                    (
                        r for r in self._records.values()
                        if r.status == ExecutionStatus.manual_required
                        and r.verdict_snapshot
                        and r.verdict_snapshot.get("proposed_action", {})
                            .get("target", {}).get("resource_id") == resource_id
                        and r.verdict_snapshot.get("proposed_action", {})
                            .get("action_type") == action_key
                    ),
                    None,
                )
                if existing:
                    # Update action_id to the latest verdict so the drilldown
                    # can find this record when looking up by the new action_id.
                    # (Each scan creates a fresh action_id UUID even for the
                    # same resource+action_type pair.)
                    existing.action_id = verdict.action_id
                    existing.updated_at = now
                    self._save(existing)
                    logger.info(
                        "ExecutionGateway: APPROVED — reusing existing "
                        "manual_required record %s for '%s' (action_id updated)",
                        existing.execution_id[:8], resource_id,
                    )
                    return existing
                record.status = ExecutionStatus.manual_required
                logger.info(
                    "ExecutionGateway: APPROVED — manual_required for '%s' "
                    "(iac_managed=%s, user will choose execution path)",
                    resource_id, iac_managed,
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

    def list_all(self) -> list[ExecutionRecord]:
        """Return all ExecutionRecords, newest first."""
        self._ensure_loaded()
        return sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)

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
        # Deduplicate: for the same (resource_id, action_type) keep only the
        # oldest manual_required record.  Re-flag scans used to create a new
        # record each time, so we may have many duplicates in data/executions/.
        # Using the oldest ensures the "Unresolved since <date>" shows the real
        # first-seen date, not the date of a re-flag pass.
        seen: dict[tuple[str, str], tuple["ProposedAction", ExecutionRecord]] = {}
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
                key = (action.target.resource_id, action.action_type.value)
                if key not in seen or record.created_at < seen[key][1].created_at:
                    seen[key] = (action, record)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ExecutionGateway: could not reconstruct ProposedAction "
                    "from verdict_snapshot for record %s — %s",
                    record.execution_id[:8], exc,
                )
        return list(seen.values())

    # ------------------------------------------------------------------
    # HITL Agent Fix + PR creation from manual_required
    # ------------------------------------------------------------------

    async def create_pr_from_manual(
        self, execution_id: str, reviewed_by: str
    ) -> ExecutionRecord:
        """Create a Terraform PR from a manual_required execution record.

        Re-uses the existing ``_create_terraform_pr`` path so there is no
        code duplication.  If GitHub is not configured, the status stays
        ``manual_required`` with an explanatory note (same as approve flow).

        Raises:
            KeyError: If execution_id is unknown.
            ValueError: If the record is not in ``manual_required`` state,
                        or the verdict snapshot is missing.
        """
        self._ensure_loaded()
        record = self._records.get(execution_id)
        if not record:
            raise KeyError(f"Execution record not found: {execution_id!r}")
        _pr_allowed = {ExecutionStatus.manual_required, ExecutionStatus.awaiting_review}
        if record.status not in _pr_allowed:
            raise ValueError(
                f"Cannot create PR for {execution_id!r}: "
                f"status is '{record.status.value}' "
                f"(must be 'manual_required' or 'awaiting_review')"
            )
        # Auto-approve escalated records when user picks a remediation action
        if record.status == ExecutionStatus.awaiting_review:
            record.status = ExecutionStatus.manual_required
            record.updated_at = datetime.now(timezone.utc)
            self._save(record)

        verdict = self._reconstruct_verdict(record)
        if verdict is None:
            raise ValueError(
                f"Cannot create PR for {execution_id!r}: "
                "verdict snapshot is missing or corrupted"
            )

        record.reviewed_by = reviewed_by
        record.updated_at = datetime.now(timezone.utc)

        try:
            record = await self._create_terraform_pr(record, verdict)
            logger.info(
                "ExecutionGateway: %s — PR created from manual_required by '%s'",
                execution_id[:8], reviewed_by,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "ExecutionGateway: create_pr_from_manual failed — %s", exc
            )
            record.status = ExecutionStatus.failed
            record.notes = f"PR creation error: {exc}"

        self._save(record)
        return record

    async def generate_agent_fix_plan(self, execution_id: str) -> dict:
        """Generate an LLM-driven execution plan for this issue.

        In mock mode (USE_LOCAL_MOCKS=true or no OpenAI endpoint): returns a
        deterministic plan using the same structure.
        In live mode: GPT-4.1 inspects the resource and generates a plan.

        The plan is stored on the ExecutionRecord so execute_agent_fix() can
        read it without regenerating.

        Returns:
            Dict with ``steps``, ``summary``, ``estimated_impact``,
            ``rollback_hint``, ``commands`` (backward compat), ``execution_id``,
            ``action_type``, ``resource_id``, and ``warning``.

        Raises:
            KeyError: If execution_id is unknown.
            ValueError: If the verdict snapshot is missing.
        """
        self._ensure_loaded()
        record = self._records.get(execution_id)
        if not record:
            raise KeyError(f"Execution record not found: {execution_id!r}")

        snapshot = record.verdict_snapshot
        if not snapshot or "proposed_action" not in snapshot:
            raise ValueError(
                f"Cannot generate plan for {execution_id!r}: "
                "verdict snapshot is missing"
            )

        action = ProposedAction.model_validate(snapshot["proposed_action"])

        from src.core.execution_agent import ExecutionAgent  # noqa: PLC0415
        agent = ExecutionAgent(cfg=settings)
        plan = await agent.plan(action, snapshot)

        # Store the plan on the record so execute_agent_fix() can read it
        record.execution_plan = plan
        record.updated_at = datetime.now(timezone.utc)
        self._save(record)

        # Augment with execution metadata for the API response
        plan["execution_id"] = execution_id
        plan["action_type"] = action.action_type.value
        plan["resource_id"] = action.target.resource_id
        plan["warning"] = (
            "These operations will modify your Azure environment. "
            "Review carefully before executing."
        )
        return plan

    async def execute_agent_fix(
        self, execution_id: str, reviewed_by: str
    ) -> ExecutionRecord:
        """Execute the pre-approved agent fix plan using the ExecutionAgent.

        Phase 28: Delegates to ExecutionAgent which uses GPT-4.1 to execute
        the approved plan step by step.  The plan must have been generated
        (and stored) by generate_agent_fix_plan() first.

        In mock mode (``settings.use_local_mocks`` or no OpenAI endpoint),
        simulates success for all steps.

        Raises:
            KeyError: If execution_id is unknown.
            ValueError: If the record is not in an executable state, or the
                        verdict snapshot / execution plan is missing.
        """
        self._ensure_loaded()
        record = self._records.get(execution_id)
        if not record:
            raise KeyError(f"Execution record not found: {execution_id!r}")
        _agent_fix_allowed = {
            ExecutionStatus.manual_required,
            ExecutionStatus.pr_created,
            ExecutionStatus.awaiting_review,
        }
        if record.status not in _agent_fix_allowed:
            raise ValueError(
                f"Cannot execute fix for {execution_id!r}: "
                f"status is '{record.status.value}' "
                f"(must be 'manual_required', 'pr_created', or 'awaiting_review')"
            )
        # Auto-approve escalated records when user picks a remediation action
        if record.status == ExecutionStatus.awaiting_review:
            record.status = ExecutionStatus.manual_required
            record.updated_at = datetime.now(timezone.utc)
            self._save(record)

        snapshot = record.verdict_snapshot
        if not snapshot or "proposed_action" not in snapshot:
            raise ValueError(
                f"Cannot execute fix for {execution_id!r}: verdict snapshot is missing"
            )

        action = ProposedAction.model_validate(snapshot["proposed_action"])

        # Use the stored plan if available; otherwise generate on the fly
        plan = record.execution_plan
        if not plan:
            logger.info(
                "ExecutionGateway: %s — no stored plan, generating now",
                execution_id[:8],
            )
            plan = await self.generate_agent_fix_plan(execution_id)
            # Re-fetch record — generate_agent_fix_plan saves updated record
            record = self._records[execution_id]

        record.reviewed_by = reviewed_by
        record.updated_at = datetime.now(timezone.utc)

        from src.core.execution_agent import ExecutionAgent  # noqa: PLC0415
        agent = ExecutionAgent(cfg=settings)
        result = await agent.execute(plan, action)

        if result["success"]:
            record.status = ExecutionStatus.applied
            record.notes = f"Agent fix applied via ExecutionAgent.\n{result['summary']}"
            logger.info(
                "ExecutionGateway: %s — agent fix applied by '%s': %s",
                execution_id[:8], reviewed_by, result["summary"],
            )
            # Post-execution verification — confirm fix took effect (non-fatal)
            try:
                verification = await agent.verify(action, result)
                record.verification = verification
                if not verification.get("confirmed"):
                    record.notes += f"\nVerification: {verification.get('message', 'not confirmed')}"
                logger.info(
                    "ExecutionGateway: %s — verification: confirmed=%s — %s",
                    execution_id[:8], verification.get("confirmed"), verification.get("message"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ExecutionGateway: %s — verification failed (non-fatal): %s",
                    execution_id[:8], exc,
                )
        else:
            record.status = ExecutionStatus.failed
            record.notes = f"Agent fix failed.\n{result['summary']}"
            logger.error(
                "ExecutionGateway: %s — agent fix failed: %s",
                execution_id[:8], result["summary"],
            )

        record.execution_log = result.get("steps_completed", [])
        self._save(record)
        return record

    async def rollback_agent_fix(
        self, execution_id: str, reviewed_by: str
    ) -> ExecutionRecord:
        """Reverse a previously applied agent fix.

        Only valid when record.status == 'applied'.
        Uses the stored execution_plan.rollback_hint to determine what to undo.

        Raises:
            KeyError:  If execution_id is unknown.
            ValueError: If the record is not in 'applied' state or has no plan.
        """
        self._ensure_loaded()
        record = self._records.get(execution_id)
        if not record:
            raise KeyError(f"Execution record not found: {execution_id!r}")
        if record.status != ExecutionStatus.applied:
            raise ValueError(
                f"Cannot rollback {execution_id!r}: status is '{record.status.value}' "
                f"(must be 'applied')"
            )
        plan = record.execution_plan
        if not plan:
            raise ValueError(
                f"Cannot rollback {execution_id!r}: no execution_plan stored on record"
            )

        snapshot = record.verdict_snapshot
        if not snapshot or "proposed_action" not in snapshot:
            raise ValueError(
                f"Cannot rollback {execution_id!r}: verdict snapshot is missing"
            )
        action = ProposedAction.model_validate(snapshot["proposed_action"])

        from src.core.execution_agent import ExecutionAgent  # noqa: PLC0415
        agent = ExecutionAgent(cfg=settings)
        result = await agent.rollback(action, plan)

        record.reviewed_by = reviewed_by
        record.updated_at = datetime.now(timezone.utc)
        record.rollback_log = result.get("steps_completed", [])

        if result["success"]:
            record.status = ExecutionStatus.rolled_back
            record.notes += f"\nRolled back: {result['summary']}"
            logger.info(
                "ExecutionGateway: %s — rollback completed by '%s': %s",
                execution_id[:8], reviewed_by, result["summary"],
            )
        else:
            # Keep status as 'applied' — the fix is still in place; the rollback failed.
            # The rollback_log on the record carries the failure detail for the UI.
            record.notes += f"\nRollback failed: {result['summary']}"
            logger.error(
                "ExecutionGateway: %s — rollback failed: %s",
                execution_id[:8], result["summary"],
            )

        self._save(record)
        return record

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
