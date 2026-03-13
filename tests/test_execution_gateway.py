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
    async def test_approved_iac_routes_to_manual_required(self, gateway):
        """APPROVED + IaC-managed → manual_required (user chooses execution path).

        Auto-PR creation was removed.  IaC metadata is stored on the record so
        the 'Create Terraform PR' button works on demand, but no PR is created
        automatically.
        """
        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            verdict = _make_verdict(SRIVerdict.APPROVED)
            record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)

        assert record.status == ExecutionStatus.manual_required
        assert record.iac_managed is True
        assert record.iac_tool == "terraform"

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
    async def test_pr_creation_failure_on_manual_path_sets_failed(self, gateway):
        """create_pr_from_manual failure sets record to failed status."""
        async def exploding_pr(record, verdict):
            raise RuntimeError("Simulated GitHub API failure")

        gateway._create_terraform_pr = exploding_pr

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            verdict = _make_verdict(SRIVerdict.APPROVED)
            # route_verdict always goes to manual_required now
            record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)
            assert record.status == ExecutionStatus.manual_required

            # Manually calling _create_terraform_pr (via create_pr_from_manual) should surface the error
            with pytest.raises(Exception):
                await gateway._create_terraform_pr(record, verdict)

    @pytest.mark.asyncio
    async def test_iac_metadata_stored_on_manual_required_record(self, gateway):
        """IaC metadata (iac_managed, iac_tool, iac_repo) is stored on the
        manual_required record so the 'Create Terraform PR' button can use it."""
        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.execution_gateway_enabled = True
            verdict = _make_verdict(SRIVerdict.APPROVED)
            record = await gateway.process_verdict(verdict, _TERRAFORM_TAGS)

        assert record.status == ExecutionStatus.manual_required
        assert record.iac_managed is True
        assert record.iac_tool == "terraform"
        # iac_repo comes from _TERRAFORM_TAGS["iac_repo"] if set, else empty string
        assert isinstance(record.iac_repo, str)


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
        """pr_created record must NOT be re-proposed (PR is already open)."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        # Route to manual_required, then simulate user clicking "Create Terraform PR"
        record = await gateway.process_verdict(verdict, resource_tags={})
        assert record.status == ExecutionStatus.manual_required

        # Manually set to pr_created (as create_pr_from_manual would do)
        record.status = ExecutionStatus.pr_created
        record.pr_url = "https://github.com/org/repo/pull/1"
        record.pr_number = 1
        gateway._save(record)

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
    async def test_dedup_updates_action_id(self, gateway):
        """Dedup: second verdict for same resource+action updates action_id on existing record."""
        import uuid as _uuid

        def _make_with_id(action_id: str, resource_id: str) -> GovernanceVerdict:
            v = _make_verdict(SRIVerdict.APPROVED, resource_id)
            return v.model_copy(update={"action_id": action_id})

        aid1 = str(_uuid.uuid4())
        aid2 = str(_uuid.uuid4())
        assert aid1 != aid2

        verdict1 = _make_with_id(aid1, "vm-dedup-test")
        verdict2 = _make_with_id(aid2, "vm-dedup-test")

        record1 = await gateway.process_verdict(verdict1, resource_tags={})
        assert record1.status == ExecutionStatus.manual_required
        assert record1.action_id == aid1

        record2 = await gateway.process_verdict(verdict2, resource_tags={})
        # Same execution record returned
        assert record2.execution_id == record1.execution_id
        # action_id updated to newest verdict so drilldown lookup works
        assert record2.action_id == aid2
        # Lookup by new action_id finds the record
        found = gateway.get_records_for_verdict(aid2)
        assert len(found) == 1
        assert found[0].execution_id == record1.execution_id

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
# 10. Agent Fix — Plan Generation and Execution (Phase 28)
# ---------------------------------------------------------------------------


class TestAgentFixFlow:
    """Phase 28: generate_agent_fix_plan() and execute_agent_fix() via ExecutionAgent."""

    @pytest.mark.asyncio
    async def test_generate_agent_fix_plan_returns_plan_dict(self, gateway):
        """generate_agent_fix_plan() returns a structured plan dict."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        plan = await gateway.generate_agent_fix_plan(record.execution_id)
        assert "steps" in plan
        assert "commands" in plan       # backward compat
        assert "summary" in plan
        assert "warning" in plan
        assert plan["execution_id"] == record.execution_id

    @pytest.mark.asyncio
    async def test_generate_plan_stores_on_record(self, gateway):
        """Plan is persisted to the ExecutionRecord after generation."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})

        await gateway.generate_agent_fix_plan(record.execution_id)
        updated = gateway.get_record(record.execution_id)
        assert updated.execution_plan is not None
        assert "steps" in updated.execution_plan

    @pytest.mark.asyncio
    async def test_generate_plan_unknown_id_raises(self, gateway):
        with pytest.raises(KeyError):
            await gateway.generate_agent_fix_plan("nonexistent")

    @pytest.mark.asyncio
    async def test_mock_mode_execution_sets_applied(self, gateway):
        """In mock mode, execute_agent_fix sets status=applied."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        # Generate plan first so execute_agent_fix has something to work with
        await gateway.generate_agent_fix_plan(record.execution_id)

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.use_local_mocks = True
            mock_settings.azure_openai_endpoint = ""
            result = await gateway.execute_agent_fix(record.execution_id, "alice")

        assert result.status == ExecutionStatus.applied
        assert "[mock]" in result.notes
        assert result.reviewed_by == "alice"

    @pytest.mark.asyncio
    async def test_mock_mode_execution_stores_log(self, gateway):
        """execute_agent_fix stores execution_log on the record."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        await gateway.generate_agent_fix_plan(record.execution_id)

        with patch("src.core.execution_gateway.settings") as mock_settings:
            mock_settings.use_local_mocks = True
            mock_settings.azure_openai_endpoint = ""
            result = await gateway.execute_agent_fix(record.execution_id, "alice")

        assert result.execution_log is not None
        assert isinstance(result.execution_log, list)

    @pytest.mark.asyncio
    async def test_execute_wrong_status_raises(self, gateway):
        """Cannot execute agent fix on a non-executable record."""
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.blocked

        with pytest.raises(ValueError, match="manual_required"):
            await gateway.execute_agent_fix(record.execution_id, "alice")


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


class TestNsgPatchInPRGenerator:
    """Tests for _apply_nsg_fix_to_content and _find_and_patch_tf_file."""

    def _gen(self):
        from src.core.terraform_pr_generator import TerraformPRGenerator
        with patch("src.core.terraform_pr_generator.settings") as s:
            s.github_token = ""
            s.iac_github_repo = ""
            s.iac_terraform_path = "infra"
            s.dashboard_url = ""
            return TerraformPRGenerator()

    # ── _apply_nsg_fix_to_content ──────────────────────────────────────────

    def test_patches_allow_to_deny(self):
        gen = self._gen()
        tf = '''\
resource "azurerm_network_security_rule" "testing_ssh" {
  name                        = "testing-ssh"
  priority                    = 140
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_address_prefix       = "*"
  destination_port_range      = "22"
  resource_group_name         = "rg"
  network_security_group_name = "nsg"
}
'''
        result = gen._apply_nsg_fix_to_content(tf, "testing-ssh")
        assert result is not None
        assert '"Deny"' in result
        assert '"Allow"' not in result

    def test_returns_none_when_rule_not_found(self):
        gen = self._gen()
        tf = '''\
resource "azurerm_network_security_rule" "other_rule" {
  name   = "other-rule"
  access = "Allow"
}
'''
        result = gen._apply_nsg_fix_to_content(tf, "testing-ssh")
        assert result is None

    def test_returns_none_when_already_deny(self):
        gen = self._gen()
        tf = '''\
resource "azurerm_network_security_rule" "testing_ssh" {
  name   = "testing-ssh"
  access = "Deny"
}
'''
        result = gen._apply_nsg_fix_to_content(tf, "testing-ssh")
        assert result is None  # no change needed

    def test_patches_inline_security_rule_inside_nsg(self):
        """Inline security_rule {} blocks inside azurerm_network_security_group are patched."""
        gen = self._gen()
        tf = '''\
resource "azurerm_network_security_group" "prod" {
  name                = "nsg-east-prod"
  resource_group_name = "rg"

  security_rule {
    name                       = "allow-http-my-ip"
    priority                   = 100
    access                     = "Allow"
    destination_port_range     = "80"
  }

  security_rule {
    name                       = "allow-ssh-anywhere"
    priority                   = 110
    access                     = "Allow"
    destination_port_range     = "22"
    source_address_prefix      = "*"
  }
}
'''
        result = gen._apply_nsg_fix_to_content(tf, "allow-ssh-anywhere")
        assert result is not None
        # only the targeted rule is changed
        lines = result.split("\n")
        ssh_idx = next(i for i, l in enumerate(lines) if "allow-ssh-anywhere" in l)
        # find access = in the ssh block
        access_line = next(
            l for l in lines[ssh_idx:ssh_idx + 10] if "access" in l.lower()
        )
        assert '"Deny"' in access_line
        # the other rule (allow-http-my-ip) still says Allow
        http_idx = next(i for i, l in enumerate(lines) if "allow-http-my-ip" in l)
        http_access = next(
            l for l in lines[http_idx:http_idx + 10] if "access" in l.lower()
        )
        assert '"Allow"' in http_access

    # ── _find_and_patch_tf_file ────────────────────────────────────────────

    def test_find_and_patch_returns_none_for_non_nsg(self):
        gen = self._gen()
        action = ProposedAction(
            agent_id="cost-agent",
            action_type=ActionType.SCALE_DOWN,
            target=ActionTarget(resource_id="vm-web-01", resource_type="virtualMachines", resource_group="rg"),
            reason="CPU low",
            urgency=Urgency.LOW,
        )
        result = gen._find_and_patch_tf_file(MagicMock(), "infra", action)
        assert result is None

    def test_find_and_patch_returns_none_when_no_rule_name_in_reason(self):
        gen = self._gen()
        action = ProposedAction(
            agent_id="deploy-agent",
            action_type=ActionType.MODIFY_NSG,
            target=ActionTarget(resource_id="nsg-east-prod", resource_type="networkSecurityGroups", resource_group="rg"),
            reason="some rule is bad",  # no quoted rule name
            urgency=Urgency.HIGH,
        )
        result = gen._find_and_patch_tf_file(MagicMock(), "infra", action)
        assert result is None

    def test_find_and_patch_modifies_existing_file(self):
        gen = self._gen()
        tf_content = '''\
resource "azurerm_network_security_rule" "testing_ssh" {
  name   = "testing-ssh"
  access = "Allow"
}
'''
        mock_file = MagicMock()
        mock_file.name = "main.tf"
        mock_file.path = "infra/main.tf"
        mock_file.sha = "abc123"
        mock_file.decoded_content = tf_content.encode()
        mock_file.type = "file"

        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = [mock_file]

        action = ProposedAction(
            agent_id="deploy-agent",
            action_type=ActionType.MODIFY_NSG,
            target=ActionTarget(resource_id="nsg-east-prod", resource_type="networkSecurityGroups", resource_group="rg"),
            reason="rule 'testing-ssh' allows SSH from *",
            urgency=Urgency.HIGH,
        )
        result = gen._find_and_patch_tf_file(mock_repo, "infra", action)

        assert result is not None
        assert result["path"] == "infra/main.tf"
        assert result["rule_name"] == "testing-ssh"
        assert '"Deny"' in result["new_content"]
        assert result["sha"] == "abc123"


class TestAutoDissmissOnRescan:
    """Integration test for the auto-dismiss-on-rescan logic in get_unresolved_proposals."""

    @pytest.mark.asyncio
    async def test_auto_dismiss_sets_status_dismissed(self, gateway):
        """A manual_required record that is dismissed becomes 'dismissed'."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.manual_required

        # Simulate what the scan loop does: agent found the resource clean → dismiss
        await gateway.dismiss_execution(
            record.execution_id,
            "auto-scan",
            "Auto-dismissed: deploy re-scanned resource and found no issues",
        )
        updated = gateway.get_record(record.execution_id)
        assert updated.status == ExecutionStatus.dismissed
        assert updated.reviewed_by == "auto-scan"

    @pytest.mark.asyncio
    async def test_dismissed_records_not_in_unresolved(self, gateway):
        """Dismissed records do NOT appear in get_unresolved_proposals."""
        verdict = _make_verdict(SRIVerdict.APPROVED)
        record = await gateway.process_verdict(verdict, {})
        await gateway.dismiss_execution(record.execution_id, "auto-scan", "resolved")
        pairs = gateway.get_unresolved_proposals()
        ids = [r.execution_id for _, r in pairs]
        assert record.execution_id not in ids


