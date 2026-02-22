"""Tests for all four Azure infrastructure clients — mock mode only.

These tests verify:
1. Each client correctly enters mock mode when credentials are absent.
2. Each client's mock implementation returns the right shapes and data.
3. Error paths are handled gracefully.

No Azure connection is needed — all tests use local JSON files
(data/seed_incidents.json, data/seed_resources.json) or tmp_path fixtures.

Why test mock mode?
-------------------
Mock mode is what runs in CI, in every developer's local environment,
and in every test run before Azure infrastructure is provisioned.
If the mock logic is broken, the whole project breaks.
"""

import json
from pathlib import Path

import pytest

from src.infrastructure.cosmos_client import CosmosDecisionClient
from src.infrastructure.openai_client import AzureOpenAIClient
from src.infrastructure.resource_graph import ResourceGraphClient
from src.infrastructure.search_client import AzureSearchClient


# ---------------------------------------------------------------------------
# Minimal settings objects that force mock mode
# ---------------------------------------------------------------------------


class _MockSettings:
    """Fake Settings with all credentials empty → forces every client to mock mode."""

    use_local_mocks = True
    azure_openai_endpoint = ""
    azure_openai_api_key = ""
    azure_openai_api_key_secret_name = "foundry-primary-key"
    azure_openai_deployment = "gpt-4o"
    azure_openai_api_version = "2024-12-01-preview"
    azure_search_endpoint = ""
    azure_search_api_key = ""
    azure_search_api_key_secret_name = "search-primary-key"
    azure_search_index = "incident-history"
    cosmos_endpoint = ""
    cosmos_key = ""
    cosmos_key_secret_name = "cosmos-primary-key"
    cosmos_database = "sentinellayer"
    cosmos_container_decisions = "governance-decisions"
    azure_subscription_id = ""
    azure_tenant_id = ""
    azure_keyvault_url = ""
    azure_managed_identity_client_id = ""


class _AzureSettings(_MockSettings):
    """Fake Settings with use_local_mocks=False but STILL no real credentials.

    Used to test the fallback logic: even when USE_LOCAL_MOCKS is False,
    a missing endpoint makes the client silently revert to mock mode.
    """

    use_local_mocks = False


class _EndpointNoKeySettings(_MockSettings):
    """Endpoints set, but keys unavailable and no Key Vault configured."""

    use_local_mocks = False
    azure_openai_endpoint = "https://demo-foundry.cognitiveservices.azure.com/"
    azure_search_endpoint = "https://demo-search.search.windows.net"
    cosmos_endpoint = "https://demo-cosmos.documents.azure.com:443/"
    azure_keyvault_url = ""


# ===========================================================================
# AzureOpenAIClient
# ===========================================================================


class TestAzureOpenAIClient:
    """Tests for the OpenAI client wrapper."""

    def test_is_mock_when_use_local_mocks_true(self):
        client = AzureOpenAIClient(cfg=_MockSettings())
        assert client.is_mock is True

    def test_is_mock_when_endpoint_missing_even_if_flag_false(self):
        """Missing endpoint → fallback to mock regardless of the flag."""
        client = AzureOpenAIClient(cfg=_AzureSettings())
        assert client.is_mock is True

    def test_complete_returns_string(self):
        client = AzureOpenAIClient(cfg=_MockSettings())
        result = client.complete("You are a risk assessor.", "Why is this risky?")
        assert isinstance(result, str)

    def test_complete_returns_non_empty_string(self):
        client = AzureOpenAIClient(cfg=_MockSettings())
        result = client.complete("system", "user")
        assert len(result) > 0

    def test_mock_response_contains_mock_label(self):
        """The canned response must clearly identify itself as a mock."""
        client = AzureOpenAIClient(cfg=_MockSettings())
        result = client.complete("system", "user")
        assert "MOCK" in result

    def test_different_prompts_return_same_mock_string(self):
        """Mock mode ignores prompt content — same response every time."""
        client = AzureOpenAIClient(cfg=_MockSettings())
        r1 = client.complete("prompt A", "question A")
        r2 = client.complete("prompt B", "question B")
        assert r1 == r2

    def test_max_tokens_param_accepted(self):
        """Extra kwargs must not crash in mock mode."""
        client = AzureOpenAIClient(cfg=_MockSettings())
        result = client.complete("system", "user", max_tokens=100)
        assert isinstance(result, str)

    def test_temperature_param_accepted(self):
        client = AzureOpenAIClient(cfg=_MockSettings())
        result = client.complete("system", "user", temperature=0.0)
        assert isinstance(result, str)

    def test_is_mock_when_endpoint_exists_but_no_key_anywhere(self):
        client = AzureOpenAIClient(cfg=_EndpointNoKeySettings())
        assert client.is_mock is True


