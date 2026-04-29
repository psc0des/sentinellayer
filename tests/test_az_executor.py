"""Tests for Phase 34E: Audited az CLI Executor with allowlist.

Covers:
1. validate_command — all 13 allowlisted patterns pass
2. validate_command — injection attempts and unlisted commands are rejected
3. execute_playbook dry_run — writes audit record, no subprocess called
4. execute_playbook live mock mode — writes audit record with simulated result
5. AllowlistDeniedError — raised and audit record written for rejected commands
6. Audit record fields — execution_id, mode, allowlist_matched, approved_by
7. ExecutionGateway skips az_execution records on load
"""

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.az_executor import AllowlistDeniedError, execute_playbook, validate_command
from src.core.models import ActionTarget, ActionType, AzPlaybookExecution, Playbook, ProposedAction, Urgency
from src.core.playbook_generator import generate_playbook
from src.infrastructure.cosmos_client import CosmosAzExecutionClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_cfg(use_local_mocks: bool = True) -> MagicMock:
    cfg = MagicMock()
    cfg.use_local_mocks = use_local_mocks
    cfg.cosmos_endpoint = ""
    cfg.cosmos_key = ""
    return cfg


def _make_playbook(
    az_command: str,
    executable_args: list[str],
    resource_id: str = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Cache/Redis/my-cache",
) -> Playbook:
    return Playbook(
        action_type="restart_service",
        resource_id=resource_id,
        az_command=az_command,
        executable_args=executable_args,
        rollback_command=None,
        expected_outcome="Cache nodes restarted.",
        risk_level="medium",
        estimated_duration_seconds=120,
        requires_downtime=True,
        supports_native_what_if=False,
    )


def _in_memory_cosmos(tmp_path: Path) -> CosmosAzExecutionClient:
    """CosmosAzExecutionClient pointing at a temp dir so tests don't touch data/."""
    client = CosmosAzExecutionClient.__new__(CosmosAzExecutionClient)
    client._cfg = _mock_cfg()
    client._dir = tmp_path / "az_executions"
    client._dir.mkdir(parents=True, exist_ok=True)
    client._is_mock = True
    client._container = None
    return client


# ---------------------------------------------------------------------------
# 1. validate_command — allowlisted patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    # App Service / Function App
    ["az", "webapp", "restart", "--name", "my-app", "--resource-group", "my-rg"],
    ["az", "functionapp", "restart", "--name", "func-01", "--resource-group", "rg-prod"],
    ["az", "appservice", "plan", "update", "--name", "plan-01", "--resource-group", "rg", "--sku", "P2v2"],
    # AKS
    ["az", "aks", "nodepool", "scale", "--name", "np1", "--cluster-name", "aks-01", "--resource-group", "rg", "--node-count", "5"],
    # Storage
    ["az", "storage", "account", "keys", "renew", "--account-name", "storage01", "--resource-group", "rg", "--key", "key1"],
    ["az", "storage", "account", "keys", "renew", "--account-name", "storage01", "--resource-group", "rg", "--key", "Primary"],
    # SQL Database
    ["az", "sql", "db", "update", "--name", "mydb", "--server", "my-server", "--resource-group", "rg", "--service-objective", "S3"],
    # Redis
    ["az", "redis", "force-reboot", "--name", "my-redis", "--resource-group", "rg", "--reboot-type", "AllNodes"],
    ["az", "redis", "update", "--name", "my-redis", "--resource-group", "rg", "--sku", "Premium", "--vm-size", "P1"],
    ["az", "redis", "regenerate-keys", "--name", "my-redis", "--resource-group", "rg", "--key-type", "Primary"],
    # Key Vault
    ["az", "keyvault", "update", "--name", "my-vault", "--resource-group", "rg", "--enable-soft-delete", "true", "--retention-days", "90"],
    # Container Registry
    ["az", "acr", "update", "--name", "myacr", "--resource-group", "rg", "--sku", "Premium"],
    # Cosmos DB
    ["az", "cosmosdb", "update", "--name", "my-cosmos", "--resource-group", "rg", "--default-consistency-level", "Session"],
    # Service Bus
    ["az", "servicebus", "namespace", "update", "--name", "my-sb", "--resource-group", "rg", "--sku", "Premium"],
    # Virtual Machines
    ["az", "vm", "restart", "--name", "vm-web-01", "--resource-group", "ruriskry-prod-rg"],
    ["az", "vm", "update", "--resource-group", "ruriskry-prod-rg", "--name", "vm-web-01", "--set", "osProfile.linuxConfiguration.disablePasswordAuthentication=true"],
    ["az", "vm", "update", "--resource-group", "rg", "--name", "vm-01", "--set", "osProfile.linuxConfiguration.disablePasswordAuthentication=false"],
    ["az", "vm", "resize", "--name", "vm-dr-01", "--resource-group", "ruriskry-prod-rg", "--size", "Standard_DS2_v2"],
    ["az", "vm", "delete", "--name", "vm-dr-01", "--resource-group", "ruriskry-prod-rg", "--yes"],
    # NSG
    ["az", "network", "nsg", "rule", "update", "--nsg-name", "nsg-east-prod", "--resource-group", "ruriskry-prod-rg", "--name", "AllowSSH", "--access", "Deny"],
    ["az", "network", "nsg", "rule", "update", "--nsg-name", "nsg-east-prod", "--resource-group", "ruriskry-prod-rg", "--name", "AllowHTTP", "--access", "Allow"],
])
def test_validate_command_allowlisted(args):
    assert validate_command(args) is True


