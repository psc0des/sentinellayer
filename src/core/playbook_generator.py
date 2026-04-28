"""Tier 3 Playbook Generator — Phase 34D.

Generates remediation playbooks for action+resource combinations that have no
Tier 1 SDK tool.  Templates are hard-coded per (action_type, resource_type)
pair; placeholders are filled from the ProposedAction without any LLM call.

Covered (action_type, resource_type) combinations beyond Tier 1:
  1.  (scale_up,          microsoft.sql/servers/databases)
  2.  (scale_down,        microsoft.sql/servers/databases)
  3.  (restart_service,   microsoft.cache/redis)
  4.  (scale_up,          microsoft.cache/redis)
  5.  (rotate_storage_key,microsoft.cache/redis)
  6.  (update_config,     microsoft.keyvault/vaults)
  7.  (scale_up,          microsoft.containerregistry/registries)
  8.  (scale_down,        microsoft.containerregistry/registries)
  9.  (update_config,     microsoft.documentdb/databaseaccounts)
  10. (scale_up,          microsoft.servicebus/namespaces)

Tier 1 SDK tools already cover:
  - Microsoft.Compute/virtualMachines      (start_vm, restart_vm, resize_vm)
  - Microsoft.Network/networkSecurityGroups (create/delete_nsg_rule)
  - Microsoft.Web/sites                    (restart_app_service, restart_function_app)
  - Microsoft.Web/serverfarms             (scale_app_service_plan)
  - Microsoft.ContainerService/managedClusters (scale_aks_nodepool)
  - Microsoft.Storage/storageAccounts     (rotate_storage_keys)
"""
from __future__ import annotations

from typing import NamedTuple

from src.core.models import Playbook, ProposedAction


class PlaybookUnsupportedError(ValueError):
    """Raised when no Tier 3 template exists for the given action+resource combo."""


# ---------------------------------------------------------------------------
# ARM ID parser
# ---------------------------------------------------------------------------

def _parse_arm(resource_id: str) -> dict[str, str]:
    """Extract resource_group, resource_name, parent_name from an ARM resource ID.

    For top-level resources like ``/…/providers/Microsoft.Cache/Redis/my-cache``:
      resource_name = "my-cache"
      parent_name   = "" (no parent)

    For nested resources like ``/…/servers/my-server/databases/my-db``:
      resource_name = "my-db"
      parent_name   = "my-server"
    """
    segs = [s for s in resource_id.split('/') if s]
    out: dict[str, str] = {'resource_group': '', 'resource_name': '', 'parent_name': ''}
    for i, seg in enumerate(segs):
        if seg.lower() == 'resourcegroups' and i + 1 < len(segs):
            out['resource_group'] = segs[i + 1]
    out['resource_name'] = segs[-1] if segs else ''
    # Find the "providers" segment; the resource pairs start after the provider namespace.
    # Top-level:  providers/{ns}/{type}/{name}        → 2 segments after ns → no parent
    # Nested:     providers/{ns}/{type}/{name}/{t2}/{n2} → 4 segments after ns → parent is segs[1]
    for i, seg in enumerate(segs):
        if seg.lower() == 'providers' and i + 2 < len(segs):
            after_ns = segs[i + 2:]   # skip "providers" + namespace
            if len(after_ns) >= 4:
                out['parent_name'] = after_ns[1]
            break
    return out


# ---------------------------------------------------------------------------
# Internal template structure
# ---------------------------------------------------------------------------

class _Tmpl(NamedTuple):
    cmd: str                            # display string; {rg}, {name}, {parent}, {sku}, {current_sku}
    args: list[str]                     # argv list with same placeholders (Phase E safe invocation)
    rollback: str | None                # rollback display string or None
    rollback_args: list[str] | None     # rollback argv list or None
    outcome: str                        # plain-English expected outcome
    risk: str                           # "low" | "medium" | "high"
    duration: int                       # estimated seconds
    downtime: bool
    what_if: bool                       # True → az ... --what-if is meaningful


# ---------------------------------------------------------------------------
# Template registry — keyed by (action_type_value, resource_type_lower)
# ---------------------------------------------------------------------------

