"""Azure Resource Graph client — resource topology and dependency queries.

Mode selection
--------------
Mock mode (USE_LOCAL_MOCKS=true, or subscription ID not set):
    Loads the resource topology from ``data/seed_resources.json`` and
    answers dependency / tag queries entirely in memory.
    This is what all existing governance agents use today.

Azure mode (USE_LOCAL_MOCKS=false + AZURE_SUBSCRIPTION_ID set):
    Uses ``azure-mgmt-resourcegraph`` with ``DefaultAzureCredential``
    to query the real Azure Resource Graph API using KQL (Kusto Query
    Language).  Dependency edges are not natively exposed by Resource
    Graph, so they are returned empty — a future enhancement would store
    custom dependency data in Cosmos DB or tags.

Usage::

    from src.infrastructure.resource_graph import ResourceGraphClient

    client = ResourceGraphClient()
    resource = client.get_resource("vm-23")
    print(resource["tags"])          # {"purpose": "disaster-recovery", ...}
    print(client.get_dependents("vm-23"))  # ["dr-failover-service", ...]
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings

logger = logging.getLogger(__name__)

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)


class ResourceGraphClient:
    """Query Azure resource topology from Resource Graph or local JSON seed data.

    Args:
        cfg: Settings object (defaults to module singleton from ``src.config``).
        resources_path: Override the local JSON path (used in tests).
    """

    def __init__(self, cfg=None, resources_path: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._resources_path: Path = resources_path or _DEFAULT_RESOURCES_PATH

        # Use mock if flag is set OR if subscription ID is absent.
        # The subscription ID is required to make Resource Graph API calls.
        self._is_mock: bool = (
            self._cfg.use_local_mocks or not self._cfg.azure_subscription_id
        )

        if self._is_mock:
            logger.info(
                "ResourceGraphClient: LOCAL MOCK mode (JSON at %s).", self._resources_path
            )
            self._resources: dict[str, dict] = self._load_local_resources()
            self._rg_client = None
        else:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
            from azure.mgmt.resourcegraph import ResourceGraphClient as AzRGClient  # type: ignore[import]

            credential = DefaultAzureCredential()
            self._rg_client = AzRGClient(credential)
            self._resources = {}
            logger.info(
                "ResourceGraphClient: connected (subscription=%s)",
                self._cfg.azure_subscription_id,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_resource(self, resource_id: str) -> dict | None:
        """Look up a resource by short name or full Azure resource ID.

        Tries an exact name match first, then falls back to the last
        path segment of a full Azure ID (the part after the final ``/``).

        Args:
            resource_id: Short name (e.g. ``"vm-23"``) or full Azure ID
                (e.g. ``"/subscriptions/.../virtualMachines/vm-23"``).

        Returns:
            Resource dict (with ``name``, ``type``, ``tags``,
            ``dependencies``, ``dependents``, etc.) or ``None`` if
            the resource is not found.
        """
        if self._is_mock:
            return self._mock_find(resource_id)
        return self._azure_get_resource(resource_id)

    def get_dependencies(self, resource_id: str) -> list[str]:
        """Return names of resources that the given resource depends on.

        Args:
            resource_id: Short name or full Azure resource ID.

        Returns:
            List of dependency resource names.  Empty if not found or
            if running in Azure mode (dependency edges not yet stored).
        """
        resource = self.get_resource(resource_id)
        if resource is None:
            return []
        return resource.get("dependencies", [])

    def get_dependents(self, resource_id: str) -> list[str]:
        """Return names of resources that depend on the given resource.

        Args:
            resource_id: Short name or full Azure resource ID.

        Returns:
            List of dependent resource names.  Empty if not found.
        """
        resource = self.get_resource(resource_id)
        if resource is None:
            return []
        return resource.get("dependents", [])

    def list_all(self) -> list[dict]:
        """Return all known resources in the topology.

        Returns:
            List of resource dicts.  In mock mode, returns all resources
            from the seed file.  In Azure mode, queries Resource Graph
            (up to 1 000 resources).
        """
        if self._is_mock:
            return list(self._resources.values())
        return self._azure_list_all()

    @property
    def is_mock(self) -> bool:
        """True if this client is running in local mock mode."""
        return self._is_mock

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    def _load_local_resources(self) -> dict[str, dict]:
        """Load seed_resources.json and index by resource name."""
        try:
            with open(self._resources_path, encoding="utf-8") as fh:
                data: dict = json.load(fh)
            return {r["name"]: r for r in data.get("resources", [])}
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("ResourceGraphClient(mock): cannot load resources: %s", exc)
            return {}

    def _mock_find(self, resource_id: str) -> dict | None:
        """Exact name match, then last-segment fallback for full Azure IDs."""
        if resource_id in self._resources:
            return self._resources[resource_id]
        # Last segment of "/subscriptions/.../providers/.../vm-23" is "vm-23"
        name = resource_id.split("/")[-1]
        return self._resources.get(name)

    # ------------------------------------------------------------------
    # Azure Resource Graph helpers
    # ------------------------------------------------------------------

    def _azure_get_resource(self, resource_id: str) -> dict | None:
        """Query Resource Graph for a single resource by name, then enrich topology."""
        from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]

        name = resource_id.split("/")[-1]
        kql = (
            f"Resources"
            f" | where name =~ '{name}'"
            f" | project id, name, type, location, tags, sku, resourceGroup"
        )
        request = QueryRequest(
            subscriptions=[self._cfg.azure_subscription_id],
            query=kql,
        )
        try:
            response = self._rg_client.resources(request)
            if response.data:
                row = response.data[0]
                resource = {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "location": row.get("location"),
                    "tags": row.get("tags") or {},
                    "sku": row.get("sku") or {},
                    "resource_group": row.get("resourceGroup", ""),
                }
                return self._azure_enrich_topology(resource)
        except Exception as exc:  # noqa: BLE001
            logger.error("ResourceGraphClient: Azure query failed: %s", exc)
        return None

    def _azure_list_all(self) -> list[dict]:
        """Query Resource Graph for all resources in the subscription (max 1 000)."""
        from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]

        kql = (
            "Resources"
            " | project id, name, type, location, tags, sku, resourceGroup"
            " | limit 1000"
        )
        request = QueryRequest(
            subscriptions=[self._cfg.azure_subscription_id],
            query=kql,
        )
        try:
            response = self._rg_client.resources(request)
            results = []
            for row in (response.data or []):
                resource = {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "location": row.get("location"),
                    "tags": row.get("tags") or {},
                    "sku": row.get("sku") or {},
                    "resource_group": row.get("resourceGroup", ""),
                }
                results.append(self._azure_enrich_topology(resource))
            return results
        except Exception as exc:  # noqa: BLE001
            logger.error("ResourceGraphClient: Azure list failed: %s", exc)
            return []

    def _azure_enrich_topology(self, resource: dict) -> dict:
        """Add inferred topology fields to a live Azure resource dict.

        Runs up to 4 lightweight KQL queries per resource to infer:

        * ``dependencies`` — from ``depends-on`` tag + VM→NSG network join.
        * ``dependents`` — reverse lookup: which other resources tag
          ``depends-on`` pointing at this resource.
        * ``governs`` — from ``governs`` tag (for NSGs) + NIC→VM join for NSGs.
        * ``monthly_cost`` — from Azure Retail Prices API via ``cost_lookup``.

        All KQL queries are wrapped in individual try/except blocks so a
        single failure does not prevent the resource from being returned.

        Args:
            resource: Partially-built resource dict (must contain ``name``,
                ``type``, ``id``, ``tags``, ``resource_group``).

        Returns:
            The same dict, mutated in-place with topology fields added.
        """
        from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]
        from src.infrastructure.cost_lookup import get_sku_monthly_cost

        name = resource.get("name", "")
        tags = resource.get("tags", {})
        rg = resource.get("resource_group", "")
        rid = resource.get("id", "")
        rtype = (resource.get("type") or "").lower()

        # ── 1. depends-on tag → dependencies ─────────────────────────────
        depends_on_tag = tags.get("depends-on", "")
        dependencies: list[str] = (
            [d.strip() for d in depends_on_tag.split(",") if d.strip()]
            if depends_on_tag
            else []
        )

        # ── 2. governs tag → governs ──────────────────────────────────────
        governs_tag = tags.get("governs", "")
        governs: list[str] = (
            [g.strip() for g in governs_tag.split(",") if g.strip()]
            if governs_tag
            else []
        )

        # ── 3. Network topology KQL (VMs only) → adds NSG to dependencies ─
        if "microsoft.compute/virtualmachines" in rtype and name:
            try:
                kql = f"""
