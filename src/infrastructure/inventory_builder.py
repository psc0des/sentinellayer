"""Resource Inventory Builder — fetches ALL Azure resources for a subscription.

Separated from agent code so all 3 agents + the API can share one implementation.

Usage::

    from src.infrastructure.inventory_builder import build_inventory

    doc = await build_inventory(
        subscription_id="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        resource_group="rg-prod",            # optional scope
        on_progress=lambda msg: print(msg),  # optional progress callback
    )
    # doc is ready for CosmosInventoryClient.upsert()
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def build_inventory(
    subscription_id: str,
    resource_group: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Fetch ALL Azure resources and return an inventory document.

    Steps:
    1. KQL query via Resource Graph — no type filter, all resources.
    2. Group by resource type (dynamic, never hardcoded).
    3. Enrich VMs with live powerState via Compute instance view API.
    4. Return a document dict ready for Cosmos upsert.

    Args:
        subscription_id: Azure subscription to query.
        resource_group:  Optional RG to scope the query (None = whole subscription).
        on_progress:     Optional callback receiving human-readable progress strings.

    Returns:
        Inventory document dict with keys:
        id, subscription_id, refreshed_at, resource_count, type_summary, resources.

    Raises:
        RuntimeError: If the Resource Graph KQL query fails entirely.
    """

    def _progress(msg: str) -> None:
        logger.info("inventory_builder: %s", msg)
        if on_progress:
            on_progress(msg)

    # ------------------------------------------------------------------
    # Step 1: KQL — ALL resources, no type filter
    # ------------------------------------------------------------------
    _progress("Querying Azure Resource Graph for all resources…")

    kql = (
        "Resources | project id, name, type, location, resourceGroup, tags, properties, sku"
    )
    if resource_group:
        kql = (
            f"Resources | where resourceGroup =~ '{resource_group}' "
            "| project id, name, type, location, resourceGroup, tags, properties, sku"
        )

    from src.infrastructure.azure_tools import query_resource_graph_async  # noqa: PLC0415

    results = await query_resource_graph_async(kql, subscription_id=subscription_id)
    _progress(f"Resource Graph returned {len(results)} resource(s).")

    # ------------------------------------------------------------------
    # Step 2: Group by type (dynamic — no hardcoded type list)
    # ------------------------------------------------------------------
    by_type: dict[str, list[dict]] = {}
    for r in results:
        type_key = (r.get("type") or "unknown").lower()
        by_type.setdefault(type_key, []).append(r)

    # ------------------------------------------------------------------
    # Step 3: VM powerState enrichment via Compute instance view
    # ------------------------------------------------------------------
    vm_type = "microsoft.compute/virtualmachines"
    vms = by_type.get(vm_type, [])
    if vms:
        _progress(f"Enriching {len(vms)} VM(s) with live power state…")
        enriched = await _enrich_vm_power_states(vms, subscription_id, _progress)
        by_type[vm_type] = enriched
        # Rebuild flat results with enriched VMs
        results = [r for r in results if (r.get("type") or "").lower() != vm_type]
        results.extend(enriched)

    # ------------------------------------------------------------------
    # Step 4: Build summary and return document
    # ------------------------------------------------------------------
    type_summary = {t: len(rs) for t, rs in by_type.items()}
    now_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    sub_short = subscription_id.replace("-", "")[:8]

    _progress(
        f"Inventory complete — {len(results)} resources, {len(type_summary)} types."
    )

    return {
        "id": f"inv-{sub_short}-{now_ts}",
        "subscription_id": subscription_id,
        "refreshed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "resource_count": len(results),
        "type_summary": type_summary,
        "resources": results,
    }


async def _enrich_vm_power_states(
    vms: list[dict],
    subscription_id: str,
    progress: Callable[[str], None],
) -> list[dict]:
    """Enrich each VM dict with a ``powerState`` field.

    Uses ComputeManagementClient.virtual_machines.instance_view() in parallel
    (asyncio.gather). If one VM fails enrichment, it gets powerState='unknown
    (enrichment failed)' and processing continues.

    Args:
        vms:             List of VM resource dicts from Resource Graph.
        subscription_id: Subscription for the Compute client.
        progress:        Progress callback.

    Returns:
        Same list with ``powerState`` injected into each dict.
    """
    from src.config import settings  # noqa: PLC0415

    if settings.use_local_mocks:
        # In mock mode just annotate with a placeholder
        for vm in vms:
            vm.setdefault("powerState", "VM running")
        return vms

    total = len(vms)

    async def _fetch_one(i: int, vm: dict) -> dict:
        vm_out = dict(vm)
        resource_id = vm.get("id", "")
        vm_name = vm.get("name", resource_id.split("/")[-1])
        try:
            from azure.identity.aio import DefaultAzureCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415

            parts = resource_id.split("/")
            sub = parts[2] if len(parts) > 2 else subscription_id
            rg = parts[4] if len(parts) > 4 else ""

            async with DefaultAzureCredential() as cred:
                async with ComputeManagementClient(cred, sub) as compute:
                    iv = await compute.virtual_machines.instance_view(rg, vm_name)
                    for status in iv.statuses or []:
                        if (status.code or "").startswith("PowerState/"):
                            vm_out["powerState"] = status.display_status
                            break
                    else:
                        vm_out["powerState"] = "unknown"
            progress(f"Enriched VM {i}/{total}: {vm_name} → {vm_out.get('powerState')}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "inventory_builder: powerState enrichment failed for '%s' — %s",
                vm_name, exc,
            )
            vm_out["powerState"] = "unknown (enrichment failed)"
        return vm_out

    tasks = [_fetch_one(i + 1, vm) for i, vm in enumerate(vms)]
    return list(await asyncio.gather(*tasks))
