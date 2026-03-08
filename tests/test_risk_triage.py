"""Tests for Phase 26 — Risk Triage Foundation.

Covers:
  - compute_fingerprint(): environment detection, network exposure, data plane,
    criticality, blast radius, reversibility, compliance scope
  - classify_tier(): all four routing rules + default
  - build_org_context(): settings → OrgContext conversion
  - Edge cases: None resource_metadata, None org_context, unknown environments
"""

import pytest

from src.core.models import ActionType, OrgContext, ProposedAction, ActionTarget, Urgency
from src.core.risk_triage import (
    build_org_context,
    classify_tier,
    compute_fingerprint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_action(
    action_type: ActionType = ActionType.UPDATE_CONFIG,
    resource_id: str = "vm-01",
    resource_type: str = "microsoft.compute/virtualmachines",
    resource_group: str | None = None,
    nsg_change_direction: str | None = None,
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
            resource_group=resource_group,
        ),
        reason="test",
        urgency=Urgency.LOW,
        nsg_change_direction=nsg_change_direction,
    )


def _make_org(
    frameworks: list[str] | None = None,
    risk_tolerance: str = "moderate",
    critical_rgs: list[str] | None = None,
) -> OrgContext:
    return OrgContext(
        org_name="TestOrg",
        resource_count=100,
        compliance_frameworks=frameworks or [],
        risk_tolerance=risk_tolerance,
        business_critical_rgs=critical_rgs or [],
    )


# ---------------------------------------------------------------------------
# compute_fingerprint — environment detection
# ---------------------------------------------------------------------------

class TestEnvironmentDetection:
    def test_tag_wins_over_name_inference(self):
        action = _make_action(resource_id="vm-dev-01")
        metadata = {"tags": {"environment": "production"}}
        fp = compute_fingerprint(action, metadata, None)
        assert fp.environment == "production"
        assert fp.is_production is True

    def test_prod_inferred_from_resource_id(self):
        action = _make_action(resource_id="vm-prod-01")
        fp = compute_fingerprint(action, None, None)
        assert fp.environment == "production"
        assert fp.is_production is True

    def test_dev_inferred_from_resource_id(self):
        action = _make_action(resource_id="vm-dev-01")
        fp = compute_fingerprint(action, None, None)
        assert fp.environment == "development"
        assert fp.is_production is False

    def test_staging_inferred_from_uat(self):
        action = _make_action(resource_id="sql-uat-db")
        fp = compute_fingerprint(action, None, None)
        assert fp.environment == "staging"

    def test_unknown_environment_when_no_hints(self):
        action = _make_action(resource_id="my-resource-42")
        fp = compute_fingerprint(action, None, None)
        assert fp.environment == "unknown"
        assert fp.is_production is False

    def test_sandbox_inferred_as_development(self):
        action = _make_action(resource_id="func-sandbox-01")
        fp = compute_fingerprint(action, None, None)
        assert fp.environment == "development"


# ---------------------------------------------------------------------------
# compute_fingerprint — network exposure
# ---------------------------------------------------------------------------

class TestNetworkExposure:
    def test_nsg_resource_type_detected(self):
        action = _make_action(resource_type="microsoft.network/networksecuritygroups")
        fp = compute_fingerprint(action, None, None)
        assert fp.has_network_exposure is True

    def test_modify_nsg_action_type_detected(self):
        action = _make_action(action_type=ActionType.MODIFY_NSG)
        fp = compute_fingerprint(action, None, None)
        assert fp.has_network_exposure is True

    def test_nsg_direction_open_detected(self):
        action = _make_action(nsg_change_direction="open")
        fp = compute_fingerprint(action, None, None)
        assert fp.has_network_exposure is True

    def test_nsg_direction_restrict_no_exposure(self):
        action = _make_action(
            action_type=ActionType.MODIFY_NSG,
            resource_type="microsoft.compute/virtualmachines",
            nsg_change_direction="restrict",
        )
        # action_type=MODIFY_NSG always sets exposure; restrict is not a factor
        fp = compute_fingerprint(action, None, None)
        assert fp.has_network_exposure is True  # because action_type is MODIFY_NSG

    def test_vm_scale_up_no_network_exposure(self):
        action = _make_action(
            action_type=ActionType.SCALE_UP,
            resource_type="microsoft.compute/virtualmachines",
        )
        fp = compute_fingerprint(action, None, None)
        assert fp.has_network_exposure is False

    def test_firewall_resource_detected(self):
        action = _make_action(resource_type="microsoft.network/azurefirewalls")
        fp = compute_fingerprint(action, None, None)
        assert fp.has_network_exposure is True

    def test_public_ip_resource_detected(self):
        action = _make_action(resource_type="microsoft.network/publicipaddresses")
        fp = compute_fingerprint(action, None, None)
        assert fp.has_network_exposure is True


