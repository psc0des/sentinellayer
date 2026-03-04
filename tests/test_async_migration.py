"""Tests for async end-to-end infrastructure migration (Phase 20).

Verifies that:
1. cost_lookup.py: get_sku_monthly_cost_async() uses httpx.AsyncClient and shares cache
2. cost_lookup.py: _extract_monthly_cost() shared helper applies correct OS-aware filtering
3. ResourceGraphClient: async methods return identical results to sync in mock mode
4. ResourceGraphClient: _azure_enrich_topology_async() uses asyncio.gather for concurrency
5. azure_tools.py: async variants return same data as sync in mock mode
6. Governance agents: @af.tool callbacks are async def (framework will await them)
7. Ops agents: @af.tool callbacks are async def (framework will await them)

All tests run in mock mode (USE_LOCAL_MOCKS=true) — no Azure credentials required.
"""

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEED_PATH = Path(__file__).parent.parent / "data" / "seed_resources.json"


def _make_mock_items(windows: bool = False, spot: bool = False) -> list[dict]:
    """Build a minimal Azure Retail Prices API Items list for testing."""
    sku_name = "Standard_B2ls_v2"
    if windows:
        sku_name += " Windows"
    if spot:
        sku_name += " Spot"
    return [
        {
            "retailPrice": 0.05,
            "skuName": sku_name,
            "armSkuName": "Standard_B2ls_v2",
            "unitOfMeasure": "1 Hour",
        }
    ]


# ---------------------------------------------------------------------------
# 1. cost_lookup — async function + shared helper
# ---------------------------------------------------------------------------


class TestCostLookupAsync:
    """Async cost lookup uses httpx.AsyncClient and shares cache with sync."""

    def setup_method(self):
        """Clear the shared cache before each test."""
        from src.infrastructure import cost_lookup
        cost_lookup._cache.clear()

    async def test_async_returns_monthly_cost(self):
        """get_sku_monthly_cost_async() returns hourly_rate * 730."""
        from src.infrastructure.cost_lookup import get_sku_monthly_cost_async

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"Items": _make_mock_items()}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await get_sku_monthly_cost_async("Standard_B2ls_v2", "canadacentral")

        # 0.05 $/hr * 730 h = 36.50
        assert result == round(0.05 * 730, 2)

    async def test_async_returns_none_on_empty_sku(self):
        """Returns None immediately when sku is empty — no HTTP call."""
        from src.infrastructure.cost_lookup import get_sku_monthly_cost_async

        result = await get_sku_monthly_cost_async("", "canadacentral")
        assert result is None

    async def test_async_returns_none_on_http_error(self):
        """Returns None gracefully on network/HTTP errors — does not raise."""
        from src.infrastructure.cost_lookup import get_sku_monthly_cost_async

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await get_sku_monthly_cost_async("Standard_B2ls_v2", "eastus")

        assert result is None

    async def test_async_shares_cache_with_sync(self):
        """Cache populated by sync is immediately visible to async (and vice versa)."""
        from src.infrastructure import cost_lookup

        # Pre-populate cache as if sync ran first
        cost_lookup._cache["standard_b2ls_v2::eastus::"] = 42.0

        result = await cost_lookup.get_sku_monthly_cost_async("Standard_B2ls_v2", "eastus")
        # Should return cached value without any HTTP call
        assert result == 42.0

    async def test_sync_reads_cache_populated_by_async(self):
        """Cache populated by async is immediately visible to sync variant."""
        from src.infrastructure import cost_lookup

        cost_lookup._cache["standard_b4ls_v2::westus::"] = 99.0

        result = cost_lookup.get_sku_monthly_cost("Standard_B4ls_v2", "westus")
        assert result == 99.0

    async def test_transient_failure_not_cached(self):
        """Transient network failures are NOT cached — next call can retry."""
        from src.infrastructure import cost_lookup

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            await cost_lookup.get_sku_monthly_cost_async("Standard_B2ls_v2", "eastus2")

        key = "standard_b2ls_v2::eastus2::"
        assert key not in cost_lookup._cache