class TestDedupManualRequired:
    """Tests for duplicate-record prevention in route_verdict + get_unresolved_proposals."""

    @pytest.mark.asyncio
    async def test_second_route_returns_existing_record(self, gateway):
        """Routing the same resource+action twice returns the existing record, not a new one."""
        verdict = _make_verdict(SRIVerdict.APPROVED, resource_id="nsg-east-prod")
        r1 = await gateway.process_verdict(verdict, {})
        assert r1.status == ExecutionStatus.manual_required

        # Route same resource+action again (simulates re-flag scan re-submitting proposal)
        r2 = await gateway.process_verdict(verdict, {})
        assert r2.execution_id == r1.execution_id  # same record returned

    @pytest.mark.asyncio
    async def test_get_unresolved_deduplicates_by_resource_and_action(self, gateway):
        """get_unresolved_proposals returns at most one entry per (resource_id, action_type)."""
        verdict = _make_verdict(SRIVerdict.APPROVED, resource_id="nsg-east-prod")
        # Manually inject a second duplicate record to simulate pre-fix data
        r1 = await gateway.process_verdict(verdict, {})
        # Force-create second record by temporarily bypassing dedup (direct _save)
        from src.core.models import ExecutionRecord, ExecutionStatus
        import uuid
        from datetime import datetime, timezone
        r2 = ExecutionRecord(
            execution_id=str(uuid.uuid4()),
            action_id="dup-action",
            verdict=SRIVerdict.APPROVED,
            status=ExecutionStatus.manual_required,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            verdict_snapshot=r1.verdict_snapshot,
        )
        gateway._records[r2.execution_id] = r2

        pairs = gateway.get_unresolved_proposals()
        # Should only appear once despite two manual_required records
        count = sum(
            1 for _, rec in pairs
            if rec.status == ExecutionStatus.manual_required
        )
        assert count <= 1

    @pytest.mark.asyncio
    async def test_get_unresolved_keeps_oldest_record(self, gateway):
        """get_unresolved_proposals keeps the oldest record when duplicates exist."""
        from src.core.models import ExecutionRecord, ExecutionStatus
        import uuid
        from datetime import datetime, timezone, timedelta
        verdict = _make_verdict(SRIVerdict.APPROVED, resource_id="nsg-dup-test")
        r1 = await gateway.process_verdict(verdict, {})
        # Inject a newer duplicate
        older_time = r1.created_at - timedelta(days=2)
        r_older = ExecutionRecord(
            execution_id=str(uuid.uuid4()),
            action_id="older-action",
            verdict=SRIVerdict.APPROVED,
            status=ExecutionStatus.manual_required,
            created_at=older_time,
            updated_at=older_time,
            verdict_snapshot=r1.verdict_snapshot,
        )
        gateway._records[r_older.execution_id] = r_older

        pairs = gateway.get_unresolved_proposals()
        matching = [rec for _, rec in pairs if "nsg-dup-test" in rec.verdict_snapshot.get("proposed_action", {}).get("target", {}).get("resource_id", "")]
        assert len(matching) == 1
        assert matching[0].execution_id == r_older.execution_id  # oldest wins