# ===========================================================================
# CosmosDecisionClient
# ===========================================================================


class TestCosmosDecisionClient:
    """Tests for the Cosmos DB / local JSON decision store."""

    def _client(self, tmp_path: Path) -> CosmosDecisionClient:
        return CosmosDecisionClient(
            cfg=_MockSettings(), decisions_dir=tmp_path / "decisions"
        )

    def test_is_mock_when_use_local_mocks_true(self, tmp_path):
        assert self._client(tmp_path).is_mock is True

    def test_is_mock_when_endpoint_missing_even_if_flag_false(self, tmp_path):
        client = CosmosDecisionClient(
            cfg=_AzureSettings(), decisions_dir=tmp_path / "decisions"
        )
        assert client.is_mock is True

    def test_is_mock_when_endpoint_exists_but_no_key_anywhere(self, tmp_path):
        client = CosmosDecisionClient(
            cfg=_EndpointNoKeySettings(), decisions_dir=tmp_path / "decisions"
        )
        assert client.is_mock is True

    def test_decisions_dir_created_on_init(self, tmp_path):
        client = self._client(tmp_path)
        assert client._decisions_dir.exists()

    # --- upsert ---

    def test_upsert_creates_json_file(self, tmp_path):
        client = self._client(tmp_path)
        client.upsert({"id": "action-001", "resource_id": "vm-23"})
        assert (client._decisions_dir / "action-001.json").exists()

    def test_upsert_writes_valid_json(self, tmp_path):
        client = self._client(tmp_path)
        client.upsert({"id": "action-002", "resource_id": "nsg-east", "decision": "denied"})
        data = json.loads((client._decisions_dir / "action-002.json").read_text())
        assert data["decision"] == "denied"

    def test_upsert_overwrites_existing_record(self, tmp_path):
        """Second upsert with the same id replaces the first (idempotent)."""
        client = self._client(tmp_path)
        client.upsert({"id": "action-003", "resource_id": "vm-23", "decision": "approved"})
        client.upsert({"id": "action-003", "resource_id": "vm-23", "decision": "denied"})
        data = json.loads((client._decisions_dir / "action-003.json").read_text())
        assert data["decision"] == "denied"

    # --- get_recent ---

    def test_get_recent_returns_list(self, tmp_path):
        assert isinstance(self._client(tmp_path).get_recent(), list)

    def test_get_recent_empty_when_no_records(self, tmp_path):
        assert self._client(tmp_path).get_recent() == []

    def test_get_recent_returns_inserted_record(self, tmp_path):
        client = self._client(tmp_path)
        record = {
            "id": "r1",
            "resource_id": "vm-1",
            "timestamp": "2026-02-20T12:00:00Z",
        }
        client.upsert(record)
        results = client.get_recent()
        assert len(results) == 1
        assert results[0]["id"] == "r1"

    def test_get_recent_respects_limit(self, tmp_path):
        client = self._client(tmp_path)
        for i in range(5):
            client.upsert({"id": f"rec-{i}", "resource_id": "vm-1",
                           "timestamp": f"2026-02-20T1{i}:00:00Z"})
        results = client.get_recent(limit=3)
        assert len(results) == 3

    def test_get_recent_newest_first(self, tmp_path):
        client = self._client(tmp_path)
        client.upsert({"id": "old", "resource_id": "vm-1", "timestamp": "2026-01-01T00:00:00Z"})
        client.upsert({"id": "new", "resource_id": "vm-1", "timestamp": "2026-02-20T00:00:00Z"})
        results = client.get_recent()
        assert results[0]["id"] == "new"

    # --- get_by_resource ---

    def test_get_by_resource_returns_list(self, tmp_path):
        assert isinstance(self._client(tmp_path).get_by_resource("vm-23"), list)

    def test_get_by_resource_filters_by_partial_match(self, tmp_path):
        client = self._client(tmp_path)
        client.upsert({"id": "a", "resource_id": "vm-23", "timestamp": "2026-01-01T00:00:00Z"})
        client.upsert({"id": "b", "resource_id": "nsg-east", "timestamp": "2026-01-01T00:00:00Z"})
        results = client.get_by_resource("vm-23")
        assert len(results) == 1
        assert results[0]["resource_id"] == "vm-23"

    def test_get_by_resource_returns_empty_for_no_match(self, tmp_path):
        client = self._client(tmp_path)
        client.upsert({"id": "x", "resource_id": "vm-23", "timestamp": "2026-01-01T00:00:00Z"})
        assert client.get_by_resource("does-not-exist") == []

    def test_get_by_resource_respects_limit(self, tmp_path):
        client = self._client(tmp_path)
        for i in range(5):
            client.upsert({
                "id": f"vm-rec-{i}", "resource_id": "vm-23",
                "timestamp": f"2026-02-20T1{i}:00:00Z",
            })
        results = client.get_by_resource("vm-23", limit=2)
        assert len(results) == 2