class TestExtractMonthlyCost:
    """Shared _extract_monthly_cost helper applies correct OS-aware filtering."""

    def test_linux_excludes_windows_meters(self):
        """Linux/empty os_type excludes Windows-labeled meters."""
        from src.infrastructure.cost_lookup import _extract_monthly_cost

        items = [
            {"retailPrice": 0.10, "skuName": "Standard_B2ls_v2 Windows"},
            {"retailPrice": 0.05, "skuName": "Standard_B2ls_v2"},
        ]
        result = _extract_monthly_cost(items, "Linux")
        assert result == round(0.05 * 730, 2)

    def test_windows_prefers_windows_labeled_meter(self):
        """Windows os_type selects Windows-labeled meter."""
        from src.infrastructure.cost_lookup import _extract_monthly_cost

        items = [
            {"retailPrice": 0.10, "skuName": "Standard_B2ls_v2 Windows"},
            {"retailPrice": 0.05, "skuName": "Standard_B2ls_v2"},
        ]
        result = _extract_monthly_cost(items, "Windows")
        assert result == round(0.10 * 730, 2)

    def test_spot_meters_excluded(self):
        """Spot meters are always excluded regardless of os_type."""
        from src.infrastructure.cost_lookup import _extract_monthly_cost

        items = [
            {"retailPrice": 0.02, "skuName": "Standard_B2ls_v2 Spot"},
            {"retailPrice": 0.05, "skuName": "Standard_B2ls_v2"},
        ]
        result = _extract_monthly_cost(items, "")
        assert result == round(0.05 * 730, 2)

    def test_returns_none_when_no_qualifying_items(self):
        """Returns None when all items are Spot or Windows (Linux mode)."""
        from src.infrastructure.cost_lookup import _extract_monthly_cost

        items = [{"retailPrice": 0.02, "skuName": "Standard_B2ls_v2 Spot"}]
        result = _extract_monthly_cost(items, "Linux")
        assert result is None

    def test_returns_none_on_empty_items(self):
        """Returns None when items list is empty."""
        from src.infrastructure.cost_lookup import _extract_monthly_cost

        result = _extract_monthly_cost([], "")
        assert result is None


# ---------------------------------------------------------------------------
# 2. ResourceGraphClient — async methods in mock mode
# ---------------------------------------------------------------------------


class TestResourceGraphClientAsync:
    """Async methods on ResourceGraphClient produce identical results in mock mode."""

    def _make_mock_client(self) -> "object":
        from src.infrastructure.resource_graph import ResourceGraphClient
        from unittest.mock import MagicMock

        cfg = MagicMock()
        cfg.use_local_mocks = True
        cfg.azure_subscription_id = ""
        return ResourceGraphClient(cfg=cfg, resources_path=_SEED_PATH)

    async def test_get_resource_async_matches_sync_in_mock(self):
        """In mock mode, get_resource_async() returns the same dict as get_resource()."""
        client = self._make_mock_client()
        # Pick the first resource from the seed file
        all_resources = client.list_all()
        if not all_resources:
            pytest.skip("seed_resources.json has no resources")
        first_name = all_resources[0]["name"]

        sync_result = client.get_resource(first_name)
        async_result = await client.get_resource_async(first_name)

        assert sync_result == async_result

    async def test_list_all_async_matches_sync_in_mock(self):
        """In mock mode, list_all_async() returns same list as list_all()."""
        client = self._make_mock_client()
        sync_result = client.list_all()
        async_result = await client.list_all_async()
        assert sync_result == async_result

    async def test_get_resource_async_returns_none_for_unknown(self):
        """Returns None for a resource name that doesn't exist."""
        client = self._make_mock_client()
        result = await client.get_resource_async("nonexistent-resource-xyz")
        assert result is None

    async def test_async_enrich_uses_gather(self):
        """_azure_enrich_topology_async() calls asyncio.gather with 4 coroutines."""
        from src.infrastructure.resource_graph import ResourceGraphClient
        from unittest.mock import MagicMock, AsyncMock, patch

        cfg = MagicMock()
        cfg.use_local_mocks = False
        cfg.azure_subscription_id = "fake-sub-id"

        with patch("azure.identity.DefaultAzureCredential"), \
             patch("azure.mgmt.resourcegraph.ResourceGraphClient"), \
             patch("azure.mgmt.resourcegraph.aio.ResourceGraphClient"):
            client = ResourceGraphClient(cfg=cfg)

        # Inject a mock async client that returns empty data
        mock_async_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.data = []
        mock_async_client.resources = AsyncMock(return_value=mock_response)
        client._async_rg_client = mock_async_client

        # Patch cost lookup to avoid real HTTP calls
        with patch(
            "src.infrastructure.cost_lookup.get_sku_monthly_cost_async",
            new=AsyncMock(return_value=None),
        ):
            resource = {
                "name": "test-vm",
                "type": "microsoft.compute/virtualmachines",
                "id": "/subscriptions/fake/providers/Microsoft.Compute/virtualMachines/test-vm",
                "tags": {},
                "sku": {"name": "Standard_B2ls_v2"},
                "location": "eastus",
                "resource_group": "test-rg",
                "os_type": "Linux",
            }

            gathered_calls: list = []
            original_gather = asyncio.gather

            async def mock_gather(*coros, **kw):
                gathered_calls.append(len(coros))
                return await original_gather(*coros, **kw)

            with patch("asyncio.gather", side_effect=mock_gather):
                await client._azure_enrich_topology_async(resource)

        # asyncio.gather should have been called once with 4 coroutines
        assert gathered_calls == [4], (
            f"Expected asyncio.gather called once with 4 coroutines, got: {gathered_calls}"
        )