Resources
| where name =~ '{name}'
| extend nicIds = properties.networkProfile.networkInterfaces
| mv-expand nic = nicIds
| extend nicId = tolower(tostring(nic.id))
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.network/networkinterfaces'
    | extend nsgId = tolower(tostring(properties.networkSecurityGroup.id))
    | project id, nsgId
) on $left.nicId == $right.id
| extend nsgName = tostring(split(nsgId, '/')[8])
| where isnotempty(nsgName)
| project nsgName
"""
                req = QueryRequest(
                    subscriptions=[self._cfg.azure_subscription_id], query=kql
                )
                resp = self._rg_client.resources(req)
                for row in (resp.data or []):
                    nsg_name = row.get("nsgName", "")
                    if nsg_name and nsg_name not in dependencies:
                        dependencies.append(nsg_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("enrich_topology: NSG KQL failed for %s: %s", name, exc)

        # ── 4. NSG governs — which VMs sit behind this NSG ────────────────
        if "microsoft.network/networksecuritygroups" in rtype and rid:
            try:
                kql = f"""
Resources
| where type =~ 'microsoft.network/networkinterfaces'
| where resourceGroup =~ '{rg}'
| where properties.networkSecurityGroup.id =~ '{rid}'
| extend vmId = tostring(properties.virtualMachine.id)
| where isnotempty(vmId)
| extend vmName = tostring(split(vmId, '/')[8])
| project vmName
"""
                req = QueryRequest(
                    subscriptions=[self._cfg.azure_subscription_id], query=kql
                )
                resp = self._rg_client.resources(req)
                for row in (resp.data or []):
                    vm_name = row.get("vmName", "")
                    if vm_name and vm_name not in governs:
                        governs.append(vm_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("enrich_topology: NSG-governs KQL failed for %s: %s", name, exc)

        # ── 5. Reverse lookup → dependents (who tags depends-on: {name}) ──
        dependents: list[str] = []
        if name and rg:
            try:
                kql = f"""
Resources
| where resourceGroup =~ '{rg}'
| where tags['depends-on'] contains '{name}'
| project name
"""
                req = QueryRequest(
                    subscriptions=[self._cfg.azure_subscription_id], query=kql
                )
                resp = self._rg_client.resources(req)
                for row in (resp.data or []):
                    dep_name = row.get("name", "")
                    if dep_name and dep_name != name:
                        dependents.append(dep_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "enrich_topology: reverse-lookup KQL failed for %s: %s", name, exc
                )

        # ── 6. Monthly cost from Azure Retail Prices API ──────────────────
        sku_name = (resource.get("sku") or {}).get("name", "")
        location = resource.get("location", "")
        monthly_cost = get_sku_monthly_cost(sku_name, location)

        resource.update(
            {
                "dependencies": dependencies,
                "dependents": dependents,
                "governs": governs,
                "services_hosted": [],
                "consumers": [],
                "monthly_cost": monthly_cost,
            }
        )
        return resource