# ---------------------------------------------------------------------------
# 2. validate_command — rejected commands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args,reason", [
    # Not az
    (["bash", "-c", "rm -rf /"],                                     "not az"),
    # Unknown subcommand
    (["az", "vm", "delete", "--name", "vm", "--resource-group", "rg"], "vm delete missing required --yes flag"),
    # Shell injection in resource name
    (["az", "webapp", "restart", "--name", "app; rm -rf /", "--resource-group", "rg"], "semicolon injection"),
    (["az", "webapp", "restart", "--name", "app$(whoami)", "--resource-group", "rg"], "subshell injection"),
    # Extra flags
    (["az", "webapp", "restart", "--name", "app", "--resource-group", "rg", "--ids", "extra"], "extra flag"),
    # Empty
    ([],                                                              "empty args"),
    # Missing required flag
    (["az", "webapp", "restart", "--name", "app"],                   "missing --resource-group"),
])
def test_validate_command_rejected(args, reason):
    assert validate_command(args) is False, f"Expected rejection for: {reason}"


# ---------------------------------------------------------------------------
# 3. execute_playbook dry_run — audit record, no subprocess
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_playbook_dry_run_writes_record(tmp_path):
    pb = _make_playbook(
        "az redis force-reboot --name my-redis --resource-group rg --reboot-type AllNodes",
        ["az", "redis", "force-reboot", "--name", "my-redis", "--resource-group", "rg", "--reboot-type", "AllNodes"],
    )
    cosmos = _in_memory_cosmos(tmp_path)
    cfg = _mock_cfg()

    result = await execute_playbook(
        playbook=pb,
        mode="dry_run",
        approved_by="user@example.com",
        decision_id="decision-123",
        cfg=cfg,
        _cosmos=cosmos,
    )

    assert isinstance(result, AzPlaybookExecution)
    assert result.mode == "dry_run"
    assert result.allowlist_matched is True
    assert result.approved_by == "user@example.com"
    assert result.decision_id == "decision-123"
    assert result.record_type == "az_execution"
    # dry_run with supports_native_what_if=False → no subprocess, exit_code is None
    assert result.exit_code is None
    assert "dry_run" in result.notes

    # Audit record written to disk
    written = list((tmp_path / "az_executions").glob("*.json"))
    assert len(written) == 1


# ---------------------------------------------------------------------------
# 4. execute_playbook live mock mode — simulated result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_playbook_live_mock_mode(tmp_path):
    pb = _make_playbook(
        "az redis force-reboot --name my-redis --resource-group rg --reboot-type AllNodes",
        ["az", "redis", "force-reboot", "--name", "my-redis", "--resource-group", "rg", "--reboot-type", "AllNodes"],
    )
    cosmos = _in_memory_cosmos(tmp_path)
    cfg = _mock_cfg(use_local_mocks=True)

    result = await execute_playbook(
        playbook=pb,
        mode="live",
        approved_by="admin@example.com",
        decision_id="decision-456",
        cfg=cfg,
        _cosmos=cosmos,
    )

    assert result.mode == "live"
    assert result.allowlist_matched is True
    assert result.exit_code == 0
    assert "[mock]" in result.stdout
    assert result.duration_ms is not None
    assert result.executed_at is not None


# ---------------------------------------------------------------------------
# 5. AllowlistDeniedError — rejected command, audit record still written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allowlist_denied_writes_audit_record(tmp_path):
    pb = _make_playbook(
        "az vm delete --name vm-01 --resource-group rg",
        ["az", "vm", "delete", "--name", "vm-01", "--resource-group", "rg"],
    )
    cosmos = _in_memory_cosmos(tmp_path)
    cfg = _mock_cfg()

    with pytest.raises(AllowlistDeniedError):
        await execute_playbook(
            playbook=pb,
            mode="live",
            approved_by="attacker@example.com",
            decision_id="decision-789",
            cfg=cfg,
            _cosmos=cosmos,
        )

    # Audit record still written (rejection must be audited)
    written = list((tmp_path / "az_executions").glob("*.json"))
    assert len(written) == 1
    import json
    rec = json.loads(written[0].read_text())
    assert rec["allowlist_matched"] is False
    assert "REJECTED" in rec["notes"]


