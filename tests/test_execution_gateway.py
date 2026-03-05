"""Tests for the Phase 21 Execution Gateway.

Covers:
1. IaC detection via resource tags
2. Verdict routing: DENIED → blocked, ESCALATED → awaiting_review,
   APPROVED + IaC → pr_created, APPROVED + no IaC → manual_required
3. Approval and dismissal flows
4. Gateway failure does not break verdict (integration smoke test)
5. Edge cases: unknown execution_id, wrong-state approvals
6. TerraformPRGenerator without PyGithub (graceful degradation)
7. Agent fix flow — command generation, ARM ID parsing, mock execution
8. Create PR from manual_required — status transition, error cases
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.execution_gateway import (
    ExecutionGateway,
    _build_az_commands,
    _parse_arm_id,
)
from src.core.models import (
    ActionTarget,
    ActionType,
    ExecutionStatus,
    GovernanceVerdict,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
    Urgency,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway(tmp_path):
    """Fresh ExecutionGateway with isolated temp dir — no cross-test pollution."""
    return ExecutionGateway(executions_dir=tmp_path / "executions")


def _make_verdict(decision: SRIVerdict, resource_id: str = "vm-web-01") -> GovernanceVerdict:
    """Build a minimal GovernanceVerdict for testing."""
    action = ProposedAction(
        agent_id="monitoring-agent",
        action_type=ActionType.UPDATE_CONFIG,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type="Microsoft.Compute/virtualMachines",
        ),
        reason="Test reason",
        urgency=Urgency.MEDIUM,
    )
    return GovernanceVerdict(
        action_id=f"test-{decision.value}-001",
        timestamp=datetime.now(timezone.utc),
        proposed_action=action,
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=10.0,
            sri_policy=0.0,
            sri_historical=5.0,
            sri_cost=2.0,
            sri_composite=5.5,
        ),
        decision=decision,
        reason=f"{decision.value.upper()} — test",
    )


_TERRAFORM_TAGS = {
    "managed_by": "terraform",
    "iac_repo": "psc0des/ruriskry",
    "iac_path": "infrastructure/terraform-prod",
}


# ---------------------------------------------------------------------------
# 1. IaC Detection
# ---------------------------------------------------------------------------


class TestIaCDetection:
    def test_terraform_tag_detected(self, gateway):
        managed, tool, repo, path = gateway._detect_iac_management(_TERRAFORM_TAGS)
        assert managed is True
        assert tool == "terraform"
        assert repo == "psc0des/ruriskry"
        assert path == "infrastructure/terraform-prod"

    def test_bicep_tag_detected(self, gateway):
        tags = {"managed_by": "bicep", "iac_repo": "org/bicep-repo", "iac_path": "infra"}
        managed, tool, repo, path = gateway._detect_iac_management(tags)
        assert managed is True
        assert tool == "bicep"

    def test_pulumi_tag_detected(self, gateway):
        tags = {"managed_by": "Pulumi", "iac_repo": "org/pulumi-repo"}  # mixed case
        managed, tool, _, _ = gateway._detect_iac_management(tags)
        assert managed is True
        assert tool == "pulumi"

    def test_no_managed_by_tag_not_managed(self, gateway):
        managed, *_ = gateway._detect_iac_management({})
        assert managed is False

    def test_unknown_managed_by_value_not_managed(self, gateway):
        managed, *_ = gateway._detect_iac_management({"managed_by": "ansible"})
        assert managed is False

    def test_tags_without_repo_still_detected(self, gateway):
        tags = {"managed_by": "terraform"}
        managed, tool, repo, path = gateway._detect_iac_management(tags)
        assert managed is True
        assert tool == "terraform"
        assert repo == ""
        assert path == ""


# ---------------------------------------------------------------------------
# 2. Verdict Routing
# ---------------------------------------------------------------------------


class TestVerdictRouting:
    @pytest.mark.asyncio
    async def test_denied_becomes_blocked(self, gateway):
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.blocked
        assert record.verdict == SRIVerdict.DENIED
        assert record.action_id == verdict.action_id

    @pytest.mark.asyncio
    async def test_escalated_becomes_awaiting_review(self, gateway):
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.awaiting_review
        assert record.verdict == SRIVerdict.ESCALATED

    @pytest.mark.asyncio
    async def test_approved_no_tags_becomes_manual_required(self, gateway):
        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            verdict = _make_verdict(SRIVerdict.APPROVED)
            record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required
        assert record.iac_managed is False

    @pytest.mark.asyncio
    async def test_approved_no_repo_becomes_manual_required(self, gateway):
        """IaC tool detected but no repo configured → manual_required."""
        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            tags = {"managed_by": "terraform"}  # no iac_repo
            verdict = _make_verdict(SRIVerdict.APPROVED)
            record = await gateway.process_verdict(verdict, tags)
        assert record.status == ExecutionStatus.manual_required
        assert record.iac_managed is True

    @pytest.mark.asyncio
    async def test_approved_iac_creates_pr_when_enabled(self, gateway):
        """APPROVED + IaC-managed + gateway enabled → pr_created."""
        mock_record_from_pr = None

        async def fake_create_pr(record, verdict):
            record.status = ExecutionStatus.pr_created
            record.pr_url = "https://github.com/psc0des/ruriskry/pull/42"
            record.pr_number = 42
            return record

        gateway._create_terraform_pr = fake_create_pr

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            verdict = _make_verdict(SRIVerdict.APPROVED)
            record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)

        assert record.status == ExecutionStatus.pr_created
        assert record.iac_managed is True
        assert record.iac_tool == "terraform"
        assert record.pr_number == 42

    @pytest.mark.asyncio
    async def test_approved_gateway_disabled_stays_pending(self, gateway):
        """When gateway disabled, APPROVED stays pending (informational)."""
        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = False
            verdict = _make_verdict(SRIVerdict.APPROVED)
            record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)
        assert record.status == ExecutionStatus.pending

    @pytest.mark.asyncio
    async def test_record_stored_after_processing(self, gateway):
        """Records are stored in-memory and retrievable by execution_id."""
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        retrieved = gateway.get_record(record.execution_id)
        assert retrieved is not None
        assert retrieved.execution_id == record.execution_id

    @pytest.mark.asyncio
    async def test_get_records_for_verdict(self, gateway):
        """get_records_for_verdict returns all records for an action_id."""
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        records = gateway.get_records_for_verdict(verdict.action_id)
        assert len(records) == 1
        assert records[0].execution_id == record.execution_id


# ---------------------------------------------------------------------------
# 3. Approval and Dismissal Flows
# ---------------------------------------------------------------------------


class TestApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_escalated_no_iac_becomes_manual(self, gateway):
        """Approving an ESCALATED verdict with no IaC tags → manual_required."""
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.awaiting_review

        approved = await gateway.approve_execution(record.execution_id, "alice@example.com")
        assert approved.reviewed_by == "alice@example.com"
        assert approved.status == ExecutionStatus.manual_required  # no IaC tags

    @pytest.mark.asyncio
    async def test_approve_escalated_iac_calls_pr_generator(self, gateway):
        """Approving ESCALATED + IaC-managed actually calls _create_terraform_pr."""
        pr_called = []

        async def fake_pr(record, verdict):
            pr_called.append(True)
            record.status = ExecutionStatus.pr_created
            record.pr_number = 99
            record.pr_url = "https://github.com/psc0des/ruriskry/pull/99"
            return record

        gateway._create_terraform_pr = fake_pr

        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)
        assert record.status == ExecutionStatus.awaiting_review

        # snapshot must have been stored
        assert record.verdict_snapshot  # non-empty dict

        approved = await gateway.approve_execution(record.execution_id, "bob@example.com")
        assert len(pr_called) == 1, "PR generator was not called"
        assert approved.status == ExecutionStatus.pr_created
        assert approved.pr_number == 99

    @pytest.mark.asyncio
    async def test_approve_non_escalated_raises(self, gateway):
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        # blocked record cannot be approved
        with pytest.raises(ValueError, match="awaiting_review"):
            await gateway.approve_execution(record.execution_id, "alice")

    @pytest.mark.asyncio
    async def test_approve_unknown_id_raises_keyerror(self, gateway):
        # Unknown ID raises KeyError (→ 404 in API), not ValueError (→ 400)
        with pytest.raises(KeyError):
            await gateway.approve_execution("nonexistent-id", "alice")

    @pytest.mark.asyncio
    async def test_dismiss_sets_dismissed_status(self, gateway):
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, {})

        dismissed = await gateway.dismiss_execution(
            record.execution_id, "bob@example.com", "Not needed this sprint"
        )
        assert dismissed.status == ExecutionStatus.dismissed
        assert dismissed.reviewed_by == "bob@example.com"
        assert dismissed.notes == "Not needed this sprint"

    @pytest.mark.asyncio
    async def test_dismiss_unknown_id_raises_keyerror(self, gateway):
        # Unknown ID raises KeyError (→ 404 in API), not ValueError (→ 400)
        with pytest.raises(KeyError):
            await gateway.dismiss_execution("bad-id", "bob")

    @pytest.mark.asyncio
    async def test_get_pending_reviews(self, gateway):
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        await gateway.process_verdict(verdict, {})

        pending = gateway.get_pending_reviews()
        assert len(pending) == 1
        assert pending[0].status == ExecutionStatus.awaiting_review

    @pytest.mark.asyncio
    async def test_dismissed_not_in_pending_reviews(self, gateway):
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, {})
        await gateway.dismiss_execution(record.execution_id, "bob")

        pending = gateway.get_pending_reviews()
        assert len(pending) == 0


# ---------------------------------------------------------------------------
# 4. Gateway Failure Does Not Break Verdict (smoke test)
# ---------------------------------------------------------------------------


class TestVerdictSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_stored_on_process(self, gateway):
        """verdict_snapshot is populated from verdict.model_dump() in process_verdict."""
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        assert record.verdict_snapshot  # non-empty
        assert record.verdict_snapshot["action_id"] == verdict.action_id
        assert record.verdict_snapshot["decision"] == "denied"

    @pytest.mark.asyncio
    async def test_reconstruct_verdict_from_snapshot(self, gateway):
        """_reconstruct_verdict returns a valid GovernanceVerdict from snapshot."""
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, {})

        reconstructed = gateway._reconstruct_verdict(record)
        assert reconstructed is not None
        assert reconstructed.action_id == verdict.action_id
        assert reconstructed.decision == SRIVerdict.ESCALATED

    @pytest.mark.asyncio
    async def test_reconstruct_empty_snapshot_returns_none(self, gateway):
        """_reconstruct_verdict returns None when snapshot is empty."""
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, {})
        record.verdict_snapshot = {}
        result = gateway._reconstruct_verdict(record)
        assert result is None


class TestPersistence:
    @pytest.mark.asyncio
    async def test_records_persist_to_disk(self, tmp_path):
        """Records written to disk can be loaded by a fresh gateway instance."""
        gw1 = ExecutionGateway(executions_dir=tmp_path)
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gw1.process_verdict(verdict, {})

        # New instance — loads from disk
        gw2 = ExecutionGateway(executions_dir=tmp_path)
        loaded = gw2.get_record(record.execution_id)
        assert loaded is not None
        assert loaded.execution_id == record.execution_id
        assert loaded.status == ExecutionStatus.blocked

    @pytest.mark.asyncio
    async def test_empty_dir_loads_cleanly(self, tmp_path):
        """Gateway with an empty directory starts with no records."""
        gw = ExecutionGateway(executions_dir=tmp_path)
        assert gw.get_pending_reviews() == []


class TestGatewayResiliency:
    @pytest.mark.asyncio
    async def test_pr_creation_failure_sets_failed_status(self, gateway):
        """When _create_terraform_pr raises, record.status = failed."""
        async def exploding_pr(record, verdict):
            raise RuntimeError("Simulated GitHub API failure")

        gateway._create_terraform_pr = exploding_pr

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            verdict = _make_verdict(SRIVerdict.APPROVED)
            record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)

        assert record.status == ExecutionStatus.failed
        assert "Simulated GitHub API failure" in record.notes

    @pytest.mark.asyncio
    async def test_pr_exception_handled_gracefully(self, gateway):
        """Any exception from _create_terraform_pr results in failed status,
        not an unhandled exception propagating to the caller."""
        async def exploding_pr(record, verdict):
            raise RuntimeError("Network timeout")

        gateway._create_terraform_pr = exploding_pr

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            verdict = _make_verdict(SRIVerdict.APPROVED)
            # Should NOT raise — exception is caught and mapped to failed status
            record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)

        assert record.status == ExecutionStatus.failed
        assert "Network timeout" in record.notes


# ---------------------------------------------------------------------------
# 5. TerraformPRGenerator (mock GitHub API)
# ---------------------------------------------------------------------------


class TestTerraformPRGenerator:
    @pytest.mark.asyncio
    async def test_no_token_returns_manual_required(self):
        from src.core.terraform_pr_generator import TerraformPRGenerator

        with patch("src.core.terraform_pr_generator.settings") as mock_settings:
            mock_settings.github_token = ""
            mock_settings.iac_github_repo = "psc0des/ruriskry"
            mock_settings.iac_terraform_path = "infrastructure/terraform-prod"
            mock_settings.dashboard_url = "http://localhost:5173"

            gen = TerraformPRGenerator()
            verdict = _make_verdict(SRIVerdict.APPROVED)

            from src.core.models import ExecutionRecord
            record = ExecutionRecord(
                execution_id="exec-001",
                action_id=verdict.action_id,
                verdict=SRIVerdict.APPROVED,
                status=ExecutionStatus.pending,
                iac_managed=True,
                iac_tool="terraform",
                iac_repo="psc0des/ruriskry",
                iac_path="infrastructure/terraform-prod",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

            result = await gen.create_pr(verdict, record)

        assert result.status == ExecutionStatus.manual_required
        assert "GITHUB_TOKEN" in result.notes

    @pytest.mark.asyncio
    async def test_no_repo_returns_manual_required(self):
        from src.core.terraform_pr_generator import TerraformPRGenerator

        with patch("src.core.terraform_pr_generator.settings") as mock_settings:
            mock_settings.github_token = "ghp_fake_token"
            mock_settings.iac_github_repo = ""
            mock_settings.iac_terraform_path = "infrastructure/terraform-prod"
            mock_settings.dashboard_url = "http://localhost:5173"

            gen = TerraformPRGenerator()
            verdict = _make_verdict(SRIVerdict.APPROVED)

            from src.core.models import ExecutionRecord
            record = ExecutionRecord(
                execution_id="exec-002",
                action_id=verdict.action_id,
                verdict=SRIVerdict.APPROVED,
                status=ExecutionStatus.pending,
                iac_managed=True,
                iac_tool="terraform",
                iac_repo="",
                iac_path="infrastructure/terraform-prod",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

            result = await gen.create_pr(verdict, record)

        assert result.status == ExecutionStatus.manual_required
        assert "IAC_GITHUB_REPO" in result.notes

    def test_generate_stub_scale_up(self):
        from src.core.terraform_pr_generator import TerraformPRGenerator
        from src.core.models import ExecutionRecord, ActionType

        with patch("src.core.terraform_pr_generator.settings") as mock_settings:
            mock_settings.github_token = ""
            mock_settings.iac_github_repo = ""
            mock_settings.iac_terraform_path = "infrastructure/terraform-prod"
            mock_settings.dashboard_url = ""
            gen = TerraformPRGenerator()

        action = ProposedAction(
            agent_id="monitoring-agent",
            action_type=ActionType.SCALE_UP,
            target=ActionTarget(
                resource_id="vm-web-01",
                resource_type="Microsoft.Compute/virtualMachines",
                current_sku="Standard_B2ls_v2",
                proposed_sku="Standard_D4as_v4",
            ),
            reason="High CPU utilisation",
        )
        verdict = GovernanceVerdict(
            action_id="test-stub-001",
            timestamp=datetime.now(timezone.utc),
            proposed_action=action,
            skry_risk_index=SRIBreakdown(
                sri_infrastructure=5.0, sri_policy=0.0,
                sri_historical=0.0, sri_cost=10.0, sri_composite=3.5,
            ),
            decision=SRIVerdict.APPROVED,
            reason="APPROVED",
        )
        record = ExecutionRecord(
            execution_id="exec-003",
            action_id=verdict.action_id,
            verdict=SRIVerdict.APPROVED,
            status=ExecutionStatus.pending,
            iac_managed=True,
            iac_tool="terraform",
            iac_repo="psc0des/ruriskry",
            iac_path="infrastructure/terraform-prod",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        stub = gen._generate_terraform_stub(verdict, record)
        assert "scale_up" in stub
        assert "Standard_B2ls_v2" in stub
        assert "Standard_D4as_v4" in stub
        assert "vm-web-01" in stub

    def test_pr_body_contains_verdict_info(self):
        from src.core.terraform_pr_generator import TerraformPRGenerator
        from src.core.models import ExecutionRecord

        with patch("src.core.terraform_pr_generator.settings") as mock_settings:
            mock_settings.github_token = ""
            mock_settings.iac_github_repo = "psc0des/ruriskry"
            mock_settings.iac_terraform_path = "infrastructure/terraform-prod"
            mock_settings.dashboard_url = "http://localhost:5173"
            gen = TerraformPRGenerator()

        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = ExecutionRecord(
            execution_id="exec-004",
            action_id=verdict.action_id,
            verdict=SRIVerdict.APPROVED,
            status=ExecutionStatus.pending,
            iac_managed=True,
            iac_tool="terraform",
            iac_repo="psc0des/ruriskry",
            iac_path="infrastructure/terraform-prod",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        body = gen._build_pr_body(verdict, record)
        assert "APPROVED" in body
        assert "monitoring-agent" in body
        assert "psc0des/ruriskry" in body
        assert "Reviewer Checklist" in body
        assert "http://localhost:5173" in body


# ---------------------------------------------------------------------------
# 8. Flag-until-fixed: get_unresolved_proposals()
# ---------------------------------------------------------------------------


class TestUnresolvedProposals:
    """get_unresolved_proposals() surfaces manual_required records for re-flagging."""

    @pytest.mark.asyncio
    async def test_manual_required_returned(self, gateway):
        """manual_required record → ProposedAction returned."""
        verdict = _make_verdict(SRIVerdict.APPROVED, "vm-no-iac")
        record = await gateway.process_verdict(verdict, resource_tags={})
        assert record.status == ExecutionStatus.manual_required

        pairs = gateway.get_unresolved_proposals()
        assert len(pairs) == 1
        action, rec = pairs[0]
        assert action.target.resource_id == "vm-no-iac"
        assert rec.execution_id == record.execution_id

    @pytest.mark.asyncio
    async def test_pr_created_not_returned(self, gateway):
        """pr_created record (IaC-managed) must NOT be re-proposed."""
        verdict = _make_verdict(SRIVerdict.APPROVED)

        async def _fake_pr(record, v):
            record.status = ExecutionStatus.pr_created
            record.pr_url = "https://github.com/org/repo/pull/1"
            record.pr_number = 1
            return record

        with patch.object(gateway, "_create_terraform_pr", side_effect=_fake_pr):
            record = await gateway.process_verdict(verdict, resource_tags=_TERRAFORM_TAGS)

        assert record.status == ExecutionStatus.pr_created
        assert gateway.get_unresolved_proposals() == []

    @pytest.mark.asyncio
    async def test_awaiting_review_not_returned(self, gateway):
        """awaiting_review (ESCALATED) must NOT be re-proposed — HITL buttons handle it."""
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, resource_tags={})
        assert record.status == ExecutionStatus.awaiting_review
        assert gateway.get_unresolved_proposals() == []

    @pytest.mark.asyncio
    async def test_blocked_not_returned(self, gateway):
        """blocked (DENIED) must NOT be re-proposed."""
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, resource_tags={})
        assert record.status == ExecutionStatus.blocked
        assert gateway.get_unresolved_proposals() == []

    @pytest.mark.asyncio
    async def test_dismissed_not_returned(self, gateway):
        """After dismiss, record must NOT appear in unresolved list."""
        verdict = _make_verdict(SRIVerdict.APPROVED, "vm-dismissed")
        record = await gateway.process_verdict(verdict, resource_tags={})
        assert record.status == ExecutionStatus.manual_required

        await gateway.dismiss_execution(record.execution_id, reviewed_by="alice")
        assert gateway.get_unresolved_proposals() == []

    @pytest.mark.asyncio
    async def test_deduplication_in_new_proposals(self, gateway):
        """Resource already in agent proposals → unresolved record skipped (no duplicate)."""
        verdict = _make_verdict(SRIVerdict.APPROVED, "vm-web-01")
        await gateway.process_verdict(verdict, resource_tags={})

        pairs = gateway.get_unresolved_proposals()
        assert len(pairs) == 1

        # Simulate: agent already proposed same resource + action
        existing_keys = {("vm-web-01", "update_config")}
        filtered = [
            (a, r) for a, r in pairs
            if (a.target.resource_id, a.action_type.value) not in existing_keys
        ]
        assert filtered == []  # nothing left — agent found it naturally

    @pytest.mark.asyncio
    async def test_multiple_unresolved_all_returned(self, gateway):
        """Multiple manual_required records across different resources are all returned."""
        for rid in ("vm-a", "vm-b", "vm-c"):
            await gateway.process_verdict(
                _make_verdict(SRIVerdict.APPROVED, rid), resource_tags={}
            )
        pairs = gateway.get_unresolved_proposals()
        assert len(pairs) == 3
        resource_ids = {a.target.resource_id for a, _ in pairs}
        assert resource_ids == {"vm-a", "vm-b", "vm-c"}


# ---------------------------------------------------------------------------
# 9. ARM ID Parsing
# ---------------------------------------------------------------------------


class TestArmIdParsing:
    def test_full_arm_id(self):
        arm_id = "/subscriptions/sub-1/resourceGroups/rg-prod/providers/Microsoft.Network/networkSecurityGroups/nsg-east"
        result = _parse_arm_id(arm_id)
        assert result["resource_group"] == "rg-prod"
        assert result["name"] == "nsg-east"
        assert result["provider"] == "Microsoft.Network/networkSecurityGroups"
        assert result["resource_type"] == "networkSecurityGroups"

    def test_short_name_only(self):
        result = _parse_arm_id("vm-web-01")
        assert result["name"] == "vm-web-01"
        assert result["resource_group"] == ""

    def test_two_part_path(self):
        result = _parse_arm_id("virtualMachines/vm-web-01")
        assert result["name"] == "vm-web-01"
        assert result["resource_type"] == "virtualMachines"


# ---------------------------------------------------------------------------
# 10. Agent Fix — Command Generation
# ---------------------------------------------------------------------------


class TestAgentFixFlow:
    def test_modify_nsg_command(self):
        action = ProposedAction(
            agent_id="deploy-agent",
            action_type=ActionType.MODIFY_NSG,
            target=ActionTarget(
                resource_id="/subscriptions/sub/resourceGroups/rg-prod/providers/Microsoft.Network/networkSecurityGroups/nsg-east",
                resource_type="Microsoft.Network/networkSecurityGroups",
            ),
            reason="Dangerous rule 'AllowAll_Inbound' — opens all ports",
        )
        cmds = _build_az_commands(action)
        assert len(cmds) == 1
        assert "nsg rule delete" in cmds[0]
        assert "--nsg-name nsg-east" in cmds[0]
        assert "--name AllowAll_Inbound" in cmds[0]
        assert "--resource-group rg-prod" in cmds[0]

    def test_scale_down_command(self):
        action = ProposedAction(
            agent_id="cost-agent",
            action_type=ActionType.SCALE_DOWN,
            target=ActionTarget(
                resource_id="/subscriptions/sub/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/vm-dr-01",
                resource_type="Microsoft.Compute/virtualMachines",
                current_sku="Standard_D4as_v4",
                proposed_sku="Standard_B2ls_v2",
            ),
            reason="CPU avg 3.2% — right-size candidate",
        )
        cmds = _build_az_commands(action)
        assert len(cmds) == 1
        assert "vm resize" in cmds[0]
        assert "--size Standard_B2ls_v2" in cmds[0]
        assert "--name vm-dr-01" in cmds[0]

    def test_delete_resource_full_arm_id(self):
        arm_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-old"
        action = ProposedAction(
            agent_id="cost-agent",
            action_type=ActionType.DELETE_RESOURCE,
            target=ActionTarget(resource_id=arm_id, resource_type="Microsoft.Compute/virtualMachines"),
            reason="Unused resource",
        )
        cmds = _build_az_commands(action)
        assert "--ids" in cmds[0]
        assert arm_id in cmds[0]

    def test_restart_service_command(self):
        action = ProposedAction(
            agent_id="monitoring-agent",
            action_type=ActionType.RESTART_SERVICE,
            target=ActionTarget(
                resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-web-01",
                resource_type="Microsoft.Compute/virtualMachines",
            ),
            reason="High memory pressure",
        )
        cmds = _build_az_commands(action)
        assert "vm restart" in cmds[0]
        assert "--name vm-web-01" in cmds[0]

    def test_unknown_action_type_fallback(self):
        action = ProposedAction(
            agent_id="test",
            action_type=ActionType.UPDATE_CONFIG,
            target=ActionTarget(resource_id="some-resource", resource_type="SomeType"),
            reason="Test",
        )
        cmds = _build_az_commands(action)
        assert cmds[0].startswith("#")

    def test_nsg_no_rule_name_uses_placeholder(self):
        action = ProposedAction(
            agent_id="deploy-agent",
            action_type=ActionType.MODIFY_NSG,
            target=ActionTarget(resource_id="nsg-test", resource_type="Microsoft.Network/networkSecurityGroups"),
            reason="Bad rule detected",
        )
        cmds = _build_az_commands(action)
        assert "<RULE_NAME>" in cmds[0]

    @pytest.mark.asyncio
    async def test_generate_agent_fix_commands(self, gateway):
        """generate_agent_fix_commands() returns preview dict."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        preview = gateway.generate_agent_fix_commands(record.execution_id)
        assert "commands" in preview
        assert "warning" in preview
        assert preview["execution_id"] == record.execution_id

    @pytest.mark.asyncio
    async def test_generate_commands_unknown_id_raises(self, gateway):
        with pytest.raises(KeyError):
            gateway.generate_agent_fix_commands("nonexistent")

    @pytest.mark.asyncio
    async def test_mock_mode_execution_sets_applied(self, gateway):
        """In mock mode, execute_agent_fix sets status=applied."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.use_local_mocks = True
            result = await gateway.execute_agent_fix(record.execution_id, "alice")

        assert result.status == ExecutionStatus.applied
        assert "[mock]" in result.notes
        assert result.reviewed_by == "alice"

    @pytest.mark.asyncio
    async def test_execute_wrong_status_raises(self, gateway):
        """Cannot execute agent fix on a non-manual_required record."""
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.blocked

        with pytest.raises(ValueError, match="manual_required"):
            await gateway.execute_agent_fix(record.execution_id, "alice")

    @pytest.mark.asyncio
    async def test_sdk_missing_arm_id_raises_valueerror(self, gateway):
        """Short resource name (no ARM ID) raises ValueError — record stays intact."""
        # _make_verdict uses "vm-web-01" (short name, no resource group)
        verdict = _make_verdict(SRIVerdict.APPROVED)
        # Override action to RESTART_SERVICE (supported type) but with short name
        verdict.proposed_action.action_type = ActionType.RESTART_SERVICE
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.use_local_mocks = False
            with pytest.raises(ValueError, match="full ARM ID"):
                await gateway.execute_agent_fix(record.execution_id, "alice")

        still = gateway.get_record(record.execution_id)
        assert still.status == ExecutionStatus.manual_required

    @pytest.mark.asyncio
    async def test_sdk_unsupported_action_raises_valueerror(self, gateway):
        """Unsupported action type raises ValueError — record stays intact."""
        # Use a full ARM ID so we pass the ARM check, but UPDATE_CONFIG type
        action = ProposedAction(
            agent_id="test-agent",
            action_type=ActionType.UPDATE_CONFIG,
            target=ActionTarget(
                resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/app-01",
                resource_type="Microsoft.Web/sites",
            ),
            reason="Test unsupported action",
        )
        verdict = GovernanceVerdict(
            action_id="test-unsupported-001",
            timestamp=datetime.now(timezone.utc),
            proposed_action=action,
            skry_risk_index=SRIBreakdown(
                sri_infrastructure=5.0, sri_policy=0.0,
                sri_historical=0.0, sri_cost=0.0, sri_composite=1.5,
            ),
            decision=SRIVerdict.APPROVED,
            reason="APPROVED",
        )

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.use_local_mocks = False
            with pytest.raises(ValueError, match="not supported"):
                await gateway.execute_agent_fix(record.execution_id, "alice")

        still = gateway.get_record(record.execution_id)
        assert still.status == ExecutionStatus.manual_required


# ---------------------------------------------------------------------------
# 11. Create PR from manual_required
# ---------------------------------------------------------------------------


class TestCreatePRFromManual:
    @pytest.mark.asyncio
    async def test_creates_pr_from_manual(self, gateway):
        """create_pr_from_manual delegates to _create_terraform_pr."""
        pr_calls = []

        async def fake_pr(record, verdict):
            pr_calls.append(True)
            record.status = ExecutionStatus.pr_created
            record.pr_url = "https://github.com/org/repo/pull/10"
            record.pr_number = 10
            return record

        gateway._create_terraform_pr = fake_pr

        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        result = await gateway.create_pr_from_manual(record.execution_id, "bob")
        assert len(pr_calls) == 1
        assert result.status == ExecutionStatus.pr_created
        assert result.reviewed_by == "bob"

    @pytest.mark.asyncio
    async def test_wrong_status_raises(self, gateway):
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        with pytest.raises(ValueError, match="manual_required"):
            await gateway.create_pr_from_manual(record.execution_id, "alice")

    @pytest.mark.asyncio
    async def test_unknown_id_raises(self, gateway):
        with pytest.raises(KeyError):
            await gateway.create_pr_from_manual("bad-id", "alice")

    @pytest.mark.asyncio
    async def test_missing_snapshot_raises(self, gateway):
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        record.verdict_snapshot = {}
        with pytest.raises(ValueError, match="snapshot"):
            await gateway.create_pr_from_manual(record.execution_id, "alice")