# ===========================================================================
# AzureSearchClient
# ===========================================================================


class TestAzureSearchClient:
    """Tests for the incident history search client."""

    def test_is_mock_when_use_local_mocks_true(self):
        assert AzureSearchClient(cfg=_MockSettings()).is_mock is True

    def test_is_mock_when_endpoint_missing_even_if_flag_false(self):
        assert AzureSearchClient(cfg=_AzureSettings()).is_mock is True

    def test_is_mock_when_endpoint_exists_but_no_key_anywhere(self):
        assert AzureSearchClient(cfg=_EndpointNoKeySettings()).is_mock is True

    def test_search_returns_list(self):
        results = AzureSearchClient(cfg=_MockSettings()).search_incidents("vm delete")
        assert isinstance(results, list)

    def test_search_each_result_is_dict(self):
        results = AzureSearchClient(cfg=_MockSettings()).search_incidents("resource")
        for item in results:
            assert isinstance(item, dict)

    def test_search_respects_top_limit(self):
        results = AzureSearchClient(cfg=_MockSettings()).search_incidents(
            "resource", top=2
        )
        assert len(results) <= 2

    def test_search_with_action_type_filter(self):
        """action_type filter must only return matching incidents."""
        results = AzureSearchClient(cfg=_MockSettings()).search_incidents(
            "resource", action_type="delete_resource"
        )
        for r in results:
            assert "delete_resource" in r.get("action_taken", "")

    def test_search_with_resource_type_filter(self):
        """resource_type filter must only return matching incidents."""
        results = AzureSearchClient(cfg=_MockSettings()).search_incidents(
            "vm", resource_type="Microsoft.Compute/virtualMachines"
        )
        for r in results:
            assert r.get("resource_type") == "Microsoft.Compute/virtualMachines"

    def test_search_finds_disaster_recovery_incidents(self):
        """'disaster recovery' query should surface the vm-23 DR incident."""
        results = AzureSearchClient(cfg=_MockSettings()).search_incidents(
            "disaster recovery delete vm", top=5
        )
        incident_ids = [r.get("incident_id") for r in results]
        assert "INC-2025-1204" in incident_ids

    def test_search_with_no_filters_returns_results(self):
        """Broad query with no filters should return something from seed data."""
        results = AzureSearchClient(cfg=_MockSettings()).search_incidents(
            "infrastructure resource change", top=10
        )
        assert len(results) >= 1

    def test_search_with_missing_incidents_file_returns_empty(self, tmp_path):
        """If the seed file doesn't exist, search returns [] instead of crashing."""
        nonexistent = tmp_path / "no-such-file.json"
        client = AzureSearchClient(cfg=_MockSettings(), incidents_path=nonexistent)
        results = client.search_incidents("anything")
        assert results == []


# ===========================================================================
# ResourceGraphClient
# ===========================================================================


