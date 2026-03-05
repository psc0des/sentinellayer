"""Tests for the Phase 21 Execution Gateway.

Covers:
1. IaC detection via resource tags
2. Verdict routing: DENIED → blocked, ESCALATED → awaiting_review,
   APPROVED + IaC → pr_created, APPROVED + no IaC → manual_required
3. Approval and dismissal flows
4. Gateway failure does not break verdict (integration smoke test)
5. Edge cases: unknown execution_id, wrong-state approvals
6. TerraformPRGenerator without PyGithub (graceful degradation)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.execution_gateway import ExecutionGateway
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
def gateway():
    """Fresh ExecutionGateway with no prior records."""
    return ExecutionGateway()


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
    async def test_approve_escalated_record(self, gateway):
        verdict = _make_verdict(SRIVerdict.ESCALATED)
        record = await gateway.process_verdict(verdict, {})
        assert record.status == ExecutionStatus.awaiting_review

        approved = await gateway.approve_execution(record.execution_id, "alice@example.com")
        assert approved.reviewed_by == "alice@example.com"
        assert approved.status in (
            ExecutionStatus.pr_created,
            ExecutionStatus.manual_required,
        )

    @pytest.mark.asyncio
    async def test_approve_non_escalated_raises(self, gateway):
        verdict = _make_verdict(SRIVerdict.DENIED)
        record = await gateway.process_verdict(verdict, {})
        # blocked record cannot be approved
        with pytest.raises(ValueError, match="awaiting_review"):
            await gateway.approve_execution(record.execution_id, "alice")

    @pytest.mark.asyncio
    async def test_approve_unknown_id_raises(self, gateway):
        with pytest.raises(ValueError, match="Unknown execution_id"):
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
    async def test_dismiss_unknown_id_raises(self, gateway):
        with pytest.raises(ValueError, match="Unknown execution_id"):
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