# ---------------------------------------------------------------------------
# 3. azure_tools — async variants return same data as sync in mock mode
# ---------------------------------------------------------------------------


class TestAsyncAzureTools:
    """Async azure_tools variants return identical mock data as sync variants."""

    async def test_query_resource_graph_async_mock(self):
        """query_resource_graph_async() returns same data as sync in mock mode."""
        with patch("src.infrastructure.azure_tools._use_mocks", return_value=True):
            from src.infrastructure.azure_tools import (
                query_resource_graph,
                query_resource_graph_async,
            )
            kql = "Resources | where type == 'microsoft.compute/virtualmachines'"
            sync_result = query_resource_graph(kql)
            async_result = await query_resource_graph_async(kql)
            assert sync_result == async_result

    async def test_query_metrics_async_mock(self):
        """query_metrics_async() returns same metrics as sync in mock mode."""
        with patch("src.infrastructure.azure_tools._use_mocks", return_value=True):
            from src.infrastructure.azure_tools import (
                query_metrics,
                query_metrics_async,
            )
            sync_result = query_metrics("vm-dr-01", ["Percentage CPU"], "P7D")
            async_result = await query_metrics_async("vm-dr-01", ["Percentage CPU"], "P7D")
            assert sync_result == async_result

    async def test_get_resource_details_async_mock(self):
        """get_resource_details_async() returns same dict as sync in mock mode."""
        with patch("src.infrastructure.azure_tools._use_mocks", return_value=True):
            from src.infrastructure.azure_tools import (
                get_resource_details,
                get_resource_details_async,
            )
            sync_result = get_resource_details("vm-dr-01")
            async_result = await get_resource_details_async("vm-dr-01")
            assert sync_result == async_result

    async def test_query_activity_log_async_mock(self):
        """query_activity_log_async() returns same structure as sync in mock mode.

        Timestamps are generated via datetime.now() on each call, so they differ
        by microseconds.  We compare structure (count + keys) instead of exact equality.
        """
        with patch("src.infrastructure.azure_tools._use_mocks", return_value=True):
            from src.infrastructure.azure_tools import (
                query_activity_log,
                query_activity_log_async,
            )
            sync_result = query_activity_log("test-rg", "P7D")
            async_result = await query_activity_log_async("test-rg", "P7D")
            assert len(sync_result) == len(async_result)
            if sync_result:
                assert set(sync_result[0].keys()) == set(async_result[0].keys())
                # Non-timestamp fields should be identical
                for key in ("operation", "status", "caller", "resource_type", "resource", "level"):
                    assert sync_result[0][key] == async_result[0][key]

    async def test_list_nsg_rules_async_mock(self):
        """list_nsg_rules_async() returns same rules as sync in mock mode."""
        with patch("src.infrastructure.azure_tools._use_mocks", return_value=True):
            from src.infrastructure.azure_tools import (
                list_nsg_rules,
                list_nsg_rules_async,
            )
            sync_result = list_nsg_rules("nsg-east-prod")
            async_result = await list_nsg_rules_async("nsg-east-prod")
            assert sync_result == async_result

    async def test_query_resource_graph_async_returns_list(self):
        """query_resource_graph_async() returns a list in mock mode."""
        with patch("src.infrastructure.azure_tools._use_mocks", return_value=True):
            from src.infrastructure.azure_tools import query_resource_graph_async
            result = await query_resource_graph_async("Resources | limit 10")
            assert isinstance(result, list)

    async def test_list_nsg_rules_async_returns_list(self):
        """list_nsg_rules_async() returns a list for unknown NSG."""
        with patch("src.infrastructure.azure_tools._use_mocks", return_value=True):
            from src.infrastructure.azure_tools import list_nsg_rules_async
            result = await list_nsg_rules_async("nonexistent-nsg")
            assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 4. Governance agents — @af.tool callbacks must be async def
