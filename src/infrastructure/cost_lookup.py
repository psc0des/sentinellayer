"""Azure Retail Prices API — SKU-based monthly cost estimation.

No authentication required.  Uses the public Azure Retail Prices REST API
(https://prices.azure.com/api/retail/prices).

Falls back gracefully to None on any network or parsing error — governance
decisions still work correctly when cost data is unavailable; they simply
treat the resource's monthly cost as unknown.

Usage::

    from src.infrastructure.cost_lookup import get_sku_monthly_cost

    cost = get_sku_monthly_cost("Standard_B2ls_v2", "canadacentral")
    # Returns e.g. 36.47 (USD/month) or None if not found / API unreachable.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

# In-memory cache keyed by "<sku_lower>::<location_lower>".
# Persists for the process lifetime — one lookup per (sku, region) pair.
_cache: dict[str, float | None] = {}

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"


def get_sku_monthly_cost(sku: str, location: str) -> float | None:
    """Return estimated monthly USD cost for a given Azure SKU and region.

    Queries the Azure Retail Prices REST API for the Consumption retail price
    of the SKU, then multiplies by 730 hours (average hours in a month).

    Results are cached in memory so each (sku, region) combination is only
    fetched once per process run.

    Args:
        sku:      Azure VM SKU name, e.g. ``"Standard_B2ls_v2"``.
        location: Azure region ARM name, e.g. ``"canadacentral"``.

    Returns:
        Estimated monthly cost in USD (rounded to 2 decimal places),
        or ``None`` if the SKU is unknown or the API call fails.
    """
    if not sku or not location:
        return None

    key = f"{sku.lower()}::{location.lower()}"
    if key in _cache:
        return _cache[key]

    try:
        filter_str = (
            f"armRegionName eq '{location}' "
            f"and armSkuName eq '{sku}' "
            f"and priceType eq 'Consumption' "
            f"and type eq 'Consumption'"
        )
        resp = httpx.get(
            RETAIL_PRICES_URL,
            params={"$filter": filter_str},
            timeout=5.0,
        )
        resp.raise_for_status()
        items = resp.json().get("Items", [])

        # Pick the lowest non-zero retail price (Linux / base tier is cheapest)
        prices = [i["retailPrice"] for i in items if i.get("retailPrice", 0) > 0]
        if prices:
            monthly = round(min(prices) * 730, 2)
            _cache[key] = monthly
            return monthly

    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "cost_lookup: failed for sku=%s location=%s: %s", sku, location, exc
        )

    _cache[key] = None
    return None