_TEMPLATES: dict[tuple[str, str], _Tmpl] = {

    # ── SQL Database ────────────────────────────────────────────────────────

    ("scale_up", "microsoft.sql/servers/databases"): _Tmpl(
        cmd=(
            "az sql db update --name {name} --server {parent} "
            "--resource-group {rg} --service-objective {sku}"
        ),
        args=[
            "az", "sql", "db", "update",
            "--name", "{name}", "--server", "{parent}",
            "--resource-group", "{rg}",
            "--service-objective", "{sku}",
        ],
        rollback=(
            "az sql db update --name {name} --server {parent} "
            "--resource-group {rg} --service-objective {current_sku}"
        ),
        rollback_args=[
            "az", "sql", "db", "update",
            "--name", "{name}", "--server", "{parent}",
            "--resource-group", "{rg}",
            "--service-objective", "{current_sku}",
        ],
        outcome=(
            "Database scaled to a higher service objective; increased DTUs/vCores "
            "improve query throughput and reduce throttling."
        ),
        risk="medium",
        duration=300,
        downtime=False,
        what_if=False,
    ),

    ("scale_down", "microsoft.sql/servers/databases"): _Tmpl(
        cmd=(
            "az sql db update --name {name} --server {parent} "
            "--resource-group {rg} --service-objective {sku}"
        ),
        args=[
            "az", "sql", "db", "update",
            "--name", "{name}", "--server", "{parent}",
            "--resource-group", "{rg}",
            "--service-objective", "{sku}",
        ],
        rollback=(
            "az sql db update --name {name} --server {parent} "
            "--resource-group {rg} --service-objective {current_sku}"
        ),
        rollback_args=[
            "az", "sql", "db", "update",
            "--name", "{name}", "--server", "{parent}",
            "--resource-group", "{rg}",
            "--service-objective", "{current_sku}",
        ],
        outcome=(
            "Database scaled to a lower service objective; reduces cost "
            "at the expense of performance headroom. Verify throughput after change."
        ),
        risk="low",
        duration=300,
        downtime=False,
        what_if=False,
    ),

    # ── Redis Cache ─────────────────────────────────────────────────────────

    ("restart_service", "microsoft.cache/redis"): _Tmpl(
        cmd=(
            "az redis force-reboot --name {name} --resource-group {rg} "
            "--reboot-type AllNodes"
        ),
        args=[
            "az", "redis", "force-reboot",
            "--name", "{name}", "--resource-group", "{rg}",
            "--reboot-type", "AllNodes",
        ],
        rollback=None,
        rollback_args=None,
        outcome=(
            "Redis cache nodes restarted; in-memory data flushed. "
            "Clients reconnect automatically — expect ~2 minutes of downtime."
        ),
        risk="medium",
        duration=120,
        downtime=True,
        what_if=False,
    ),

    ("scale_up", "microsoft.cache/redis"): _Tmpl(
        cmd=(
            "az redis update --name {name} --resource-group {rg} "
            "--sku Premium --vm-size P1"
        ),
        args=[
            "az", "redis", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "Premium", "--vm-size", "P1",
        ],
        rollback=(
            "az redis update --name {name} --resource-group {rg} "
            "--sku {current_sku} --vm-size C1"
        ),
        rollback_args=[
            "az", "redis", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "{current_sku}", "--vm-size", "C1",
        ],
        outcome=(
            "Redis scaled to Premium tier; unlocks clustering, geo-replication, "
            "and higher throughput. Migration completes in ~30 minutes."
        ),
        risk="low",
        duration=1800,
        downtime=False,
        what_if=False,
    ),

    ("rotate_storage_key", "microsoft.cache/redis"): _Tmpl(
        cmd=(
            "az redis regenerate-keys --name {name} --resource-group {rg} "
            "--key-type Primary"
        ),
        args=[
            "az", "redis", "regenerate-keys",
            "--name", "{name}", "--resource-group", "{rg}",
            "--key-type", "Primary",
        ],
        rollback=None,
        rollback_args=None,
        outcome=(
            "Primary access key rotated. Update all clients that connect via this "
            "key with the new value returned by the command."
        ),
        risk="medium",
        duration=30,
        downtime=False,
        what_if=False,
    ),

    # ── Key Vault ───────────────────────────────────────────────────────────

    ("update_config", "microsoft.keyvault/vaults"): _Tmpl(
        cmd=(
            "az keyvault update --name {name} --resource-group {rg} "
            "--enable-soft-delete true --retention-days 90"
        ),
        args=[
            "az", "keyvault", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--enable-soft-delete", "true",
            "--retention-days", "90",
        ],
        rollback=None,
        rollback_args=None,
        outcome=(
            "Soft-delete enabled with 90-day retention. Deleted secrets, keys, and "
            "certificates are recoverable for 90 days before permanent removal."
        ),
        risk="low",
        duration=30,
        downtime=False,
        what_if=False,
    ),

    # ── Container Registry ──────────────────────────────────────────────────

    ("scale_up", "microsoft.containerregistry/registries"): _Tmpl(
        cmd=(
            "az acr update --name {name} --resource-group {rg} --sku Premium"
        ),
        args=[
            "az", "acr", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "Premium",
        ],
        rollback=(
            "az acr update --name {name} --resource-group {rg} --sku {current_sku}"
        ),
        rollback_args=[
            "az", "acr", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "{current_sku}",
        ],
        outcome=(
            "Registry upgraded to Premium tier; enables geo-replication, content "
            "trust, private endpoints, and higher concurrent pull limits."
        ),
        risk="low",
        duration=60,
        downtime=False,
        what_if=False,
    ),

    ("scale_down", "microsoft.containerregistry/registries"): _Tmpl(
        cmd=(
            "az acr update --name {name} --resource-group {rg} --sku Standard"
        ),
        args=[
            "az", "acr", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "Standard",
        ],
        rollback=(
            "az acr update --name {name} --resource-group {rg} --sku {current_sku}"
        ),
        rollback_args=[
            "az", "acr", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "{current_sku}",
        ],
        outcome=(
            "Registry downgraded to Standard tier; geo-replication and private "
            "endpoints will be disabled. Verify no dependent pipelines rely on them."
        ),
        risk="low",
        duration=60,
        downtime=False,
        what_if=False,
    ),

    # ── Cosmos DB (ARM provider = Microsoft.DocumentDB) ─────────────────────

    ("update_config", "microsoft.documentdb/databaseaccounts"): _Tmpl(
        cmd=(
            "az cosmosdb update --name {name} --resource-group {rg} "
            "--default-consistency-level Session"
        ),
        args=[
            "az", "cosmosdb", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--default-consistency-level", "Session",
        ],
        rollback=(
            "az cosmosdb update --name {name} --resource-group {rg} "
            "--default-consistency-level Eventual"
        ),
        rollback_args=[
            "az", "cosmosdb", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--default-consistency-level", "Eventual",
        ],
        outcome=(
            "Default consistency set to Session; guarantees read-your-writes within "
            "a client session with minimal latency impact."
        ),
        risk="medium",
        duration=120,
        downtime=False,
        what_if=False,
    ),

    # ── Service Bus ─────────────────────────────────────────────────────────

    ("scale_up", "microsoft.servicebus/namespaces"): _Tmpl(
        cmd=(
            "az servicebus namespace update --name {name} --resource-group {rg} "
            "--sku Premium"
        ),
        args=[
            "az", "servicebus", "namespace", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "Premium",
        ],
        rollback=(
            "az servicebus namespace update --name {name} --resource-group {rg} "
            "--sku Standard"
        ),
        rollback_args=[
            "az", "servicebus", "namespace", "update",
            "--name", "{name}", "--resource-group", "{rg}",
            "--sku", "Standard",
        ],
        outcome=(
            "Service Bus upgraded to Premium tier; dedicated capacity, VNet "
            "integration, and higher message throughput. Migration takes ~10 minutes."
        ),
        risk="low",
        duration=600,
        downtime=False,
        what_if=False,
    ),
    # ── Virtual Machines ────────────────────────────────────────────────────

    ("update_config", "microsoft.compute/virtualmachines"): _Tmpl(
        cmd=(
            "az vm boot-diagnostics enable --name {name} --resource-group {rg}"
        ),
        args=[
            "az", "vm", "boot-diagnostics", "enable",
            "--name", "{name}", "--resource-group", "{rg}",
        ],
        rollback=(
            "az vm boot-diagnostics disable --name {name} --resource-group {rg}"
        ),
        rollback_args=[
            "az", "vm", "boot-diagnostics", "disable",
            "--name", "{name}", "--resource-group", "{rg}",
        ],
        outcome=(
            "Boot diagnostics enabled on the VM — captures serial console output "
            "and screenshots for post-restart debugging. Uses managed storage by "
            "default in modern subscriptions. Append --storage <account> if an "
            "explicit storage account is required."
        ),
        risk="low",
        duration=60,
        downtime=False,
        what_if=False,
    ),

    ("delete_resource", "microsoft.compute/virtualmachines"): _Tmpl(
        cmd=(
            "az vm delete --name {name} --resource-group {rg} --yes"
        ),
        args=[
            "az", "vm", "delete",
            "--name", "{name}", "--resource-group", "{rg}",
            "--yes",
        ],
        rollback=None,
        rollback_args=None,
        outcome=(
            "Virtual machine deleted. All associated OS and data disks are detached "
            "but not automatically deleted — verify disk cleanup separately."
        ),
        risk="high",
        duration=120,
        downtime=True,
        what_if=False,
    ),

    ("restart_service", "microsoft.compute/virtualmachines"): _Tmpl(
        cmd=(
            "az vm restart --name {name} --resource-group {rg}"
        ),
        args=[
            "az", "vm", "restart",
            "--name", "{name}", "--resource-group", "{rg}",
        ],
        rollback=None,
        rollback_args=None,
        outcome=(
            "Virtual machine restarted. Guest OS will reboot and services will resume "
            "automatically. Expect 2–5 minutes of downtime."
        ),
        risk="medium",
        duration=300,
        downtime=True,
        what_if=False,
    ),

    ("scale_down", "microsoft.compute/virtualmachines"): _Tmpl(
        cmd=(
            "az vm resize --name {name} --resource-group {rg} --size {sku}"
        ),
        args=[
            "az", "vm", "resize",
            "--name", "{name}", "--resource-group", "{rg}",
            "--size", "{sku}",
        ],
        rollback=(
            "az vm resize --name {name} --resource-group {rg} --size {current_sku}"
        ),
        rollback_args=[
            "az", "vm", "resize",
            "--name", "{name}", "--resource-group", "{rg}",
            "--size", "{current_sku}",
        ],
        outcome=(
            "VM resized to a smaller SKU; reduces compute cost. "
            "A reboot is required — expect 3–5 minutes of downtime."
        ),
        risk="medium",
        duration=300,
        downtime=True,
        what_if=False,
    ),

    # ── Network Security Groups ─────────────────────────────────────────────

    ("modify_nsg", "microsoft.network/networksecuritygroups"): _Tmpl(
        cmd=(
            "az network nsg rule update --nsg-name {name} "
            "--resource-group {rg} --name {rule} --access Deny"
        ),
        args=[
            "az", "network", "nsg", "rule", "update",
            "--nsg-name", "{name}", "--resource-group", "{rg}",
            "--name", "{rule}", "--access", "Deny",
        ],
        rollback=(
            "az network nsg rule update --nsg-name {name} "
            "--resource-group {rg} --name {rule} --access Allow"
        ),
        rollback_args=[
            "az", "network", "nsg", "rule", "update",
            "--nsg-name", "{name}", "--resource-group", "{rg}",
            "--name", "{rule}", "--access", "Allow",
        ],
        outcome=(
            "NSG rule access changed to Deny, restricting the flagged traffic path. "
            "Verify connectivity requirements before applying in production."
        ),
        risk="medium",
        duration=30,
        downtime=False,
        what_if=False,
    ),

    ("update_config", "microsoft.network/networksecuritygroups"): _Tmpl(
        cmd=(
            "az network nsg rule update --nsg-name {name} "
            "--resource-group {rg} --name {rule} --access Deny"
        ),
        args=[
            "az", "network", "nsg", "rule", "update",
            "--nsg-name", "{name}", "--resource-group", "{rg}",
            "--name", "{rule}", "--access", "Deny",
        ],
        rollback=(
            "az network nsg rule update --nsg-name {name} "
            "--resource-group {rg} --name {rule} --access Allow"
        ),
        rollback_args=[
            "az", "network", "nsg", "rule", "update",
            "--nsg-name", "{name}", "--resource-group", "{rg}",
            "--name", "{rule}", "--access", "Allow",
        ],
        outcome=(
            "NSG rule updated to restrict flagged access. "
            "Review existing connections that may be interrupted."
        ),
        risk="medium",
        duration=30,
        downtime=False,
        what_if=False,
    ),
}