# ---------------------------------------------------------------------------
# compute_fingerprint — data plane
# ---------------------------------------------------------------------------

class TestDataPlane:
    def test_storage_account_detected(self):
        action = _make_action(resource_type="microsoft.storage/storageaccounts")
        fp = compute_fingerprint(action, None, None)
        assert fp.has_data_plane_impact is True

    def test_keyvault_detected(self):
        action = _make_action(resource_type="microsoft.keyvault/vaults")
        fp = compute_fingerprint(action, None, None)
        assert fp.has_data_plane_impact is True

    def test_vm_no_data_plane(self):
        action = _make_action(resource_type="microsoft.compute/virtualmachines")
        fp = compute_fingerprint(action, None, None)
        assert fp.has_data_plane_impact is False


# ---------------------------------------------------------------------------
# compute_fingerprint — criticality and reversibility
# ---------------------------------------------------------------------------

class TestCriticalityAndReversibility:
    def test_criticality_tag_detected(self):
        action = _make_action()
        metadata = {"tags": {"criticality": "critical"}}
        fp = compute_fingerprint(action, metadata, None)
        assert fp.is_critical_resource is True

    def test_disaster_recovery_purpose_detected(self):
        action = _make_action()
        metadata = {"tags": {"purpose": "disaster-recovery"}}
        fp = compute_fingerprint(action, metadata, None)
        assert fp.is_critical_resource is True

    def test_delete_resource_is_destructive(self):
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        fp = compute_fingerprint(action, None, None)
        assert fp.change_reversibility == "destructive"

    def test_modify_nsg_is_semi_reversible(self):
        action = _make_action(action_type=ActionType.MODIFY_NSG)
        fp = compute_fingerprint(action, None, None)
        assert fp.change_reversibility == "semi-reversible"

    def test_scale_up_is_reversible(self):
        action = _make_action(action_type=ActionType.SCALE_UP)
        fp = compute_fingerprint(action, None, None)
        assert fp.change_reversibility == "reversible"


# ---------------------------------------------------------------------------
# compute_fingerprint — compliance scope
# ---------------------------------------------------------------------------

class TestComplianceScope:
    def test_no_org_context_defaults_to_in_scope(self):
        action = _make_action()
        fp = compute_fingerprint(action, None, None)
        assert fp.compliance_scope is True

    def test_empty_org_context_production_with_frameworks_in_scope(self):
        action = _make_action(resource_id="vm-prod-01")
        org = _make_org(frameworks=["HIPAA"])
        fp = compute_fingerprint(action, None, org)
        # is_production=True + frameworks → in scope
        assert fp.compliance_scope is True

    def test_dev_resource_empty_frameworks_not_in_scope(self):
        action = _make_action(resource_id="vm-dev-01")
        org = _make_org(frameworks=[])
        fp = compute_fingerprint(action, None, org)
        assert fp.compliance_scope is False

    def test_critical_rg_override_makes_in_scope(self):
        action = _make_action(resource_id="vm-dev-01", resource_group="rg-payments")
        org = _make_org(frameworks=[], critical_rgs=["rg-payments"])
        fp = compute_fingerprint(action, None, org)
        assert fp.compliance_scope is True

    def test_compliance_tag_makes_in_scope(self):
        action = _make_action(resource_id="vm-dev-01")
        org = _make_org(frameworks=[])
        metadata = {"tags": {"compliance": "PCI-DSS"}}
        fp = compute_fingerprint(action, metadata, org)
        assert fp.compliance_scope is True


# ---------------------------------------------------------------------------
# classify_tier — rule-based routing
# ---------------------------------------------------------------------------

