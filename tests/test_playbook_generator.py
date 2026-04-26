"""Tests for Phase 34D: Tier 3 Playbook Generator.

Covers:
1. All 10 supported (action_type, resource_type) combinations return valid Playbooks
2. Unsupported combinations raise PlaybookUnsupportedError
3. Tier 1 combinations (VM, NSG, App Service, AKS, Storage) raise PlaybookUnsupportedError
4. ARM ID parsing — top-level and nested resources
5. proposed_sku / current_sku fill into az_command and rollback_command
6. resource_group fallback — from action.target vs parsed from resource_id
7. supported_combinations() returns at least 10 entries
"""

import pytest

from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
from src.core.playbook_generator import (
    PlaybookUnsupportedError,
    _parse_arm,
    generate_playbook,
    supported_combinations,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _action(
    action_type: ActionType,
    resource_id: str,
    resource_type: str,
    resource_group: str | None = None,
    proposed_sku: str | None = None,
    current_sku: str | None = None,
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
            resource_group=resource_group,
            proposed_sku=proposed_sku,
            current_sku=current_sku,
        ),
        reason="test",
        urgency=Urgency.MEDIUM,
    )


_RG = "rg-test"
_SUB = "/subscriptions/sub-123/resourceGroups/rg-test/providers"


# ---------------------------------------------------------------------------
# ARM ID parser
# ---------------------------------------------------------------------------

def test_parse_arm_top_level():
    rid = f"{_SUB}/Microsoft.Cache/Redis/my-cache"
    out = _parse_arm(rid)
    assert out["resource_group"] == "rg-test"
    assert out["resource_name"] == "my-cache"
    assert out["parent_name"] == ""  # no sub-resource


def test_parse_arm_nested():
    rid = f"{_SUB}/Microsoft.Sql/servers/my-server/databases/my-db"
    out = _parse_arm(rid)
    assert out["resource_group"] == "rg-test"
    assert out["resource_name"] == "my-db"
    assert out["parent_name"] == "my-server"


def test_parse_arm_empty():
    out = _parse_arm("")
    assert out["resource_name"] == ""
    assert out["resource_group"] == ""


# ---------------------------------------------------------------------------
# Supported (action_type, resource_type) combinations — 10 templates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action_type,resource_type,rid_suffix,extra", [
    (
        ActionType.SCALE_UP,
        "Microsoft.Sql/servers/databases",
        "/Microsoft.Sql/servers/my-server/databases/my-db",
        {"proposed_sku": "S3"},
    ),
    (
        ActionType.SCALE_DOWN,
        "Microsoft.Sql/servers/databases",
        "/Microsoft.Sql/servers/my-server/databases/my-db",
        {"proposed_sku": "S1", "current_sku": "S3"},
    ),
    (
        ActionType.RESTART_SERVICE,
        "Microsoft.Cache/Redis",
        "/Microsoft.Cache/Redis/my-cache",
        {},
    ),
    (
        ActionType.SCALE_UP,
        "Microsoft.Cache/Redis",
        "/Microsoft.Cache/Redis/my-cache",
        {"proposed_sku": "Premium"},
    ),
    (
        ActionType.ROTATE_STORAGE_KEY,
        "Microsoft.Cache/Redis",
        "/Microsoft.Cache/Redis/my-cache",
        {},
    ),
    (
        ActionType.UPDATE_CONFIG,
        "Microsoft.KeyVault/vaults",
        "/Microsoft.KeyVault/vaults/my-vault",
        {},
    ),
    (
        ActionType.SCALE_UP,
        "Microsoft.ContainerRegistry/registries",
        "/Microsoft.ContainerRegistry/registries/myacr",
        {"proposed_sku": "Premium"},
    ),
    (
        ActionType.SCALE_DOWN,
        "Microsoft.ContainerRegistry/registries",
        "/Microsoft.ContainerRegistry/registries/myacr",
        {"current_sku": "Premium"},
    ),
    (
        ActionType.UPDATE_CONFIG,
        "Microsoft.DocumentDB/databaseAccounts",
        "/Microsoft.DocumentDB/databaseAccounts/my-cosmos",
        {},
    ),
    (
        ActionType.SCALE_UP,
        "Microsoft.ServiceBus/namespaces",
        "/Microsoft.ServiceBus/namespaces/my-sb",
        {"proposed_sku": "Premium"},
    ),
])
def test_supported_combination(action_type, resource_type, rid_suffix, extra):
    rid = f"{_SUB}{rid_suffix}"
    action = _action(
        action_type=action_type,
        resource_id=rid,
        resource_type=resource_type,
        resource_group=_RG,
        **extra,
    )
    pb = generate_playbook(action)

    assert pb.action_type == action_type.value
    assert pb.resource_id == rid
    assert len(pb.az_command) > 10, "az_command should be non-trivial"
    assert pb.az_command.startswith("az "), f"Expected az command, got: {pb.az_command!r}"
    assert isinstance(pb.executable_args, list)
    assert pb.executable_args[0] == "az"
    assert len(pb.executable_args) >= 4
    assert pb.expected_outcome
    assert pb.risk_level in ("low", "medium", "high")
    assert pb.estimated_duration_seconds > 0
    assert isinstance(pb.requires_downtime, bool)
    assert isinstance(pb.supports_native_what_if, bool)


# ---------------------------------------------------------------------------
# Playbook field correctness
# ---------------------------------------------------------------------------

