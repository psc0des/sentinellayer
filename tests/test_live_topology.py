"""Tests for Phase 19: live Azure topology in governance agents.

All tests mock the Azure SDK calls so no real Azure credentials are required.
Mock mode (USE_LOCAL_MOCKS=true) is exercised by the pre-existing test suites
(test_blast_radius.py, test_financial_impact.py).  This file focuses on the
NEW live-mode code paths added in Phase 19.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_action(
    resource_id: str,
    action_type: ActionType = ActionType.DELETE_RESOURCE,
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type="Microsoft.Compute/virtualMachines",
        ),
        reason="test",
        urgency=Urgency.LOW,
    )


def _make_live_cfg(*, mocks: bool = False, sub: str = "sub-123", endpoint: str = ""):
    """Build a minimal settings-like object for live-mode testing."""
    cfg = MagicMock()
    cfg.use_local_mocks = mocks
    cfg.azure_subscription_id = sub
    cfg.azure_openai_endpoint = endpoint
    cfg.azure_openai_deployment = "gpt-4o"
    return cfg


# ---------------------------------------------------------------------------
# TestCostLookup — unit tests for cost_lookup.get_sku_monthly_cost()
# ---------------------------------------------------------------------------


class TestCostLookup:
    """Unit tests for the Azure Retail Prices API wrapper."""

    def setup_method(self):
        """Clear the module-level cache before each test."""
        import src.infrastructure.cost_lookup as cl
        cl._cache.clear()

    def test_returns_none_for_empty_sku(self):
        from src.infrastructure.cost_lookup import get_sku_monthly_cost
        assert get_sku_monthly_cost("", "canadacentral") is None

    def test_returns_none_for_empty_location(self):
        from src.infrastructure.cost_lookup import get_sku_monthly_cost
        assert get_sku_monthly_cost("Standard_B2ls_v2", "") is None

    def test_returns_none_on_http_error(self):
        """API failure must silently return None — governance must never crash."""
        import httpx
        from src.infrastructure.cost_lookup import get_sku_monthly_cost

        with patch("httpx.get", side_effect=httpx.ConnectError("timeout")):
            result = get_sku_monthly_cost("Standard_B2ls_v2", "canadacentral")
        assert result is None

    def test_calculates_monthly_from_hourly(self):
        """Monthly cost = min(retailPrice) × 730 hours."""
        from src.infrastructure.cost_lookup import get_sku_monthly_cost

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "Items": [
                {"retailPrice": 0.05, "armSkuName": "Standard_B2ls_v2"},
                {"retailPrice": 0.10, "armSkuName": "Standard_B2ls_v2"},  # higher — ignored
            ]
        }
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.get", return_value=mock_resp):
            result = get_sku_monthly_cost("Standard_B2ls_v2", "canadacentral")

        # 0.05 (cheapest) × 730 = 36.50
        assert result == pytest.approx(36.50, rel=1e-3)

    def test_caches_result(self):
        """Second call must not hit the network — uses in-memory cache."""
        from src.infrastructure.cost_lookup import get_sku_monthly_cost

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"Items": [{"retailPrice": 0.05}]}
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            get_sku_monthly_cost("Standard_B2ls_v2", "canadacentral")
            get_sku_monthly_cost("Standard_B2ls_v2", "canadacentral")  # second call

        # httpx.get should only have been called once
        assert mock_get.call_count == 1

    def test_caches_none_on_empty_items(self):
        """No matching items → None cached, no repeated API calls."""
        from src.infrastructure.cost_lookup import get_sku_monthly_cost

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"Items": []}
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            r1 = get_sku_monthly_cost("Unknown_SKU", "canadacentral")
            r2 = get_sku_monthly_cost("Unknown_SKU", "canadacentral")

        assert r1 is None
        assert r2 is None
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# TestResourceGraphLiveEnrichment
# ---------------------------------------------------------------------------


class TestResourceGraphLiveEnrichment:
    """ResourceGraphClient enriches live Azure resources with topology fields.

    Strategy: create the client in mock mode (which avoids the Azure SDK
    entirely), then manually set ``_is_mock = False`` and inject a mock
    ``_rg_client``.  This lets us test ``_azure_enrich_topology()`` directly
    without fighting the Azure SDK import machinery.
    """

    def _make_live_rg_client(self, azure_sdk_mock: MagicMock):
        """Return a ResourceGraphClient wired for live mode with a fake Azure client."""
        from src.infrastructure.resource_graph import ResourceGraphClient

        # Start in mock mode — no Azure SDK needed.
        client = ResourceGraphClient(cfg=_make_live_cfg(mocks=True))
        # Switch to live mode by injecting the mock Azure SDK client.
        client._is_mock = False
        client._rg_client = azure_sdk_mock
        return client

    def _enrich(self, client, resource: dict) -> dict:
        """Call _azure_enrich_topology() with a fake QueryRequest import."""
        class _FakeQueryRequest:
            def __init__(self, subscriptions=None, query=""):
                self.subscriptions = subscriptions or []
                self.query = query

        with patch(
            "src.infrastructure.resource_graph.QueryRequest",
            _FakeQueryRequest,
            create=True,
        ):
            # Patch the in-method import of QueryRequest
            import sys
            original = sys.modules.get("azure.mgmt.resourcegraph.models")
            fake_models = MagicMock()
            fake_models.QueryRequest = _FakeQueryRequest
            sys.modules["azure.mgmt.resourcegraph.models"] = fake_models
            try:
                return client._azure_enrich_topology(resource)
            finally:
                if original is None:
                    sys.modules.pop("azure.mgmt.resourcegraph.models", None)
                else:
                    sys.modules["azure.mgmt.resourcegraph.models"] = original

    def _resp(self, rows: list[dict]) -> MagicMock:
        r = MagicMock()
        r.data = rows
        return r

    def test_depends_on_tag_parsed_to_dependencies(self):
        """A 'depends-on' tag value must be split into the dependencies list."""
        azure_sdk = MagicMock()
        # KQL calls: NSG join (VM type) → empty, reverse lookup → empty
        azure_sdk.resources.side_effect = [
            self._resp([]),  # VM NSG join
            self._resp([]),  # reverse lookup
        ]

        resource = {
            "id": "/sub/rg/vm/vm-dr-01",
            "name": "vm-dr-01",
            "type": "Microsoft.Compute/virtualMachines",
            "location": "canadacentral",
            "tags": {"depends-on": "ruriskryprod01,nsg-east-prod"},
            "sku": {},
            "resource_group": "ruriskry-prod-rg",
        }

        client = self._make_live_rg_client(azure_sdk)
        with patch("src.infrastructure.cost_lookup.get_sku_monthly_cost", return_value=None):
            result = self._enrich(client, resource)

        assert "ruriskryprod01" in result["dependencies"]
        assert "nsg-east-prod" in result["dependencies"]

    def test_governs_tag_parsed_to_governs_list(self):
        """A 'governs' tag must be split into the governs list."""
        azure_sdk = MagicMock()
        # KQL calls: NSG-governs (NIC join) → empty, reverse lookup → empty
        azure_sdk.resources.side_effect = [
            self._resp([]),  # NSG NIC join
            self._resp([]),  # reverse lookup
        ]

        resource = {
            "id": "/sub/rg/nsg/nsg-east-prod",
            "name": "nsg-east-prod",
            "type": "Microsoft.Network/networkSecurityGroups",
            "location": "canadacentral",
            "tags": {"governs": "vm-dr-01,vm-web-01"},
            "sku": {},
            "resource_group": "ruriskry-prod-rg",
        }

        client = self._make_live_rg_client(azure_sdk)
        with patch("src.infrastructure.cost_lookup.get_sku_monthly_cost", return_value=None):
            result = self._enrich(client, resource)

        assert "vm-dr-01" in result["governs"]
        assert "vm-web-01" in result["governs"]

    def test_reverse_lookup_adds_dependents(self):
        """Resources that tag 'depends-on: {name}' must appear in dependents."""
        azure_sdk = MagicMock()
        # Storage is not a VM or NSG → only 1 KQL call: reverse lookup
        azure_sdk.resources.return_value = self._resp([
            {"name": "vm-dr-01"},
            {"name": "vm-web-01"},
        ])

        resource = {
            "id": "/sub/rg/storage/ruriskryprod01",
            "name": "ruriskryprod01",
            "type": "Microsoft.Storage/storageAccounts",
            "location": "canadacentral",
            "tags": {},
            "sku": {"name": "Standard_LRS"},
            "resource_group": "ruriskry-prod-rg",
        }

        client = self._make_live_rg_client(azure_sdk)
        with patch("src.infrastructure.cost_lookup.get_sku_monthly_cost", return_value=42.0):
            result = self._enrich(client, resource)

        assert "vm-dr-01" in result["dependents"]
        assert "vm-web-01" in result["dependents"]
        assert result["monthly_cost"] == 42.0

    def test_topology_fields_present_even_on_empty_kql_results(self):
        """Even when all KQL queries return nothing, all topology keys exist."""
        azure_sdk = MagicMock()
        azure_sdk.resources.return_value = self._resp([])

        resource = {
            "id": "/sub/rg/vm/vm-x",
            "name": "vm-x",
            "type": "Microsoft.Compute/virtualMachines",
            "location": "eastus",
            "tags": {},
            "sku": {},
            "resource_group": "my-rg",
        }

        client = self._make_live_rg_client(azure_sdk)
        with patch("src.infrastructure.cost_lookup.get_sku_monthly_cost", return_value=None):
            result = self._enrich(client, resource)

        for key in ("dependencies", "dependents", "governs", "services_hosted", "consumers"):
            assert key in result, f"Missing topology key: {key}"


# ---------------------------------------------------------------------------
# TestBlastRadiusAgentLiveMode
# ---------------------------------------------------------------------------


class TestBlastRadiusAgentLiveMode:
    """BlastRadiusAgent uses ResourceGraphClient in live mode, not JSON."""

    def test_live_mode_sets_rg_client_not_none(self):
        """When USE_LOCAL_MOCKS=false + subscription_id set, _rg_client must exist."""
        from src.governance_agents.blast_radius_agent import BlastRadiusAgent

        cfg = _make_live_cfg()
        mock_rg = MagicMock()

        # The import is inside __init__: "from src.infrastructure.resource_graph
        # import ResourceGraphClient" — patch at the source module.
        with patch(
            "src.infrastructure.resource_graph.ResourceGraphClient",
            return_value=mock_rg,
        ):
            agent = BlastRadiusAgent(cfg=cfg)

        assert agent._rg_client is mock_rg
        assert agent._resources == {}
        assert agent._edges == []

    def test_mock_mode_keeps_json_loading(self):
        """When USE_LOCAL_MOCKS=true, _rg_client must stay None (JSON path)."""
        from src.governance_agents.blast_radius_agent import BlastRadiusAgent

        cfg = _make_live_cfg(mocks=True)
        agent = BlastRadiusAgent(cfg=cfg)

        assert agent._rg_client is None
        assert len(agent._resources) > 0  # JSON was loaded

    async def test_live_find_resource_calls_rg_client(self):
        """_find_resource() in live mode must delegate to _rg_client.get_resource()."""
        from src.governance_agents.blast_radius_agent import BlastRadiusAgent

        cfg = _make_live_cfg()
        mock_rg = MagicMock()
        mock_rg.get_resource.return_value = {
            "name": "vm-dr-01",
            "type": "Microsoft.Compute/virtualMachines",
            "tags": {"criticality": "high", "disaster-recovery": "true"},
            "dependencies": ["nsg-east-prod"],
            "dependents": [],
            "governs": [],
            "services_hosted": [],
            "consumers": [],
            "monthly_cost": 36.50,
            "location": "canadacentral",
        }

        with patch(
            "src.infrastructure.resource_graph.ResourceGraphClient",
            return_value=mock_rg,
        ):
            agent = BlastRadiusAgent(cfg=cfg)

        action = _make_action("vm-dr-01", ActionType.DELETE_RESOURCE)
        result = await agent.evaluate(action)

        mock_rg.get_resource.assert_called()
        assert result.sri_infrastructure > 0
        assert "vm-dr-01" in result.affected_resources or result.sri_infrastructure > 40


# ---------------------------------------------------------------------------
# TestFinancialAgentLiveMode
# ---------------------------------------------------------------------------


class TestFinancialAgentLiveMode:
    """FinancialImpactAgent uses live cost data from ResourceGraphClient."""

    def test_live_mode_sets_rg_client_not_none(self):
        """When USE_LOCAL_MOCKS=false + subscription_id set, _rg_client must exist."""
        from src.governance_agents.financial_agent import FinancialImpactAgent

        cfg = _make_live_cfg()
        mock_rg = MagicMock()

        with patch(
            "src.infrastructure.resource_graph.ResourceGraphClient",
            return_value=mock_rg,
        ):
            agent = FinancialImpactAgent(cfg=cfg)

        assert agent._rg_client is mock_rg
        assert agent._resources == {}

    def test_mock_mode_keeps_json_loading(self):
        """When USE_LOCAL_MOCKS=true, _rg_client must stay None."""
        from src.governance_agents.financial_agent import FinancialImpactAgent

        cfg = _make_live_cfg(mocks=True)
        agent = FinancialImpactAgent(cfg=cfg)

        assert agent._rg_client is None
        assert len(agent._resources) > 0

    async def test_live_monthly_cost_from_rg_client(self):
        """FinancialImpactAgent uses monthly_cost returned by ResourceGraphClient."""
        from src.governance_agents.financial_agent import FinancialImpactAgent

        cfg = _make_live_cfg()
        mock_rg = MagicMock()
        mock_rg.get_resource.return_value = {
            "name": "vm-dr-01",
            "type": "Microsoft.Compute/virtualMachines",
            "tags": {},
            "dependencies": [],
            "dependents": [],
            "governs": [],
            "services_hosted": [],
            "consumers": [],
            "monthly_cost": 36.50,  # from Azure Retail Prices API
            "location": "canadacentral",
        }

        with patch(
            "src.infrastructure.resource_graph.ResourceGraphClient",
            return_value=mock_rg,
        ):
            agent = FinancialImpactAgent(cfg=cfg)

        action = _make_action("vm-dr-01", ActionType.DELETE_RESOURCE)
        result = await agent.evaluate(action)

        mock_rg.get_resource.assert_called()
        # DELETE of a $36.50/month VM should yield savings of -$36.50
        assert result.immediate_monthly_change == pytest.approx(-36.50, abs=0.01)
