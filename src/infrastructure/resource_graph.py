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
    Language).  Dependency edges are populated via ``depends-on`` /
    ``governs`` tag parsing and VM→NIC→NSG network topology KQL queries
    (added in Phase 19 via ``_azure_enrich_topology``).

    Async variants (Phase 20) use ``azure.mgmt.resourcegraph.aio`` and
    ``asyncio.gather()`` to run the 4 topology KQL queries concurrently,
    reducing per-resource enrichment from ~1,100ms (sequential) to ~300ms.

Usage::

    from src.infrastructure.resource_graph import ResourceGraphClient

    client = ResourceGraphClient()
    resource = client.get_resource("vm-23")
    print(resource["tags"])          # {"purpose": "disaster-recovery", ...}
    print(client.get_dependents("vm-23"))  # ["dr-failover-service", ...]

Async usage (non-blocking, for use inside async @af.tool callbacks)::

    resource = await client.get_resource_async("vm-23")
"""

import asyncio
import json
import logging
from pathlib import Path

from src.config import settings as _default_settings

logger = logging.getLogger(__name__)


def _kql_escape(value: str) -> str:
    """Escape a string value for safe interpolation into a KQL string literal.

    KQL string delimiters are single quotes; a literal single quote is escaped
    by doubling it (``'`` → ``''``).  Use this for every user- or
    data-derived value interpolated into a KQL query to prevent query breakage
    or injection from resource names / IDs containing special characters.
    """
    return value.replace("'", "''")

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)


async def query_resources_async(kql: str) -> list[dict]:
    """Execute a raw KQL query against Azure Resource Graph (async).

    In mock mode returns an empty list (no real API to call).
    In Azure mode executes the KQL and returns the raw result rows.
    Raises on authentication or network errors so callers can surface the issue.
    """
    client = ResourceGraphClient()
    if client.is_mock:
        return []
    from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]
    request = QueryRequest(
        subscriptions=[client._cfg.azure_subscription_id],
        query=kql,
    )
    response = await client._async_rg_client.resources(request)
    return list(response.data or [])


class ResourceGraphClient:
    """Query Azure resource topology from Resource Graph or local JSON seed data.

    Provides both synchronous and asynchronous APIs:

    * Sync methods (``get_resource``, ``list_all``, etc.) — used by the
      non-framework evaluation path and ``asyncio.to_thread()`` callers.
    * Async methods (``get_resource_async``, ``list_all_async``, etc.) —
      used by ``async def`` ``@af.tool`` callbacks inside the Microsoft Agent
      Framework.  The async enrichment method runs 4 KQL queries concurrently
      via ``asyncio.gather()`` for a ~3–4× throughput improvement.

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
            self._async_rg_client = None
        else:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
            from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential  # type: ignore[import]
            from azure.mgmt.resourcegraph import ResourceGraphClient as AzRGClient  # type: ignore[import]
            from azure.mgmt.resourcegraph.aio import ResourceGraphClient as AsyncAzRGClient  # type: ignore[import]

            credential = DefaultAzureCredential()
            self._rg_client = AzRGClient(credential)
            # Async client requires an AsyncTokenCredential — use the .aio variant.
            self._async_rg_client = AsyncAzRGClient(AsyncDefaultAzureCredential())
            self._resources = {}
            logger.info(
                "ResourceGraphClient: connected (subscription=%s)",
                self._cfg.azure_subscription_id,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the async Azure SDK client and release its connection pool.

        Call this when the ResourceGraphClient is no longer needed (e.g., at
        application shutdown) to avoid ``ResourceWarning: unclosed client session``
        log noise from the underlying aiohttp/httpx transport.
        """
        if self._async_rg_client is not None:
            await self._async_rg_client.close()
            self._async_rg_client = None

    # ------------------------------------------------------------------
    # Public API — synchronous
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
            List of dependency resource names.  Empty if not found.
            In Azure mode, dependencies are populated from ``depends-on``
            tags and VM→NIC→NSG network joins by ``_azure_enrich_topology``.
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
    # Public API — asynchronous (Phase 20)
    # ------------------------------------------------------------------

    async def get_resource_async(self, resource_id: str) -> dict | None:
        """Async variant of :meth:`get_resource` — non-blocking Azure API calls.

        In mock mode, returns the same result as the sync variant instantly
        (in-memory dict lookup — no I/O).  In Azure mode, uses the async
        Resource Graph client and runs topology enrichment concurrently.

        Args:
            resource_id: Short name or full Azure resource ID.

        Returns:
            Resource dict or ``None`` if not found.
        """
        if self._is_mock:
            return self._mock_find(resource_id)
        return await self._azure_get_resource_async(resource_id)

    async def list_all_async(self) -> list[dict]:
        """Async variant of :meth:`list_all` — non-blocking Azure API calls.

        Returns:
            List of resource dicts.
        """
        if self._is_mock:
            return list(self._resources.values())
        return await self._azure_list_all_async()

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
    # Azure Resource Graph helpers — synchronous
    # ------------------------------------------------------------------

    def _azure_get_resource(self, resource_id: str) -> dict | None:
        """Query Resource Graph for a single resource, then enrich topology.

        When a full ARM resource ID is supplied (starts with ``/`` and
        contains ``/providers/``), the query filters on the exact ``id``
        field, avoiding false matches when multiple resources share a name
        across resource groups or subscriptions.

        For short names the query filters on ``name`` (case-insensitive).
        """
        from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]

        is_arm_id = resource_id.startswith("/") and "/providers/" in resource_id
        if is_arm_id:
            kql = (
                "Resources"
                f" | where id =~ '{_kql_escape(resource_id)}'"
                " | extend osType = tostring(properties.storageProfile.osDisk.osType)"
                " | project id, name, type, location, tags, sku, resourceGroup, osType"
            )
        else:
            name = resource_id.split("/")[-1]
            kql = (
                "Resources"
                f" | where name =~ '{_kql_escape(name)}'"
                " | extend osType = tostring(properties.storageProfile.osDisk.osType)"
                " | project id, name, type, location, tags, sku, resourceGroup, osType"
            )
        request = QueryRequest(
            subscriptions=[self._cfg.azure_subscription_id],
            query=kql,
        )
        try:
            response = self._rg_client.resources(request)
            if response.data:
                if not is_arm_id and len(response.data) > 1:
                    # Multiple resources share this short name across resource
                    # groups or types.  We proceed with the first result, but
                    # callers should use full ARM IDs to avoid ambiguity.
                    logger.warning(
                        "ResourceGraphClient: short-name lookup for %r matched %d resources"
                        " — using first result; provide a full ARM ID for a deterministic"
                        " lookup.",
                        resource_id,
                        len(response.data),
                    )
                row = response.data[0]
                resource = {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "location": row.get("location"),
                    "tags": row.get("tags") or {},
                    "sku": row.get("sku") or {},
                    "resource_group": row.get("resourceGroup", ""),
                    # "Windows" | "Linux" for VMs; "" for all other resource types
                    "os_type": row.get("osType", ""),
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
            " | extend osType = tostring(properties.storageProfile.osDisk.osType)"
            " | project id, name, type, location, tags, sku, resourceGroup, osType"
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
                    "os_type": row.get("osType", ""),
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
        tags = resource.get("tags") or {}
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
| where name =~ '{_kql_escape(name)}'
| extend nicIds = properties.networkProfile.networkInterfaces
| mv-expand nic = nicIds
| extend nicId = tolower(tostring(nic.id))
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.network/networkinterfaces'
    | extend nsgId = tolower(tostring(properties.networkSecurityGroup.id))
    | project id=tolower(id), nsgId
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
| where resourceGroup =~ '{_kql_escape(rg)}'
| where properties.networkSecurityGroup.id =~ '{_kql_escape(rid)}'
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
        if name:
            try:
                # Subscription-wide reverse lookup: find every resource in any
                # resource group whose 'depends-on' tag lists this resource.
                # Scoping by RG would miss cross-RG dependents and understate
                # blast radius in multi-RG environments.
                # isnotempty() pre-filter keeps the scan cheap even at scale.
                kql = f"""
Resources
| where isnotempty(tags['depends-on'])
| extend _deps = split(tags['depends-on'], ',')
| mv-expand _dep = _deps
| where trim(' ', tostring(_dep)) =~ '{_kql_escape(name)}'
| distinct name
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
        os_type = resource.get("os_type", "")  # "Windows" | "Linux" | ""
        monthly_cost = get_sku_monthly_cost(sku_name, location, os_type=os_type)

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

    # ------------------------------------------------------------------
    # Azure Resource Graph helpers — asynchronous (Phase 20)
    # ------------------------------------------------------------------

    async def _azure_get_resource_async(self, resource_id: str) -> dict | None:
        """Async version of :meth:`_azure_get_resource`.

        Uses the async Resource Graph client so the event loop is not blocked
        while waiting for the Azure API response.
        """
        from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]

        is_arm_id = resource_id.startswith("/") and "/providers/" in resource_id
        if is_arm_id:
            kql = (
                "Resources"
                f" | where id =~ '{_kql_escape(resource_id)}'"
                " | extend osType = tostring(properties.storageProfile.osDisk.osType)"
                " | project id, name, type, location, tags, sku, resourceGroup, osType"
            )
        else:
            name = resource_id.split("/")[-1]
            kql = (
                "Resources"
                f" | where name =~ '{_kql_escape(name)}'"
                " | extend osType = tostring(properties.storageProfile.osDisk.osType)"
                " | project id, name, type, location, tags, sku, resourceGroup, osType"
            )
        request = QueryRequest(
            subscriptions=[self._cfg.azure_subscription_id],
            query=kql,
        )
        try:
            response = await self._async_rg_client.resources(request)
            if response.data:
                if not is_arm_id and len(response.data) > 1:
                    logger.warning(
                        "ResourceGraphClient(async): short-name lookup for %r matched %d resources"
                        " — using first result; provide a full ARM ID for a deterministic lookup.",
                        resource_id,
                        len(response.data),
                    )
                row = response.data[0]
                resource = {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "location": row.get("location"),
                    "tags": row.get("tags") or {},
                    "sku": row.get("sku") or {},
                    "resource_group": row.get("resourceGroup", ""),
                    "os_type": row.get("osType", ""),
                }
                return await self._azure_enrich_topology_async(resource)
        except Exception as exc:  # noqa: BLE001
            logger.error("ResourceGraphClient(async): Azure query failed: %s", exc)
        return None

    async def _azure_list_all_async(self) -> list[dict]:
        """Async version of :meth:`_azure_list_all`."""
        from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]

        kql = (
            "Resources"
            " | extend osType = tostring(properties.storageProfile.osDisk.osType)"
            " | project id, name, type, location, tags, sku, resourceGroup, osType"
            " | limit 1000"
        )
        request = QueryRequest(
            subscriptions=[self._cfg.azure_subscription_id],
            query=kql,
        )
        try:
            response = await self._async_rg_client.resources(request)
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
                    "os_type": row.get("osType", ""),
                }
                results.append(await self._azure_enrich_topology_async(resource))
            return results
        except Exception as exc:  # noqa: BLE001
            logger.error("ResourceGraphClient(async): Azure list failed: %s", exc)
            return []

    async def _azure_enrich_topology_async(self, resource: dict) -> dict:
        """Async version of :meth:`_azure_enrich_topology`.

        Runs 4 KQL queries **concurrently** via ``asyncio.gather()`` instead
        of sequentially.  On a typical Azure subscription with ~275ms per KQL
        round-trip, this reduces total enrichment time from ~1,100ms to ~300ms
        (the latency of the slowest individual query).

        Args:
            resource: Partially-built resource dict.

        Returns:
            The same dict mutated in-place with topology fields added.
        """
        from azure.mgmt.resourcegraph.models import QueryRequest  # type: ignore[import]
        from src.infrastructure.cost_lookup import get_sku_monthly_cost_async

        name = resource.get("name", "")
        tags = resource.get("tags") or {}
        rg = resource.get("resource_group", "")
        rid = resource.get("id", "")
        rtype = (resource.get("type") or "").lower()

        # ── Tag-based fields (instant, no I/O) ───────────────────────────
        depends_on_tag = tags.get("depends-on", "")
        dependencies: list[str] = (
            [d.strip() for d in depends_on_tag.split(",") if d.strip()]
            if depends_on_tag else []
        )
        governs_tag = tags.get("governs", "")
        governs: list[str] = (
            [g.strip() for g in governs_tag.split(",") if g.strip()]
            if governs_tag else []
        )

        # ── Concurrent KQL coroutines ─────────────────────────────────────

        async def _nsg_for_vm() -> list[str]:
            """VM → NIC → NSG network join (VMs only)."""
            if "microsoft.compute/virtualmachines" not in rtype or not name:
                return []
            kql = f"""
Resources
| where name =~ '{_kql_escape(name)}'
| extend nicIds = properties.networkProfile.networkInterfaces
| mv-expand nic = nicIds
| extend nicId = tolower(tostring(nic.id))
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.network/networkinterfaces'
    | extend nsgId = tolower(tostring(properties.networkSecurityGroup.id))
    | project id=tolower(id), nsgId
) on $left.nicId == $right.id
| extend nsgName = tostring(split(nsgId, '/')[8])
| where isnotempty(nsgName)
| project nsgName
"""
            try:
                req = QueryRequest(
                    subscriptions=[self._cfg.azure_subscription_id], query=kql
                )
                resp = await self._async_rg_client.resources(req)
                return [
                    row.get("nsgName", "")
                    for row in (resp.data or [])
                    if row.get("nsgName")
                ]
            except Exception as exc:  # noqa: BLE001
                logger.debug("enrich_topology(async): NSG KQL failed for %s: %s", name, exc)
                return []

        async def _vms_behind_nsg() -> list[str]:
            """NSG governs — which VMs sit behind this NSG (NSGs only)."""
            if "microsoft.network/networksecuritygroups" not in rtype or not rid:
                return []
            kql = f"""
Resources
| where type =~ 'microsoft.network/networkinterfaces'
| where resourceGroup =~ '{_kql_escape(rg)}'
| where properties.networkSecurityGroup.id =~ '{_kql_escape(rid)}'
| extend vmId = tostring(properties.virtualMachine.id)
| where isnotempty(vmId)
| extend vmName = tostring(split(vmId, '/')[8])
| project vmName
"""
            try:
                req = QueryRequest(
                    subscriptions=[self._cfg.azure_subscription_id], query=kql
                )
                resp = await self._async_rg_client.resources(req)
                return [
                    row.get("vmName", "")
                    for row in (resp.data or [])
                    if row.get("vmName")
                ]
            except Exception as exc:  # noqa: BLE001
                logger.debug("enrich_topology(async): NSG-governs KQL failed for %s: %s", name, exc)
                return []

        async def _reverse_dependents() -> list[str]:
            """Subscription-wide reverse lookup: who has depends-on pointing at this resource."""
            if not name:
                return []
            kql = f"""
Resources
| where isnotempty(tags['depends-on'])
| extend _deps = split(tags['depends-on'], ',')
| mv-expand _dep = _deps
| where trim(' ', tostring(_dep)) =~ '{_kql_escape(name)}'
| distinct name
"""
            try:
                req = QueryRequest(
                    subscriptions=[self._cfg.azure_subscription_id], query=kql
                )
                resp = await self._async_rg_client.resources(req)
                return [
                    row.get("name", "")
                    for row in (resp.data or [])
                    if row.get("name") and row.get("name") != name
                ]
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "enrich_topology(async): reverse-lookup failed for %s: %s", name, exc
                )
                return []

        async def _get_cost() -> float | None:
            """Monthly cost from Azure Retail Prices API (non-blocking)."""
            sku_name = (resource.get("sku") or {}).get("name", "")
            location = resource.get("location", "")
            os_type = resource.get("os_type", "")
            return await get_sku_monthly_cost_async(sku_name, location, os_type=os_type)

        # ── Run all 4 queries concurrently ────────────────────────────────
        # asyncio.gather() schedules all coroutines on the same event loop
        # and suspends until ALL complete.  Network latency for each query
        # overlaps — total wait ≈ max(individual_latencies) instead of sum.
        nsg_names, governed_vms, dependents, monthly_cost = await asyncio.gather(
            _nsg_for_vm(),
            _vms_behind_nsg(),
            _reverse_dependents(),
            _get_cost(),
        )

        # Merge KQL results into dependency / governs lists
        for nsg in nsg_names:
            if nsg not in dependencies:
                dependencies.append(nsg)
        for vm in governed_vms:
            if vm not in governs:
                governs.append(vm)

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