def test_sql_db_scale_up_fills_placeholders():
    rid = f"{_SUB}/Microsoft.Sql/servers/prod-server/databases/prod-db"
    action = _action(
        ActionType.SCALE_UP,
        rid,
        "Microsoft.Sql/servers/databases",
        resource_group=_RG,
        proposed_sku="P1",
        current_sku="S3",
    )
    pb = generate_playbook(action)

    assert "prod-db" in pb.az_command
    assert "prod-server" in pb.az_command
    assert _RG in pb.az_command
    assert "P1" in pb.az_command
    # Rollback uses current_sku
    assert pb.rollback_command is not None
    assert "S3" in pb.rollback_command
    assert "prod-db" in pb.rollback_command


def test_redis_restart_no_rollback():
    rid = f"{_SUB}/Microsoft.Cache/Redis/my-redis"
    action = _action(ActionType.RESTART_SERVICE, rid, "Microsoft.Cache/Redis")
    pb = generate_playbook(action)

    assert pb.rollback_command is None
    assert pb.requires_downtime is True


def test_keyvault_update_no_rollback():
    rid = f"{_SUB}/Microsoft.KeyVault/vaults/my-vault"
    action = _action(ActionType.UPDATE_CONFIG, rid, "Microsoft.KeyVault/vaults")
    pb = generate_playbook(action)

    assert pb.rollback_command is None
    assert "my-vault" in pb.az_command
    assert "enable-soft-delete" in pb.az_command


def test_resource_group_from_target_field():
    rid = f"{_SUB}/Microsoft.Cache/Redis/my-redis"
    action = _action(ActionType.RESTART_SERVICE, rid, "Microsoft.Cache/Redis", resource_group="explicit-rg")
    pb = generate_playbook(action)
    assert "explicit-rg" in pb.az_command


def test_resource_group_parsed_from_resource_id():
    rid = f"{_SUB}/Microsoft.Cache/Redis/my-redis"
    # No explicit resource_group on target — should parse from rid
    action = _action(ActionType.RESTART_SERVICE, rid, "Microsoft.Cache/Redis")
    pb = generate_playbook(action)
    assert "rg-test" in pb.az_command


def test_executable_args_matches_command_parts():
    rid = f"{_SUB}/Microsoft.ServiceBus/namespaces/my-sb"
    action = _action(ActionType.SCALE_UP, rid, "Microsoft.ServiceBus/namespaces")
    pb = generate_playbook(action)

    # executable_args should be a list starting with "az"
    assert pb.executable_args[0] == "az"
    # Reconstruct command from args and check key parts match
    combined = " ".join(pb.executable_args)
    assert "my-sb" in combined
    assert "rg-test" in combined


# ---------------------------------------------------------------------------
# Cosmos DB alias
# ---------------------------------------------------------------------------

def test_cosmosdb_alias_works():
    rid = f"{_SUB}/Microsoft.CosmosDB/databaseAccounts/my-cosmos"
    action = _action(ActionType.UPDATE_CONFIG, rid, "Microsoft.CosmosDB/databaseAccounts")
    pb = generate_playbook(action)
    assert "cosmosdb" in pb.az_command
    assert "my-cosmos" in pb.az_command


# ---------------------------------------------------------------------------
# Unsupported combinations raise PlaybookUnsupportedError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action_type,resource_type,rid_suffix", [
    # Tier 1 — already handled by SDK tools
    (ActionType.RESTART_SERVICE, "Microsoft.Compute/virtualMachines",     "/Microsoft.Compute/virtualMachines/vm-01"),
    (ActionType.SCALE_UP,        "Microsoft.Web/serverfarms",             "/Microsoft.Web/serverfarms/plan-01"),
    (ActionType.RESTART_SERVICE, "Microsoft.Web/sites",                  "/Microsoft.Web/sites/app-01"),
    (ActionType.SCALE_UP,        "Microsoft.ContainerService/managedClusters", "/Microsoft.ContainerService/managedClusters/aks-01"),
    (ActionType.ROTATE_STORAGE_KEY, "Microsoft.Storage/storageAccounts", "/Microsoft.Storage/storageAccounts/storage01"),
    # Truly unsupported
    (ActionType.DELETE_RESOURCE, "Microsoft.Compute/virtualMachines",    "/Microsoft.Compute/virtualMachines/vm-01"),
    (ActionType.CREATE_RESOURCE, "Microsoft.Network/virtualNetworks",    "/Microsoft.Network/virtualNetworks/vnet-01"),
])
def test_unsupported_combination_raises(action_type, resource_type, rid_suffix):
    rid = f"{_SUB}{rid_suffix}"
    action = _action(action_type, rid, resource_type)
    with pytest.raises(PlaybookUnsupportedError):
        generate_playbook(action)


# ---------------------------------------------------------------------------
# supported_combinations() utility
# ---------------------------------------------------------------------------

def test_supported_combinations_has_ten_or_more():
    combos = supported_combinations()
    assert len(combos) >= 10, f"Expected at least 10 templates, got {len(combos)}"


def test_supported_combinations_are_tuples():
    for combo in supported_combinations():
        assert isinstance(combo, tuple)
        assert len(combo) == 2
        action_type_str, resource_type_str = combo
        assert isinstance(action_type_str, str)
        assert isinstance(resource_type_str, str)
        # Keys are always lowercase
        assert action_type_str == action_type_str.lower()
        assert resource_type_str == resource_type_str.lower()
