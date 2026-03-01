"""Generic Azure investigation tools — shared by all operational agents.

Live mode (USE_LOCAL_MOCKS=false): real Azure SDK calls using
DefaultAzureCredential (works with ``az login`` locally and Managed Identity
in Azure — no code changes between environments).

Mock mode (USE_LOCAL_MOCKS=true, or when Azure SDK calls fail):
returns realistic data sourced from ``data/seed_resources.json``.

Design principles
-----------------
- All functions are **synchronous** so they can be called directly from
  ``@af.tool`` decorated functions inside the Microsoft Agent Framework
  without needing ``asyncio.run()`` (which cannot be called inside a
  running event loop).
- Every function catches all exceptions and falls back to mock data — a
  permissions gap or missing SDK never crashes an agent; it just reduces
  data fidelity and logs a warning.
- Functions accept resource IDs and resource group names as parameters —
  they are NOT hard-coded to sentinel-prod-rg.  They work in ANY Azure
  environment.

Usage (inside an agent's ``@af.tool`` function)::

    from src.infrastructure.azure_tools import (
        query_resource_graph,
        query_metrics,
        get_resource_details,
        query_activity_log,
        list_nsg_rules,
    )

    @af.tool(name="check_cpu", description="Get CPU metrics for a VM")
    def check_cpu(resource_id: str) -> str:
        data = query_metrics(resource_id, ["Percentage CPU"], "P7D")
        return json.dumps(data)
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SEED_PATH = Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
_seed_cache: dict | None = None


def _seed() -> dict:
    """Load seed_resources.json (cached after first read)."""
    global _seed_cache
    if _seed_cache is None:
        with open(_SEED_PATH, encoding="utf-8") as fh:
            _seed_cache = json.load(fh)
    return _seed_cache


def _use_mocks() -> bool:
    """Return True when local mock mode is active."""
    from src.config import settings
    return settings.use_local_mocks


# ---------------------------------------------------------------------------
# 1. query_resource_graph
# ---------------------------------------------------------------------------


def query_resource_graph(kusto_query: str, subscription_id: str = "") -> list[dict]:
    """Query Azure Resource Graph with a Kusto query.

    Discovers resources across an Azure subscription using KQL — the same
    query language as Azure Monitor and Log Analytics.

    Example queries::

        "Resources | where type == 'microsoft.compute/virtualmachines'"
        "Resources | where resourceGroup == 'sentinel-prod-rg'"
        "Resources | where tags['environment'] == 'production'"

    Args:
        kusto_query: KQL query string.
        subscription_id: Azure subscription to scope the query to.
            Defaults to ``AZURE_SUBSCRIPTION_ID`` from settings.

    Returns:
        List of resource dicts in Azure Resource Graph format.
        Each dict includes: id, name, type, location, resourceGroup,
        subscriptionId, tags, properties.
    """
    if not _use_mocks():
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.resourcegraph import ResourceGraphClient
            from azure.mgmt.resourcegraph.models import QueryRequest
            from src.config import settings

            sub_id = subscription_id or settings.azure_subscription_id
            credential = DefaultAzureCredential()
            client = ResourceGraphClient(credential)
            request = QueryRequest(
                subscriptions=[sub_id] if sub_id else [],
                query=kusto_query,
            )
            result = client.resources(request)
            return [dict(row) for row in (result.data or [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "azure_tools.query_resource_graph: Azure call failed (%s) — using mock.", exc
            )

    return _mock_query_resource_graph(kusto_query)


def _mock_query_resource_graph(kusto_query: str) -> list[dict]:
    """Return seed resources filtered by type and resource-group hints in the KQL.

    This is an approximate parser — it extracts type keywords and an optional
    ``resourceGroup`` equality filter from the Kusto query string.  It does not
    implement full KQL semantics; complex joins, projections, and predicates are
    silently ignored.  Live mode uses the real Resource Graph API instead.
    """
    resources = _seed().get("resources", [])
    query_lower = kusto_query.lower()

    # --- Resource-type filter (keyword matching) ---
    # Map Kusto type substrings → canonical Azure resource type strings.
    _TYPE_MAP: list[tuple[str, str]] = [
        ("virtualmachines",         "Microsoft.Compute/virtualMachines"),
        ("networksecuritygroups",   "Microsoft.Network/networkSecurityGroups"),
        ("managedclusters",         "Microsoft.ContainerService/managedClusters"),
        ("storageaccounts",         "Microsoft.Storage/storageAccounts"),
        ("serverfarms",             "Microsoft.Web/serverFarms"),
        ("sites",                   "Microsoft.Web/sites"),
        ("servers/databases",       "Microsoft.Sql/servers/databases"),
        ("databaseaccounts",        "Microsoft.DocumentDB/databaseAccounts"),
        ("registries",              "Microsoft.ContainerRegistry/registries"),
        ("azurefirewalls",          "Microsoft.Network/azureFirewalls"),
        ("workspaces",              "Microsoft.OperationalInsights/workspaces"),
    ]
    type_filters: list[str] = [t for kw, t in _TYPE_MAP if kw in query_lower]
    if type_filters:
        resources = [r for r in resources if r.get("type") in type_filters]

    # --- Resource-group filter (simple equality / =~ extraction) ---
    # Handles patterns: resourceGroup == 'rg-name'  or  resourceGroup =~ 'rg-name'
    import re as _re
    rg_match = _re.search(
        r"resourcegroup\s*=~?\s*'([^']+)'",
        query_lower,
    )
    if rg_match:
        target_rg = rg_match.group(1).lower()
        seed_meta = _seed()
        resources = [
            r for r in resources
            if (r.get("resource_group") or seed_meta.get("resource_group", "")).lower()
            == target_rg
        ]

    seed_meta = _seed()
    result: list[dict] = []
    for r in resources:
        if "_comment" in r:
            continue
        result.append({
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "type": r.get("type", ""),
            "location": r.get("location", ""),
            "resourceGroup": r.get("resource_group", seed_meta.get("resource_group", "")),
            "subscriptionId": seed_meta.get("subscription_id", ""),
            "tags": r.get("tags", {}),
            "sku": {"name": r.get("sku", "")},
            "monthly_cost": r.get("monthly_cost"),
            "node_count": r.get("node_count"),
            "properties": {
                "securityRules": r.get("rules", []),
            },
        })
    return result


# ---------------------------------------------------------------------------
# 2. query_metrics
# ---------------------------------------------------------------------------


def query_metrics(
    resource_id: str,
    metric_names: list[str],
    timespan: str = "PT24H",
) -> dict:
    """Query Azure Monitor metrics for a specific resource.

    Args:
        resource_id: Full Azure ARM resource ID.
        metric_names: List of Azure Monitor metric names to retrieve.
            Common VM metrics: ``["Percentage CPU", "Network In", "Network Out"]``.
            AKS: ``["node_cpu_usage_percentage", "node_memory_rss_percentage"]``.
        timespan: ISO 8601 duration string.
            ``"PT24H"`` = last 24 hours.
            ``"P7D"``   = last 7 days.
            ``"P30D"``  = last 30 days.

    Returns:
        Dict with structure::

            {
                "resource_id": "...",
                "timespan": "P7D",
                "metrics": {
                    "Percentage CPU": {
                        "average": 12.5,
                        "maximum": 35.2,
                        "minimum": 2.1,
                        "unit": "Percent"
                    }
                }
            }
    """
    if not _use_mocks():
        try:
            from azure.identity import DefaultAzureCredential
            from azure.monitor.query import MetricsQueryClient

            credential = DefaultAzureCredential()
            client = MetricsQueryClient(credential)
            result = client.query_resource(
                resource_uri=resource_id,
                metric_names=metric_names,
                timespan=_parse_duration(timespan),
            )

            output: dict = {"resource_id": resource_id, "timespan": timespan, "metrics": {}}
            for metric in result.metrics:
                values: list[float] = []
                for ts in metric.timeseries:
                    for dp in ts.data:
                        if dp.average is not None:
                            values.append(dp.average)
                if values:
                    output["metrics"][metric.name] = {
                        "average": round(sum(values) / len(values), 2),
                        "maximum": round(max(values), 2),
                        "minimum": round(min(values), 2),
                        "unit": metric.unit.value if metric.unit else "unknown",
                    }
            return output
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "azure_tools.query_metrics: Azure call failed (%s) — using mock.", exc
            )

    return _mock_query_metrics(resource_id, metric_names, timespan)


def _mock_query_metrics(
    resource_id: str, metric_names: list[str], timespan: str
) -> dict:
    """Return realistic mock metric values based on resource name patterns."""
    name_lower = resource_id.lower()

    # Assign a CPU profile based on what we know about each resource.
    if "dr-01" in name_lower or "dr-failover" in name_lower or "backup" in name_lower:
        # DR VM: nearly idle — a prime cost-saving candidate
        cpu_avg, cpu_max, cpu_min = 3.2, 14.8, 0.5
    elif "web-01" in name_lower or "web-tier" in name_lower:
        # Web VM: high load from stress-ng cron job
        cpu_avg, cpu_max, cpu_min = 82.5, 100.0, 45.2
    elif "api" in name_lower:
        # API server: moderate
        cpu_avg, cpu_max, cpu_min = 18.3, 42.1, 5.0
    elif "aks" in name_lower:
        # AKS cluster: moderate
        cpu_avg, cpu_max, cpu_min = 45.7, 78.3, 22.1
    elif "vm-23" in name_lower:
        # Legacy demo DR VM: idle
        cpu_avg, cpu_max, cpu_min = 4.1, 18.5, 0.8
    else:
        # Unknown resource: return a moderate "busy" baseline so that unknown
        # resources do NOT trigger false-positive right-sizing proposals in
        # mock mode.  The 20 % threshold for right-sizing would fire on the
        # old default (20 % avg); 50 % is safely above that threshold.
        cpu_avg, cpu_max, cpu_min = 50.0, 75.0, 20.0

    metrics: dict = {}
    for metric_name in metric_names:
        ml = metric_name.lower()
        if "cpu" in ml:
            metrics[metric_name] = {
                "average": cpu_avg, "maximum": cpu_max, "minimum": cpu_min, "unit": "Percent",
            }
        elif "network in" in ml:
            metrics[metric_name] = {
                "average": 1_245_000.0, "maximum": 8_320_000.0, "minimum": 0.0, "unit": "Bytes",
            }
        elif "network out" in ml:
            metrics[metric_name] = {
                "average": 892_000.0, "maximum": 4_210_000.0, "minimum": 0.0, "unit": "Bytes",
            }
        elif "disk" in ml:
            metrics[metric_name] = {
                "average": 12.4, "maximum": 45.2, "minimum": 0.5, "unit": "MBps",
            }
        elif "memory" in ml:
            metrics[metric_name] = {
                "average": 65.0, "maximum": 89.5, "minimum": 40.2, "unit": "Percent",
            }
        else:
            metrics[metric_name] = {
                "average": 20.0, "maximum": 45.0, "minimum": 5.0, "unit": "Count",
            }

    return {"resource_id": resource_id, "timespan": timespan, "metrics": metrics}


# ---------------------------------------------------------------------------
# 3. get_resource_details
# ---------------------------------------------------------------------------


def get_resource_details(resource_id: str) -> dict:
    """Get full details for a specific Azure resource by its ARM resource ID.

    Uses Resource Graph so it works for any resource type without needing
    separate management SDK packages.

    Args:
        resource_id: Full Azure ARM resource ID or short name.

    Returns:
        Resource detail dict. Empty dict if not found.
    """
    # Delegate to query_resource_graph (which handles live/mock routing).
    safe_id = resource_id.replace("'", "''")
    results = query_resource_graph(f"Resources | where id =~ '{safe_id}'")
    if results:
        return results[0]

    # Secondary match: search by name (handles short names like "vm-web-01").
    name = resource_id.split("/")[-1] if "/" in resource_id else resource_id
    for r in _seed().get("resources", []):
        if r.get("name", "").lower() == name.lower():
            return r

    return {}


# ---------------------------------------------------------------------------
# 4. query_activity_log
# ---------------------------------------------------------------------------


def query_activity_log(resource_group: str, timespan: str = "P7D") -> list[dict]:
    """Query Azure Monitor activity logs for a resource group.

    Uses the Log Analytics workspace (``LOG_ANALYTICS_WORKSPACE_ID``) with
    a KQL query against the ``AzureActivity`` table.  Requires that activity
    logs have been streamed to the workspace via a Diagnostic Setting.

    Args:
        resource_group: Azure resource group name to filter logs for.
        timespan: ISO 8601 duration (e.g. ``"P7D"`` = last 7 days).

    Returns:
        List of activity log entry dicts, newest-first.  Each dict has:
        timestamp, operation, status, caller, resource_type, resource, level.
    """
    if not _use_mocks():
        try:
            from azure.identity import DefaultAzureCredential
            from azure.monitor.query import LogsQueryClient, LogsQueryStatus
            from src.config import settings

            if not settings.log_analytics_workspace_id:
                logger.warning(
                    "azure_tools.query_activity_log: LOG_ANALYTICS_WORKSPACE_ID not set — mock."
                )
                return _mock_activity_log(resource_group)

            credential = DefaultAzureCredential()
            client = LogsQueryClient(credential)
            kql = (
                "AzureActivity "
                f"| where ResourceGroup =~ '{resource_group}' "
                "| order by TimeGenerated desc "
                "| take 50 "
                "| project TimeGenerated, OperationNameValue, ActivityStatusValue, "
                "Caller, ResourceType, Resource, Level"
            )
            result = client.query_workspace(
                workspace_id=settings.log_analytics_workspace_id,
                query=kql,
                timespan=_parse_duration(timespan),
            )
            if result.status == LogsQueryStatus.SUCCESS:
                rows: list[dict] = []
                for row in result.table.rows:
                    rows.append({
                        "timestamp": str(row[0]),
                        "operation": str(row[1]),
                        "status": str(row[2]),
                        "caller": str(row[3]),
                        "resource_type": str(row[4]),
                        "resource": str(row[5]),
                        "level": str(row[6]),
                    })
                return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "azure_tools.query_activity_log: Azure call failed (%s) — using mock.", exc
            )

    return _mock_activity_log(resource_group)


def _mock_activity_log(resource_group: str) -> list[dict]:
    """Return realistic fabricated activity log entries scoped to the given resource group.

    Entries use the resource_group parameter so they are generic and do not
    reference resources specific to any one environment.  The caller field uses
    a placeholder domain so no real identity is implied.
    """
    now = datetime.now(timezone.utc)
    rg = resource_group or "unknown-rg"
    # Derive plausible short names from the resource group name rather than
    # hardcoding environment-specific resource names.
    rg_short = rg.split("-rg")[0] if "-rg" in rg else rg
    vm_name  = f"vm-{rg_short}"
    nsg_name = f"nsg-{rg_short}"
    sa_name  = f"sa{rg_short.replace('-', '')}"[:24]  # storage account name constraints

    return [
        {
            "timestamp": (now - timedelta(hours=2)).isoformat(),
            "operation": "Microsoft.Compute/virtualMachines/start/action",
            "status": "Succeeded",
            "caller": "automation-principal@org.example.com",
            "resource_type": "Microsoft.Compute/virtualMachines",
            "resource": vm_name,
            "level": "Informational",
        },
        {
            "timestamp": (now - timedelta(hours=6)).isoformat(),
            "operation": "Microsoft.Network/networkSecurityGroups/securityRules/write",
            "status": "Succeeded",
            "caller": "automation-principal@org.example.com",
            "resource_type": "Microsoft.Network/networkSecurityGroups",
            "resource": nsg_name,
            "level": "Informational",
        },
        {
            "timestamp": (now - timedelta(days=1)).isoformat(),
            "operation": "Microsoft.Compute/virtualMachines/extensions/write",
            "status": "Succeeded",
            "caller": "automation-principal@org.example.com",
            "resource_type": "Microsoft.Compute/virtualMachines",
            "resource": vm_name,
            "level": "Informational",
        },
        {
            "timestamp": (now - timedelta(days=2)).isoformat(),
            "operation": "Microsoft.Storage/storageAccounts/write",
            "status": "Succeeded",
            "caller": "automation-principal@org.example.com",
            "resource_type": "Microsoft.Storage/storageAccounts",
            "resource": sa_name,
            "level": "Informational",
        },
        {
            "timestamp": (now - timedelta(days=3)).isoformat(),
            "operation": "Microsoft.Network/networkSecurityGroups/write",
            "status": "Failed",
            "caller": "ops-user@org.example.com",
            "resource_type": "Microsoft.Network/networkSecurityGroups",
            "resource": nsg_name,
            "level": "Warning",
        },
    ]


# ---------------------------------------------------------------------------
# 5. list_nsg_rules
# ---------------------------------------------------------------------------


def list_nsg_rules(nsg_resource_id: str) -> list[dict]:
    """List the security rules for an Azure Network Security Group.

    Args:
        nsg_resource_id: Full Azure ARM resource ID of the NSG,
            or a short name like ``"nsg-east-prod"``.

    Returns:
        List of security rule dicts.  Each dict typically includes:
        name, port (destinationPortRange), access (Allow/Deny),
        priority, direction.
    """
    details = get_resource_details(nsg_resource_id)

    # Resource Graph format: properties.securityRules
    props = details.get("properties", {})
    rules = props.get("securityRules", [])
    if rules:
        return rules

    # seed_resources.json format: "rules" key directly on the resource
    seed_rules = details.get("rules", [])
    if seed_rules:
        return seed_rules

    # Last resort: scan seed by name substring
    name = nsg_resource_id.split("/")[-1] if "/" in nsg_resource_id else nsg_resource_id
    for r in _seed().get("resources", []):
        if (
            r.get("name", "").lower() == name.lower()
            and "networkSecurityGroups" in r.get("type", "")
        ):
            return r.get("rules", [])

    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_duration(iso_duration: str) -> timedelta:
    """Parse a simple ISO 8601 duration string to :class:`datetime.timedelta`.

    Supports the subset used by Azure Monitor:
    - ``PTnH``  — n hours (e.g. ``PT24H``)
    - ``PnD``   — n days  (e.g. ``P7D``)
    - ``PnW``   — n weeks (e.g. ``P2W``)
    - ``PnM``   — n months, approximated as 30 days each
    """
    s = iso_duration.upper().lstrip("P")
    if "T" in s:
        _, time_part = s.split("T", 1)
        if "H" in time_part:
            return timedelta(hours=float(time_part.rstrip("H")))
    else:
        if "D" in s:
            return timedelta(days=float(s.rstrip("D")))
        if "W" in s:
            return timedelta(weeks=float(s.rstrip("W")))
        if "M" in s:
            return timedelta(days=float(s.rstrip("M")) * 30)
    return timedelta(hours=24)
