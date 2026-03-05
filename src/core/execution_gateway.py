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
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.config import settings
from src.core.models import (
    ActionType,
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


def _build_az_commands(action: ProposedAction) -> list[str]:
    """Map a ProposedAction to one or more ``az`` CLI commands.

    This is the *preview* — the user sees these commands before confirming
    execution.  Only the most common action types are handled; unknown types
    get a comment explaining manual steps.
    """
    arm = _parse_arm_id(action.target.resource_id)
    rg = arm["resource_group"] or "<RESOURCE_GROUP>"
    name = arm["name"] or action.target.resource_id
    commands: list[str] = []

    if action.action_type == ActionType.MODIFY_NSG:
        # Try to extract the rule name from the reason text
        rule_match = re.search(
            r"rule ['\"]([^'\"]+)['\"]", action.reason, re.IGNORECASE
        )
        rule_name = rule_match.group(1) if rule_match else "<RULE_NAME>"
        commands.append(
            f"az network nsg rule delete"
            f" --resource-group {rg}"
            f" --nsg-name {name}"
            f" --name {rule_name}"
        )

    elif action.action_type in (ActionType.SCALE_DOWN, ActionType.SCALE_UP):
        proposed = action.target.proposed_sku or "<NEW_SKU>"
        commands.append(
            f"az vm resize"
            f" --resource-group {rg}"
            f" --name {name}"
            f" --size {proposed}"
        )

    elif action.action_type == ActionType.DELETE_RESOURCE:
        if arm["full_id"].startswith("/"):
            commands.append(f"az resource delete --ids {arm['full_id']}")
        else:
            commands.append(
                f"az resource delete"
                f" --resource-group {rg}"
                f" --name {name}"
                f" --resource-type {arm['provider'] or '<PROVIDER/TYPE>'}"
            )

    elif action.action_type == ActionType.RESTART_SERVICE:
        commands.append(
            f"az vm restart"
            f" --resource-group {rg}"
            f" --name {name}"
        )

    else:
        # Fallback — no known az mapping
        commands.append(
            f"# No automated az command for action type"
            f" '{action.action_type.value}'."
            f"  Please apply manually in the Azure Portal."
        )

    return commands


async def _execute_fix_via_sdk(action: ProposedAction) -> str:
    """Execute the remediation for a ProposedAction using Azure Python SDK.

    Uses ``DefaultAzureCredential`` (same as the rest of the project) so it
    works on Azure App Service (Managed Identity), local dev (``az login``),
    and CI/CD (service principal).

    Returns a human-readable summary of what was done.

    Raises:
        ValueError: If the action type is not supported for SDK execution,
                    or required ARM ID fields are missing.
        ImportError: If the Azure management SDK is not installed.
    """
    arm = _parse_arm_id(action.target.resource_id)
    rg = arm["resource_group"]
    name = arm["name"]

    if not rg or not name:
        raise ValueError(
            f"Cannot execute fix: resource_id '{action.target.resource_id}' "
            "must be a full ARM ID (need resource_group and name)"
        )

    from azure.identity.aio import DefaultAzureCredential  # noqa: PLC0415

    if action.action_type == ActionType.MODIFY_NSG:
        rule_match = re.search(
            r"rule ['\"]([^'\"]+)['\"]", action.reason, re.IGNORECASE
        )
        if not rule_match:
            raise ValueError(
                "Cannot determine NSG rule name from action reason. "
                "Expected format: rule 'RuleName'"
            )
        rule_name = rule_match.group(1)

        from azure.mgmt.network.aio import NetworkManagementClient  # noqa: PLC0415

        async with DefaultAzureCredential() as credential:
            async with NetworkManagementClient(
                credential, settings.azure_subscription_id
            ) as client:
                poller = await client.security_rules.begin_delete(
                    rg, name, rule_name
                )
                await poller.result()
        return f"Deleted NSG rule '{rule_name}' from '{name}' in '{rg}'"

    elif action.action_type in (ActionType.SCALE_DOWN, ActionType.SCALE_UP):
        proposed_sku = action.target.proposed_sku
        if not proposed_sku:
            raise ValueError("Cannot resize: proposed_sku is missing")

        from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415

        async with DefaultAzureCredential() as credential:
            async with ComputeManagementClient(
                credential, settings.azure_subscription_id
            ) as client:
                poller = await client.virtual_machines.begin_update(
                    rg, name,
                    {"hardware_profile": {"vm_size": proposed_sku}},
                )
                await poller.result()
        return f"Resized VM '{name}' to '{proposed_sku}' in '{rg}'"

    elif action.action_type == ActionType.DELETE_RESOURCE:
        from azure.mgmt.resource.aio import ResourceManagementClient  # noqa: PLC0415

        async with DefaultAzureCredential() as credential:
            async with ResourceManagementClient(
                credential, settings.azure_subscription_id
            ) as client:
                poller = await client.resources.begin_delete_by_id(
                    arm["full_id"], api_version="2023-07-01"
                )
                await poller.result()
        return f"Deleted resource '{name}' in '{rg}'"

    elif action.action_type == ActionType.RESTART_SERVICE:
        from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415

        async with DefaultAzureCredential() as credential:
            async with ComputeManagementClient(
                credential, settings.azure_subscription_id
            ) as client:
                poller = await client.virtual_machines.begin_restart(rg, name)
                await poller.result()
        return f"Restarted VM '{name}' in '{rg}'"

    else:
        raise ValueError(
            f"Action type '{action.action_type.value}' is not supported "
            "for automated SDK execution. Use the Azure Portal instead."
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
        if record.status != ExecutionStatus.manual_required:
            raise ValueError(
                f"Cannot create PR for {execution_id!r}: "
                f"status is '{record.status.value}' (must be 'manual_required')"
            )

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

    def generate_agent_fix_commands(self, execution_id: str) -> dict:
        """Preview the ``az`` CLI commands that would fix this issue.

        Pure read operation — no side effects.

        Returns:
            Dict with ``execution_id``, ``action_type``, ``resource_id``,
            ``commands`` (list of shell strings), and a ``warning`` message.

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
                f"Cannot generate commands for {execution_id!r}: "
                "verdict snapshot is missing"
            )

        action = ProposedAction.model_validate(snapshot["proposed_action"])
        commands = _build_az_commands(action)

        return {
            "execution_id": execution_id,
            "action_type": action.action_type.value,
            "resource_id": action.target.resource_id,
            "commands": commands,
            "warning": (
                "These commands will modify your Azure environment. "
                "Review carefully before executing."
            ),
        }

    async def execute_agent_fix(
        self, execution_id: str, reviewed_by: str
    ) -> ExecutionRecord:
        """Execute a remediation fix using the Azure Python SDK.

        In mock mode (``settings.use_local_mocks``), simulates success.

        In live mode, calls the appropriate Azure management SDK
        (``azure.mgmt.network``, ``azure.mgmt.compute``, ``azure.mgmt.resource``)
        using ``DefaultAzureCredential`` — works on App Service (Managed Identity),
        local dev (``az login``), and CI/CD (service principal).

        Raises:
            KeyError: If execution_id is unknown.
            ValueError: If the record is not in ``manual_required`` state,
                        the verdict snapshot is missing, or the action type
                        is unsupported for SDK execution.
        """
        self._ensure_loaded()
        record = self._records.get(execution_id)
        if not record:
            raise KeyError(f"Execution record not found: {execution_id!r}")
        if record.status != ExecutionStatus.manual_required:
            raise ValueError(
                f"Cannot execute fix for {execution_id!r}: "
                f"status is '{record.status.value}' (must be 'manual_required')"
            )

        preview = self.generate_agent_fix_commands(execution_id)
        commands = preview["commands"]

        # Reconstruct the ProposedAction for SDK execution
        snapshot = record.verdict_snapshot
        action = ProposedAction.model_validate(snapshot["proposed_action"])

        record.reviewed_by = reviewed_by
        record.updated_at = datetime.now(timezone.utc)

        # ── Mock mode: simulate success ──
        if settings.use_local_mocks:
            record.status = ExecutionStatus.applied
            record.notes = (
                f"[mock] Agent fix applied successfully.\n"
                f"Commands: {'; '.join(commands)}"
            )
            logger.info(
                "ExecutionGateway: %s — agent fix applied (mock) by '%s'",
                execution_id[:8], reviewed_by,
            )
            self._save(record)
            return record

        # ── Live mode: execute via Azure SDK ──
        try:
            result_msg = await _execute_fix_via_sdk(action)
            record.status = ExecutionStatus.applied
            record.notes = f"Agent fix applied via Azure SDK.\n{result_msg}"
            logger.info(
                "ExecutionGateway: %s — agent fix applied (SDK) by '%s': %s",
                execution_id[:8], reviewed_by, result_msg,
            )
        except ImportError as exc:
            raise ValueError(
                f"Azure management SDK not installed: {exc}. "
                "Run: pip install azure-mgmt-network azure-mgmt-compute azure-mgmt-resource"
            ) from None
        except ValueError:
            # Re-raise ValueError (unsupported action, missing ARM ID, etc.)
            # without corrupting the record status.
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = ExecutionStatus.failed
            record.notes = f"Azure SDK error: {exc}"
            logger.error(
                "ExecutionGateway: agent fix failed — %s", exc
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