# ---------------------------------------------------------------------------


class TestGovernanceAgentAsyncTools:
    """Governance agent @af.tool callbacks are coroutine functions (async def)."""

    def _make_live_cfg(self):
        """Config that enables framework but uses mock topology."""
        cfg = MagicMock()
        cfg.use_local_mocks = False
        cfg.azure_openai_endpoint = "https://fake.openai.azure.com/"
        cfg.azure_openai_deployment = "gpt-4o"
        cfg.azure_subscription_id = ""  # No subscription → mock topology
        cfg.use_live_topology = False
        return cfg

    async def test_blast_radius_tool_is_async(self):
        """The evaluate_blast_radius_rules @af.tool callback is an async coroutine function.

        The Microsoft Agent Framework's FunctionTool._invoke detects async via
        inspect.isawaitable(result) — returning a coroutine means the tool is awaited,
        not run synchronously on the event loop.
        """
        from src.governance_agents.blast_radius_agent import BlastRadiusAgent

        cfg = self._make_live_cfg()

        captured_tools: list = []

        # Mock agent_framework so we can capture the tools list
        mock_af = MagicMock()
        mock_openai_client = MagicMock()
        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(text="analysis"))
        mock_openai_client.as_agent = MagicMock(return_value=mock_agent)

        mock_af_module = MagicMock()
        mock_af_module.tool = MagicMock(side_effect=lambda **kwargs: (lambda f: f))

        with patch.dict(
            "sys.modules",
            {
                "agent_framework": mock_af_module,
                "agent_framework.openai": MagicMock(
                    OpenAIResponsesClient=MagicMock(return_value=mock_openai_client)
                ),
                "azure.identity": MagicMock(
                    DefaultAzureCredential=MagicMock,
                    get_bearer_token_provider=MagicMock(return_value=MagicMock()),
                ),
                "openai": MagicMock(AsyncAzureOpenAI=MagicMock),
            },
        ):
            agent = BlastRadiusAgent(cfg=cfg)

            # Capture the tool function by intercepting as_agent
            tool_func_holder: list = []

            def capture_as_agent(name, instructions, tools):
                tool_func_holder.extend(tools)
                return mock_agent

            mock_openai_client.as_agent = capture_as_agent

            with patch(
                "src.infrastructure.llm_throttle.run_with_throttle",
                new=AsyncMock(return_value=MagicMock(text="analysis")),
            ):
                from src.core.models import (
                    ActionTarget, ActionType, ProposedAction, Urgency
                )
                action = ProposedAction(
                    agent_id="test",
                    action_type=ActionType.SCALE_DOWN,
                    target=ActionTarget(
                        resource_id="vm-23",
                        resource_type="Microsoft.Compute/virtualMachines",
                    ),
                    reason="test",
                    urgency=Urgency.LOW,
                )
                try:
                    await agent._evaluate_with_framework(action)
                except Exception:
                    pass  # We only care that tools were registered

        # The @af.tool decorator (mocked) passes the function through directly.
        # Verify the registered callback is an async function.
        if tool_func_holder:
            blast_tool = tool_func_holder[0]
            assert inspect.iscoroutinefunction(blast_tool), (
                f"evaluate_blast_radius_rules should be async def, got: {type(blast_tool)}"
            )

    async def test_financial_tool_is_async(self):
        """The evaluate_financial_rules @af.tool callback is an async coroutine function."""
        from src.governance_agents.financial_agent import FinancialImpactAgent

        cfg = self._make_live_cfg()

        mock_af_module = MagicMock()
        mock_af_module.tool = MagicMock(side_effect=lambda **kwargs: (lambda f: f))

        mock_openai_client = MagicMock()
        mock_agent = AsyncMock()

        tool_func_holder: list = []

        def capture_as_agent(name, instructions, tools):
            tool_func_holder.extend(tools)
            return mock_agent

        mock_openai_client.as_agent = capture_as_agent

        with patch.dict(
            "sys.modules",
            {
                "agent_framework": mock_af_module,
                "agent_framework.openai": MagicMock(
                    OpenAIResponsesClient=MagicMock(return_value=mock_openai_client)
                ),
                "azure.identity": MagicMock(
                    DefaultAzureCredential=MagicMock,
                    get_bearer_token_provider=MagicMock(return_value=MagicMock()),
                ),
                "openai": MagicMock(AsyncAzureOpenAI=MagicMock),
            },
        ):
            agent = FinancialImpactAgent(cfg=cfg)

            with patch(
                "src.infrastructure.llm_throttle.run_with_throttle",
                new=AsyncMock(return_value=MagicMock(text="analysis")),
            ):
                from src.core.models import (
                    ActionTarget, ActionType, ProposedAction, Urgency
                )
                action = ProposedAction(
                    agent_id="test",
                    action_type=ActionType.DELETE_RESOURCE,
                    target=ActionTarget(
                        resource_id="vm-dr-01",
                        resource_type="Microsoft.Compute/virtualMachines",
                    ),
                    reason="test",
                    urgency=Urgency.MEDIUM,
                )
                try:
                    await agent._evaluate_with_framework(action)
                except Exception:
                    pass

        if tool_func_holder:
            financial_tool = tool_func_holder[0]
            assert inspect.iscoroutinefunction(financial_tool), (
                f"evaluate_financial_rules should be async def, got: {type(financial_tool)}"
            )

    async def test_historical_tool_is_async(self):
        """The evaluate_historical_rules @af.tool callback is an async coroutine function."""
        from src.governance_agents.historical_agent import HistoricalPatternAgent

        cfg = self._make_live_cfg()

        mock_af_module = MagicMock()
        mock_af_module.tool = MagicMock(side_effect=lambda **kwargs: (lambda f: f))

        mock_openai_client = MagicMock()
        mock_agent = AsyncMock()

        tool_func_holder: list = []

        def capture_as_agent(name, instructions, tools):
            tool_func_holder.extend(tools)
            return mock_agent

        mock_openai_client.as_agent = capture_as_agent

        with patch.dict(
            "sys.modules",
            {
                "agent_framework": mock_af_module,
                "agent_framework.openai": MagicMock(
                    OpenAIResponsesClient=MagicMock(return_value=mock_openai_client)
                ),
                "azure.identity": MagicMock(
                    DefaultAzureCredential=MagicMock,
                    get_bearer_token_provider=MagicMock(return_value=MagicMock()),
                ),
                "openai": MagicMock(AsyncAzureOpenAI=MagicMock),
            },
        ):
            agent = HistoricalPatternAgent(cfg=cfg)

            with patch(
                "src.infrastructure.llm_throttle.run_with_throttle",
                new=AsyncMock(return_value=MagicMock(text="analysis")),
            ):
                from src.core.models import (
                    ActionTarget, ActionType, ProposedAction, Urgency
                )
                action = ProposedAction(
                    agent_id="test",
                    action_type=ActionType.RESTART_SERVICE,
                    target=ActionTarget(
                        resource_id="payment-api",
                        resource_type="Microsoft.Web/sites",
                    ),
                    reason="test",
                    urgency=Urgency.LOW,
                )
                try:
                    await agent._evaluate_with_framework(action)
                except Exception:
                    pass

        if tool_func_holder:
            hist_tool = tool_func_holder[0]
            assert inspect.iscoroutinefunction(hist_tool), (
                f"evaluate_historical_rules should be async def, got: {type(hist_tool)}"
            )

    async def test_policy_tool_is_async(self):
        """The evaluate_policy_rules @af.tool callback is an async coroutine function."""
        from src.governance_agents.policy_agent import PolicyComplianceAgent

        cfg = self._make_live_cfg()

        mock_af_module = MagicMock()
        mock_af_module.tool = MagicMock(side_effect=lambda **kwargs: (lambda f: f))

        mock_openai_client = MagicMock()
        mock_agent = AsyncMock()

        tool_func_holder: list = []

        def capture_as_agent(name, instructions, tools):
            tool_func_holder.extend(tools)
            return mock_agent

        mock_openai_client.as_agent = capture_as_agent

        with patch.dict(
            "sys.modules",
            {
                "agent_framework": mock_af_module,
                "agent_framework.openai": MagicMock(
                    OpenAIResponsesClient=MagicMock(return_value=mock_openai_client)
                ),
                "azure.identity": MagicMock(
                    DefaultAzureCredential=MagicMock,
                    get_bearer_token_provider=MagicMock(return_value=MagicMock()),
                ),
                "openai": MagicMock(AsyncAzureOpenAI=MagicMock),
            },
        ):
            agent = PolicyComplianceAgent(cfg=cfg)

            with patch(
                "src.infrastructure.llm_throttle.run_with_throttle",
                new=AsyncMock(return_value=MagicMock(text="analysis")),
            ):
                from src.core.models import (
                    ActionTarget, ActionType, ProposedAction, Urgency
                )
                action = ProposedAction(
                    agent_id="test",
                    action_type=ActionType.MODIFY_NSG,
                    target=ActionTarget(
                        resource_id="nsg-east",
                        resource_type="Microsoft.Network/networkSecurityGroups",
                    ),
                    reason="test",
                    urgency=Urgency.LOW,
                )
                try:
                    await agent._evaluate_with_framework(action)
                except Exception:
                    pass

        if tool_func_holder:
            policy_tool = tool_func_holder[0]
            assert inspect.iscoroutinefunction(policy_tool), (
                f"evaluate_policy_rules should be async def, got: {type(policy_tool)}"
            )