# ---------------------------------------------------------------------------
# 6. Audit record field contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_record_has_required_fields(tmp_path):
    pb = _make_playbook(
        "az keyvault update --name my-vault --resource-group rg --enable-soft-delete true --retention-days 90",
        ["az", "keyvault", "update", "--name", "my-vault", "--resource-group", "rg",
         "--enable-soft-delete", "true", "--retention-days", "90"],
        resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/my-vault",
    )
    cosmos = _in_memory_cosmos(tmp_path)
    cfg = _mock_cfg()

    result = await execute_playbook(
        playbook=pb,
        mode="dry_run",
        approved_by="ops@example.com",
        decision_id="d-001",
        cfg=cfg,
        _cosmos=cosmos,
    )

    # All required fields present
    assert result.execution_id
    assert result.record_type == "az_execution"
    assert result.decision_id == "d-001"
    assert result.resource_id == pb.resource_id
    assert result.action_type == pb.action_type
    assert result.az_command == pb.az_command
    assert result.executable_args == pb.executable_args
    assert result.approved_by == "ops@example.com"
    assert result.created_at is not None


# ---------------------------------------------------------------------------
# 7. validate_command rejects injection with special chars in name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "name|evil",
    "name&evil",
    "name`evil`",
    "name$(evil)",
    "../../../etc/passwd",
    "name\nevil",
    "name; evil",
])
def test_injection_attempts_rejected(name):
    args = ["az", "webapp", "restart", "--name", name, "--resource-group", "rg"]
    assert validate_command(args) is False


# ---------------------------------------------------------------------------
# 8. Integration: generate_playbook → validate_command passes for all templates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action_type,resource_type,rid_suffix,extra", [
    (ActionType.SCALE_UP,           "Microsoft.Sql/servers/databases",         "/Microsoft.Sql/servers/my-server/databases/my-db",  {"proposed_sku": "S3"}),
    (ActionType.RESTART_SERVICE,    "Microsoft.Cache/Redis",                   "/Microsoft.Cache/Redis/my-cache",                   {}),
    (ActionType.SCALE_UP,           "Microsoft.Cache/Redis",                   "/Microsoft.Cache/Redis/my-cache",                   {"proposed_sku": "Premium"}),
    (ActionType.ROTATE_STORAGE_KEY, "Microsoft.Cache/Redis",                   "/Microsoft.Cache/Redis/my-cache",                   {}),
    (ActionType.UPDATE_CONFIG,      "Microsoft.KeyVault/vaults",               "/Microsoft.KeyVault/vaults/my-vault",               {}),
    (ActionType.SCALE_UP,           "Microsoft.ContainerRegistry/registries",  "/Microsoft.ContainerRegistry/registries/myacr",     {}),
    (ActionType.UPDATE_CONFIG,      "Microsoft.DocumentDB/databaseAccounts",   "/Microsoft.DocumentDB/databaseAccounts/my-cosmos",  {}),
    (ActionType.SCALE_UP,           "Microsoft.ServiceBus/namespaces",         "/Microsoft.ServiceBus/namespaces/my-sb",            {}),
])
def test_generated_playbook_args_pass_allowlist(action_type, resource_type, rid_suffix, extra):
    _sub = "/subscriptions/sub-123/resourceGroups/rg-test/providers"
    rid = f"{_sub}{rid_suffix}"
    action = ProposedAction(
        agent_id="test",
        action_type=action_type,
        target=ActionTarget(
            resource_id=rid,
            resource_type=resource_type,
            resource_group="rg-test",
            **extra,
        ),
        reason="test",
        urgency=Urgency.MEDIUM,
    )
    pb = generate_playbook(action)
    assert validate_command(pb.executable_args), (
        f"Generated args failed allowlist for {action_type.value}+{resource_type}: "
        f"{' '.join(pb.executable_args)}"
    )


# ---------------------------------------------------------------------------
# 9. ExecutionGateway skips az_execution records
# ---------------------------------------------------------------------------

def test_execution_gateway_skips_az_execution_records(tmp_path):
    """ExecutionGateway._ensure_loaded must not raise on az_execution records."""
    import json
    from src.core.execution_gateway import ExecutionGateway

    exec_dir = tmp_path / "executions"
    exec_dir.mkdir()

    # Write a valid az_execution record that has no ExecutionRecord fields
    az_rec = {
        "id": "az-exec-001",
        "execution_id": "az-exec-001",
        "record_type": "az_execution",
        "decision_id": "d-001",
        "resource_id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Cache/Redis/r",
        "action_type": "restart_service",
        "az_command": "az redis force-reboot ...",
        "executable_args": ["az", "redis", "force-reboot"],
        "mode": "dry_run",
        "approved_by": "user@example.com",
        "allowlist_matched": True,
        "created_at": "2026-04-26T00:00:00+00:00",
    }
    (exec_dir / "az-exec-001.json").write_text(json.dumps(az_rec), encoding="utf-8")

    cfg = MagicMock()
    cfg.use_local_mocks = True
    cfg.cosmos_endpoint = ""
    cfg.cosmos_key = ""
    cfg.execution_gateway_enabled = True
    cfg.iac_github_repo = ""
    cfg.github_token = ""
    cfg.iac_terraform_path = ""

    gw = ExecutionGateway(executions_dir=exec_dir)
    gw._ensure_loaded()

    # The az record must not appear in the gateway's SDK records
    assert "az-exec-001" not in gw._records