class TestClassifyTier:
    def _fp(self, **overrides):
        """Build a minimal Tier-3-default ActionFingerprint with selective overrides."""
        from src.core.models import ActionFingerprint
        defaults = dict(
            action_type="update_config",
            resource_type="microsoft.compute/virtualmachines",
            environment="unknown",
            compliance_scope=False,
            has_network_exposure=False,
            has_data_plane_impact=False,
            is_production=False,
            is_critical_resource=False,
            estimated_blast_radius="isolated",
            change_reversibility="reversible",
        )
        defaults.update(overrides)
        return ActionFingerprint(**defaults)

    def test_rule1_network_exposure_and_compliance_is_tier3(self):
        fp = self._fp(has_network_exposure=True, compliance_scope=True, is_production=True)
        assert classify_tier(fp) == 3

    def test_rule1_network_without_compliance_does_not_trigger_rule1(self):
        # Rule 1 requires BOTH — network alone may route to a lower tier
        fp = self._fp(
            has_network_exposure=True,
            compliance_scope=False,
            is_production=False,
            estimated_blast_radius="isolated",
        )
        # Falls through to Rule 3 (non-prod + isolated)
        assert classify_tier(fp) == 1

    def test_rule2_destructive_production_critical_is_tier3(self):
        fp = self._fp(
            change_reversibility="destructive",
            is_production=True,
            is_critical_resource=True,
        )
        assert classify_tier(fp) == 3

    def test_rule2_destructive_production_non_critical_falls_through(self):
        fp = self._fp(
            change_reversibility="destructive",
            is_production=True,
            is_critical_resource=False,
            estimated_blast_radius="service",
        )
        # Not Rule 2; Rule 4: production + service + no network = Tier 2
        assert classify_tier(fp) == 2

    def test_rule3_nonprod_isolated_is_tier1(self):
        fp = self._fp(is_production=False, estimated_blast_radius="isolated")
        assert classify_tier(fp) == 1

    def test_rule3_prod_isolated_does_not_trigger_rule3(self):
        fp = self._fp(
            is_production=True,
            estimated_blast_radius="isolated",
        )
        # is_production=True blocks Rule 3; no match for Rule 4 (no "service" radius)
        # → default Tier 3
        assert classify_tier(fp) == 3

    def test_rule4_production_service_no_network_is_tier2(self):
        fp = self._fp(
            is_production=True,
            estimated_blast_radius="service",
            has_network_exposure=False,
        )
        assert classify_tier(fp) == 2

    def test_rule4_production_service_with_network_blocked_by_rule1_if_compliant(self):
        fp = self._fp(
            is_production=True,
            estimated_blast_radius="service",
            has_network_exposure=True,
            compliance_scope=True,
        )
        # Rule 1 fires first
        assert classify_tier(fp) == 3

    def test_default_unknown_env_is_tier3(self):
        fp = self._fp(
            environment="unknown",
            is_production=False,
            estimated_blast_radius="service",  # not "isolated" → Rule 3 won't fire
        )
        assert classify_tier(fp) == 3


# ---------------------------------------------------------------------------
# build_org_context
# ---------------------------------------------------------------------------

class TestBuildOrgContext:
    def test_empty_strings_produce_empty_lists(self, monkeypatch):
        monkeypatch.setattr("src.core.risk_triage.settings.org_compliance_frameworks", "")
        monkeypatch.setattr("src.core.risk_triage.settings.org_business_critical_rgs", "")
        monkeypatch.setattr("src.core.risk_triage.settings.org_name", "Contoso")
        monkeypatch.setattr("src.core.risk_triage.settings.org_resource_count", 0)
        monkeypatch.setattr("src.core.risk_triage.settings.org_risk_tolerance", "moderate")
        ctx = build_org_context()
        assert ctx.compliance_frameworks == []
        assert ctx.business_critical_rgs == []

    def test_comma_separated_frameworks_parsed(self, monkeypatch):
        monkeypatch.setattr(
            "src.core.risk_triage.settings.org_compliance_frameworks",
            "HIPAA, PCI-DSS, SOC2",
        )
        monkeypatch.setattr("src.core.risk_triage.settings.org_business_critical_rgs", "")
        monkeypatch.setattr("src.core.risk_triage.settings.org_name", "TestOrg")
        monkeypatch.setattr("src.core.risk_triage.settings.org_resource_count", 500)
        monkeypatch.setattr("src.core.risk_triage.settings.org_risk_tolerance", "conservative")
        ctx = build_org_context()
        assert ctx.compliance_frameworks == ["HIPAA", "PCI-DSS", "SOC2"]
        assert ctx.org_name == "TestOrg"
        assert ctx.resource_count == 500

    def test_critical_rgs_parsed(self, monkeypatch):
        monkeypatch.setattr("src.core.risk_triage.settings.org_compliance_frameworks", "")
        monkeypatch.setattr(
            "src.core.risk_triage.settings.org_business_critical_rgs",
            "rg-prod-payments,rg-prod-identity",
        )
        monkeypatch.setattr("src.core.risk_triage.settings.org_name", "X")
        monkeypatch.setattr("src.core.risk_triage.settings.org_resource_count", 0)
        monkeypatch.setattr("src.core.risk_triage.settings.org_risk_tolerance", "moderate")
        ctx = build_org_context()
        assert "rg-prod-payments" in ctx.business_critical_rgs
        assert "rg-prod-identity" in ctx.business_critical_rgs