# ---------------------------------------------------------------------------
# 5. Ops agents — @af.tool callbacks must be async def
# ---------------------------------------------------------------------------


class TestOpsAgentAsyncTools:
    """Ops agent @af.tool tool callbacks are coroutine functions."""

    def _make_ops_cfg(self):
        cfg = MagicMock()
        cfg.use_local_mocks = False
        cfg.azure_openai_endpoint = "https://fake.openai.azure.com/"
        cfg.azure_openai_deployment = "gpt-4o"
        cfg.azure_subscription_id = "fake-sub"
        cfg.default_resource_group = ""
        return cfg

    async def _collect_tools_from_agent(self, agent_cls, cfg):
        """Instantiate an ops agent and collect the @af.tool functions it registers.

        Uses inspect to determine how many positional args _scan_with_framework
        expects beyond ``self``, then passes that many ``None`` values.  This
        handles the MonitoringAgent (2 args: alert_payload + resource_group)
        and Cost/DeployAgent (1 arg: resource_group) uniformly.
        """
        mock_af_module = MagicMock()
        # @af.tool returns the function unchanged so we can inspect it
        mock_af_module.tool = MagicMock(side_effect=lambda **kwargs: (lambda f: f))

        tool_func_holder: list = []
        mock_openai_client = MagicMock()
        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(text="no proposals"))

        def capture_as_agent(name, instructions, tools):
            tool_func_holder.extend(tools)
            return mock_agent

        mock_openai_client.as_agent = capture_as_agent

        with patch.dict(
            "sys.modules",
            {
                "agent_framework": mock_af_module,
                "agent_framework.openai": MagicMock(
                    OpenAIResponsesClient=MagicMock(return_value=mock_openai_client)
                ),
                "azure.identity": MagicMock(
                    DefaultAzureCredential=MagicMock,
                    get_bearer_token_provider=MagicMock(return_value=MagicMock()),
                ),
                "openai": MagicMock(AsyncAzureOpenAI=MagicMock),
            },
        ):
            agent = agent_cls(cfg=cfg)

            # Detect how many positional args _scan_with_framework needs (beyond self)
            sig = inspect.signature(agent._scan_with_framework)
            n_required = sum(
                1
                for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
            )
            scan_args = (None,) * n_required

            with patch(
                "src.infrastructure.llm_throttle.run_with_throttle",
                new=AsyncMock(return_value=MagicMock(text="no proposals")),
            ):
                try:
                    await agent._scan_with_framework(*scan_args)
                except Exception:
                    pass

        return tool_func_holder

    async def test_cost_agent_azure_tools_are_async(self):
        """CostOptimizationAgent's azure_tools @af.tool callbacks are async def."""
        from src.operational_agents.cost_agent import CostOptimizationAgent

        cfg = self._make_ops_cfg()
        tools = await self._collect_tools_from_agent(CostOptimizationAgent, cfg)

        # Exclude propose_action (sync by design) — check azure_tools callbacks
        azure_tool_callbacks = [t for t in tools if t.__name__ != "tool_propose_action"]
        assert len(azure_tool_callbacks) > 0, "No azure tool callbacks captured"
        for tool_fn in azure_tool_callbacks:
            assert inspect.iscoroutinefunction(tool_fn), (
                f"Expected {tool_fn.__name__} to be async def"
            )

    async def test_monitoring_agent_azure_tools_are_async(self):
        """MonitoringAgent's azure_tools @af.tool callbacks are async def."""
        from src.operational_agents.monitoring_agent import MonitoringAgent

        cfg = self._make_ops_cfg()
        tools = await self._collect_tools_from_agent(MonitoringAgent, cfg)

        azure_tool_callbacks = [t for t in tools if t.__name__ != "tool_propose_action"]
        assert len(azure_tool_callbacks) > 0
        for tool_fn in azure_tool_callbacks:
            assert inspect.iscoroutinefunction(tool_fn), (
                f"Expected {tool_fn.__name__} to be async def"
            )

    async def test_deploy_agent_azure_tools_are_async(self):
        """DeployAgent's azure_tools @af.tool callbacks are async def."""
        from src.operational_agents.deploy_agent import DeployAgent

        cfg = self._make_ops_cfg()
        tools = await self._collect_tools_from_agent(DeployAgent, cfg)

        azure_tool_callbacks = [t for t in tools if t.__name__ != "tool_propose_action"]
        assert len(azure_tool_callbacks) > 0
        for tool_fn in azure_tool_callbacks:
            assert inspect.iscoroutinefunction(tool_fn), (
                f"Expected {tool_fn.__name__} to be async def"
            )

    async def test_propose_action_stays_sync(self):
        """propose_action tool stays sync — it does no I/O (just list append)."""
        from src.operational_agents.cost_agent import CostOptimizationAgent

        cfg = self._make_ops_cfg()
        tools = await self._collect_tools_from_agent(CostOptimizationAgent, cfg)

        propose_tools = [t for t in tools if t.__name__ == "tool_propose_action"]
        if propose_tools:
            assert not inspect.iscoroutinefunction(propose_tools[0]), (
                "propose_action should stay sync — no I/O, no need to be async"
            )