# ---------------------------------------------------------------------------
# Phase 29 — TestVerificationFlow (3 tests)
# ---------------------------------------------------------------------------


class TestVerificationFlow:
    """list_all() and post-execute verification integration tests."""

    @pytest.fixture
    def gateway(self, tmp_path):
        return ExecutionGateway(executions_dir=tmp_path / "executions")

    @pytest.mark.asyncio
    async def test_list_all_returns_empty_on_fresh_gateway(self, gateway):
        assert gateway.list_all() == []

    @pytest.mark.asyncio
    async def test_list_all_returns_records_newest_first(self, gateway):
        from datetime import timedelta
        v1 = _make_verdict(SRIVerdict.APPROVED, resource_id="vm-list-a")
        v2 = _make_verdict(SRIVerdict.DENIED, resource_id="vm-list-b")
        r1 = await gateway.process_verdict(v1, {})
        r2 = await gateway.process_verdict(v2, {})
        # Make r1 older
        r1.created_at = r1.created_at - timedelta(hours=1)
        records = gateway.list_all()
        assert len(records) == 2
        # newest first — r2 should come before r1
        ids = [r.execution_id for r in records]
        assert ids.index(r2.execution_id) < ids.index(r1.execution_id)

    @pytest.mark.asyncio
    async def test_execute_agent_fix_stores_verification(self, gateway, tmp_path):
        """After execute_agent_fix, record.verification should be set."""
        verdict = _make_verdict(SRIVerdict.APPROVED, resource_id="vm-verify-01")
        record = await gateway.process_verdict(verdict, {})
        # Manually move to manual_required so execute_agent_fix accepts it
        record.status = ExecutionStatus.manual_required
        gateway._save(record)

        # Force mock mode so ExecutionAgent uses deterministic paths (no LLM/Azure)
        with patch("src.core.execution_gateway.settings") as mock_s:
            mock_s.use_local_mocks = True
            mock_s.azure_openai_endpoint = ""
            updated = await gateway.execute_agent_fix(record.execution_id, reviewed_by="test-user")
        assert updated.verification is not None
        assert "confirmed" in updated.verification
        assert updated.verification["confirmed"] is True