# Alias: some callers use "microsoft.cosmosdb/databaseaccounts" colloquially
_TEMPLATES[("update_config", "microsoft.cosmosdb/databaseaccounts")] = (
    _TEMPLATES[("update_config", "microsoft.documentdb/databaseaccounts")]
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def supported_combinations() -> list[tuple[str, str]]:
    """Return all (action_type, resource_type) pairs that have a template."""
    return list(_TEMPLATES.keys())


def _extract_rule_name(action: ProposedAction) -> str:
    """Return the NSG rule name for templates that need {rule}.

    Priority: explicit nsg_rule_names on the action → /securityRules/<name>
    segment in the resource_id → human-readable placeholder.
    """
    if getattr(action, 'nsg_rule_names', None):
        return action.nsg_rule_names[0]
    rid = action.target.resource_id or ''
    if '/securityRules/' in rid:
        return rid.split('/securityRules/')[-1]
    return 'RULE_NAME'


def generate_playbook(
    action: ProposedAction,
    resource_details: dict | None = None,
) -> Playbook:
    """Generate a Tier 3 remediation playbook for *action*.

    Args:
        action: The :class:`~src.core.models.ProposedAction` to generate a
            playbook for.  Must have ``target.resource_id`` and
            ``target.resource_type`` set.
        resource_details: Optional extra context (e.g. from a live resource
            query).  Currently unused but reserved for Phase E enrichment.

    Returns:
        A fully populated :class:`~src.core.models.Playbook`.

    Raises:
        PlaybookUnsupportedError: If no template exists for this
            ``(action_type, resource_type)`` combination.
    """
    if resource_details is None:
        resource_details = {}

    rt = (action.target.resource_type or '').strip().lower()
    at = action.action_type.value if hasattr(action.action_type, 'value') else str(action.action_type)
    key = (at, rt)

    tmpl = _TEMPLATES.get(key)
    if tmpl is None:
        raise PlaybookUnsupportedError(
            f"No Tier 3 playbook template for action_type={at!r}, "
            f"resource_type={rt!r}. "
            "Tier 1 SDK tools cover: VMs, NSGs, App Service, AKS nodepools, "
            "Storage Account keys. "
            "For other combinations, open a GitHub issue to request a template."
        )

    arm = _parse_arm(action.target.resource_id)
    ctx = {
        'rg': action.target.resource_group or arm['resource_group'] or 'RESOURCE_GROUP',
        'name': arm['resource_name'] or 'RESOURCE_NAME',
        'parent': arm['parent_name'] or 'PARENT_NAME',
        'sku': action.target.proposed_sku or resource_details.get('proposed_sku', 'S3'),
        'current_sku': (
            action.target.current_sku
            or resource_details.get('current_sku')
            or action.target.proposed_sku
            or 'S2'
        ),
        'rule': _extract_rule_name(action),
    }

    def fill(s: str) -> str:
        return s.format(**ctx)

    def fill_list(lst: list[str]) -> list[str]:
        return [fill(s) for s in lst]

    return Playbook(
        action_type=at,
        resource_id=action.target.resource_id,
        az_command=fill(tmpl.cmd),
        executable_args=fill_list(tmpl.args),
        rollback_command=fill(tmpl.rollback) if tmpl.rollback else None,
        expected_outcome=tmpl.outcome,
        risk_level=tmpl.risk,
        estimated_duration_seconds=tmpl.duration,
        requires_downtime=tmpl.downtime,
        supports_native_what_if=tmpl.what_if,
    )