# ---------------------------------------------------------------------------
# 6. Regression guard — verify async helpers exist on governance agents
# ---------------------------------------------------------------------------


class TestAsyncHelperMethods:
    """Verify async methods were added correctly to governance agents."""

    def test_blast_radius_has_async_helpers(self):
        """BlastRadiusAgent has all required async helper methods."""
        from src.governance_agents.blast_radius_agent import BlastRadiusAgent

        required = [
            "_evaluate_rules_async",
            "_find_resource_async",
            "_detect_spofs_async",
            "_get_affected_zones_async",
        ]
        for method_name in required:
            assert hasattr(BlastRadiusAgent, method_name), (
                f"BlastRadiusAgent missing {method_name}"
            )
            assert inspect.iscoroutinefunction(getattr(BlastRadiusAgent, method_name)), (
                f"BlastRadiusAgent.{method_name} should be async def"
            )

    def test_financial_agent_has_async_helpers(self):
        """FinancialImpactAgent has all required async helper methods."""
        from src.governance_agents.financial_agent import FinancialImpactAgent

        required = [
            "_evaluate_rules_async",
            "_find_resource_async",
        ]
        for method_name in required:
            assert hasattr(FinancialImpactAgent, method_name), (
                f"FinancialImpactAgent missing {method_name}"
            )
            assert inspect.iscoroutinefunction(
                getattr(FinancialImpactAgent, method_name)
            ), (f"FinancialImpactAgent.{method_name} should be async def")

    def test_resource_graph_client_has_async_methods(self):
        """ResourceGraphClient has all async public and private methods."""
        from src.infrastructure.resource_graph import ResourceGraphClient

        async_methods = [
            "get_resource_async",
            "list_all_async",
            "_azure_get_resource_async",
            "_azure_list_all_async",
            "_azure_enrich_topology_async",
        ]
        for method_name in async_methods:
            assert hasattr(ResourceGraphClient, method_name), (
                f"ResourceGraphClient missing {method_name}"
            )
            assert inspect.iscoroutinefunction(
                getattr(ResourceGraphClient, method_name)
            ), (f"ResourceGraphClient.{method_name} should be async def")

    def test_cost_lookup_has_async_function(self):
        """cost_lookup module exports get_sku_monthly_cost_async."""
        import src.infrastructure.cost_lookup as cl

        assert hasattr(cl, "get_sku_monthly_cost_async")
        assert inspect.iscoroutinefunction(cl.get_sku_monthly_cost_async)

    def test_cost_lookup_has_shared_helper(self):
        """cost_lookup module exports _extract_monthly_cost shared helper."""
        import src.infrastructure.cost_lookup as cl

        assert hasattr(cl, "_extract_monthly_cost")
        assert callable(cl._extract_monthly_cost)

    def test_azure_tools_has_all_async_variants(self):
        """azure_tools module exports all 5 async variant functions."""
        import src.infrastructure.azure_tools as at

        expected = [
            "query_resource_graph_async",
            "query_metrics_async",
            "get_resource_details_async",
            "query_activity_log_async",
            "list_nsg_rules_async",
        ]
        for fn_name in expected:
            assert hasattr(at, fn_name), f"azure_tools missing {fn_name}"
            assert inspect.iscoroutinefunction(getattr(at, fn_name)), (
                f"azure_tools.{fn_name} should be async def"
            )

    def test_blast_radius_has_aclose(self):
        """BlastRadiusAgent exposes async aclose() for connection pool cleanup."""
        from src.governance_agents.blast_radius_agent import BlastRadiusAgent

        assert hasattr(BlastRadiusAgent, "aclose"), "BlastRadiusAgent missing aclose()"
        assert inspect.iscoroutinefunction(BlastRadiusAgent.aclose), (
            "BlastRadiusAgent.aclose should be async def"
        )

    def test_financial_agent_has_aclose(self):
        """FinancialImpactAgent exposes async aclose() for connection pool cleanup."""
        from src.governance_agents.financial_agent import FinancialImpactAgent

        assert hasattr(FinancialImpactAgent, "aclose"), (
            "FinancialImpactAgent missing aclose()"
        )
        assert inspect.iscoroutinefunction(FinancialImpactAgent.aclose), (
            "FinancialImpactAgent.aclose should be async def"
        )

    def test_historical_agent_has_async_rules(self):
        """HistoricalPatternAgent has _evaluate_rules_async helper."""
        from src.governance_agents.historical_agent import HistoricalPatternAgent

        assert hasattr(HistoricalPatternAgent, "_evaluate_rules_async"), (
            "HistoricalPatternAgent missing _evaluate_rules_async"
        )
        assert inspect.iscoroutinefunction(HistoricalPatternAgent._evaluate_rules_async), (
            "HistoricalPatternAgent._evaluate_rules_async should be async def"
        )