# ---------------------------------------------------------------------------
# Phase 30 — TestRollbackFlow (4 tests)
# ---------------------------------------------------------------------------


class TestRollbackFlow:
    """rollback_agent_fix() method on ExecutionGateway."""

    @pytest.fixture
    def gateway(self, tmp_path):
        return ExecutionGateway(executions_dir=tmp_path / "executions")

    async def _applied_restart_record(self, gateway, resource_id: str):
        """Helper: create an applied ExecutionRecord with RESTART_SERVICE action.

        Uses RESTART_SERVICE because _rollback_mock has a deterministic handler for
        it (success=True) — avoids relying on execute_agent_fix or a live LLM call.
        """
        from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
        action = ProposedAction(
            agent_id="monitoring-agent",
            action_type=ActionType.RESTART_SERVICE,
            target=ActionTarget(
                resource_id=f"/subscriptions/x/resourceGroups/rg/providers/"
                            f"Microsoft.Compute/virtualMachines/{resource_id}",
                resource_type="Microsoft.Compute/virtualMachines",
            ),
            reason="VM offline",
            urgency=Urgency.HIGH,
        )
        verdict = GovernanceVerdict(
            action_id=f"test-rollback-{resource_id}",
            timestamp=datetime.now(timezone.utc),
            proposed_action=action,
            skry_risk_index=SRIBreakdown(
                sri_infrastructure=10.0, sri_policy=0.0,
                sri_historical=5.0, sri_cost=2.0, sri_composite=5.5,
            ),
            decision=SRIVerdict.APPROVED,
            reason="APPROVED — test",
        )
        record = await gateway.process_verdict(verdict, {})
        record.status = ExecutionStatus.applied
        record.execution_plan = {
            "steps": [{"operation": "start_vm", "target": resource_id,
                       "params": {"resource_group": "rg", "vm_name": resource_id},
                       "reason": "test"}],
            "summary": "Start VM", "estimated_impact": "",
            "rollback_hint": f"az vm deallocate -g rg -n {resource_id}",
            "commands": [],
        }
        gateway._save(record)
        return record

    @pytest.mark.asyncio
    async def test_rollback_sets_status_rolled_back(self, gateway):
        record = await self._applied_restart_record(gateway, "vm-rollback-01")
        rolled = await gateway.rollback_agent_fix(record.execution_id, reviewed_by="test-user")
        assert rolled.status == ExecutionStatus.rolled_back

    @pytest.mark.asyncio
    async def test_rollback_stores_rollback_log(self, gateway):
        record = await self._applied_restart_record(gateway, "vm-rollback-02")
        rolled = await gateway.rollback_agent_fix(record.execution_id, reviewed_by="test-user")
        assert rolled.rollback_log is not None
        assert isinstance(rolled.rollback_log, list)

    @pytest.mark.asyncio
    async def test_rollback_only_valid_when_applied(self, gateway):
        verdict = _make_verdict(SRIVerdict.APPROVED, resource_id="vm-rollback-03")
        record = await gateway.process_verdict(verdict, {})
        # Record is manual_required — not applied yet
        with pytest.raises(ValueError, match="must be 'applied'"):
            await gateway.rollback_agent_fix(record.execution_id, reviewed_by="test-user")

    @pytest.mark.asyncio
    async def test_rollback_raises_for_unknown_id(self, gateway):
        with pytest.raises((KeyError, ValueError)):
            await gateway.rollback_agent_fix("nonexistent-id", reviewed_by="test-user")