class TestResourceGraphClient:
    """Tests for the Azure Resource Graph / topology client."""

    def test_is_mock_when_use_local_mocks_true(self):
        assert ResourceGraphClient(cfg=_MockSettings()).is_mock is True

    def test_is_mock_when_subscription_missing_even_if_flag_false(self):
        assert ResourceGraphClient(cfg=_AzureSettings()).is_mock is True

    # --- get_resource ---

    def test_get_resource_returns_dict_for_known_name(self):
        result = ResourceGraphClient(cfg=_MockSettings()).get_resource("vm-23")
        assert isinstance(result, dict)

    def test_get_resource_returns_none_for_unknown(self):
        result = ResourceGraphClient(cfg=_MockSettings()).get_resource("xyz-does-not-exist")
        assert result is None

    def test_get_resource_by_full_azure_id(self):
        """Last-segment fallback: full ID path should resolve to the resource."""
        result = ResourceGraphClient(cfg=_MockSettings()).get_resource(
            "/subscriptions/demo/resourceGroups/prod/"
            "providers/Microsoft.Compute/virtualMachines/vm-23"
        )
        assert result is not None
        assert result["name"] == "vm-23"

    def test_get_resource_has_name_field(self):
        r = ResourceGraphClient(cfg=_MockSettings()).get_resource("vm-23")
        assert "name" in r

    def test_get_resource_has_type_field(self):
        r = ResourceGraphClient(cfg=_MockSettings()).get_resource("vm-23")
        assert "type" in r

    def test_get_resource_has_tags_field(self):
        r = ResourceGraphClient(cfg=_MockSettings()).get_resource("vm-23")
        assert "tags" in r

    def test_vm23_has_disaster_recovery_tag(self):
        """vm-23 is tagged as disaster-recovery in seed data — critical to governance."""
        r = ResourceGraphClient(cfg=_MockSettings()).get_resource("vm-23")
        assert r["tags"].get("purpose") == "disaster-recovery"

    # --- get_dependencies ---

    def test_get_dependencies_returns_list(self):
        deps = ResourceGraphClient(cfg=_MockSettings()).get_dependencies("vm-23")
        assert isinstance(deps, list)

    def test_get_dependencies_non_empty_for_known_resource(self):
        deps = ResourceGraphClient(cfg=_MockSettings()).get_dependencies("vm-23")
        assert len(deps) > 0

    def test_get_dependencies_empty_for_unknown(self):
        deps = ResourceGraphClient(cfg=_MockSettings()).get_dependencies("no-such-resource")
        assert deps == []

    # --- get_dependents ---

    def test_get_dependents_returns_list(self):
        deps = ResourceGraphClient(cfg=_MockSettings()).get_dependents("vm-23")
        assert isinstance(deps, list)

    def test_get_dependents_non_empty_for_known_resource(self):
        """vm-23 has downstream dependents listed in seed data."""
        deps = ResourceGraphClient(cfg=_MockSettings()).get_dependents("vm-23")
        assert len(deps) > 0

    def test_get_dependents_empty_for_unknown(self):
        deps = ResourceGraphClient(cfg=_MockSettings()).get_dependents("not-real")
        assert deps == []

    # --- list_all ---

    def test_list_all_returns_list(self):
        result = ResourceGraphClient(cfg=_MockSettings()).list_all()
        assert isinstance(result, list)

    def test_list_all_returns_multiple_resources(self):
        """Seed data has many resources — list_all should return them all."""
        result = ResourceGraphClient(cfg=_MockSettings()).list_all()
        assert len(result) > 1

    def test_list_all_each_entry_is_dict(self):
        result = ResourceGraphClient(cfg=_MockSettings()).list_all()
        for r in result:
            assert isinstance(r, dict)

    def test_list_all_includes_vm23(self):
        names = [r.get("name") for r in ResourceGraphClient(cfg=_MockSettings()).list_all()]
        assert "vm-23" in names

    def test_missing_resources_file_returns_empty(self, tmp_path):
        """If the seed file is absent, list_all returns [] instead of crashing."""
        client = ResourceGraphClient(
            cfg=_MockSettings(), resources_path=tmp_path / "no-file.json"
        )
        assert client.list_all() == []
        assert client.get_resource("vm-23") is None
