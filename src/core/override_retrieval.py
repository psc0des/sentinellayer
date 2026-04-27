"""Phase 35B — retrieve relevant past operator overrides for LLM prompt injection.

Three-tier fallback:
  Tier 1 — exact fingerprint match (same action_type, resource_type, is_production, is_critical)
  Tier 2 — same action_type + resource_type  (broader, cross-context match)
  Tier 3 — same action_type only             (last resort; catches any related decision)

Results are limited to the last 90 days and capped at ``limit`` (default 5).
Returns ``[]`` on any error — retrieval must never block the governance pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.core.models import ProposedAction, VerdictOverride
    from src.infrastructure.cosmos_client import CosmosOverrideClient

logger = logging.getLogger(__name__)

_RECENCY_DAYS = 90
_DEFAULT_LIMIT = 5

_override_client: Optional["CosmosOverrideClient"] = None


def _get_client() -> "CosmosOverrideClient":
    global _override_client
    if _override_client is None:
        from src.infrastructure.cosmos_client import CosmosOverrideClient  # noqa: PLC0415
        _override_client = CosmosOverrideClient()
    return _override_client


async def retrieve_relevant_overrides(
    action: "ProposedAction",
    limit: int = _DEFAULT_LIMIT,
) -> list["VerdictOverride"]:
    """Return the most relevant past overrides for this action (newest-first).

    Uses a 3-tier fallback — stops at the first tier that returns results:
      1. Exact fingerprint (same action_type + resource_type + is_production + is_critical)
      2. Same action_type + resource_type (any production/criticality flags)
      3. Same action_type only

    Returns [] on any error so override retrieval never blocks the pipeline.
    """
    try:
        return await _retrieve(action, limit)
    except Exception as exc:
        logger.warning("override_retrieval: failed (%s) — returning empty list", exc)
        return []


async def _retrieve(
    action: "ProposedAction",
    limit: int,
) -> list["VerdictOverride"]:
    import asyncio

    from src.core.models import VerdictOverride  # noqa: PLC0415
    from src.core.override_capture import (  # noqa: PLC0415
        _derive_is_production,
        compute_fingerprint_hash,
    )

    client = _get_client()
    resource_id = action.target.resource_id or ""
    resource_group = action.target.resource_group or ""
    action_type = action.action_type.value
    resource_type = action.target.resource_type or ""
    is_production = _derive_is_production(resource_id, resource_group)
    is_critical = resource_type.lower() in (
        "microsoft.compute/virtualmachines",
        "microsoft.sql/servers",
        "microsoft.storage/storageaccounts",
    )

    fingerprint = compute_fingerprint_hash(
        action_type, resource_type, is_production, is_critical
    )

    # Tier 1: exact fingerprint match (uses the partition key — cheapest query)
    tier1_raw = await asyncio.to_thread(
        client.get_by_fingerprint, fingerprint, limit, _RECENCY_DAYS
    )
    if tier1_raw:
        return _to_models(tier1_raw, VerdictOverride)

    # Tier 2: same action_type + resource_type (cross-partition, no flags filter)
    tier2_raw = await asyncio.to_thread(
        client.get_by_action_resource, action_type, resource_type, limit, _RECENCY_DAYS
    )
    if tier2_raw:
        return _to_models(tier2_raw, VerdictOverride)

    # Tier 3: same action_type only — broadest possible fallback
    tier3_raw = await asyncio.to_thread(
        client.get_by_action_type, action_type, limit, _RECENCY_DAYS
    )
    return _to_models(tier3_raw, VerdictOverride)


def _to_models(records: list[dict], model_class) -> list:
    """Deserialise raw Cosmos/mock dicts into Pydantic models.

    Skips records that fail validation (e.g. written by older schema versions)
    so a single bad record never breaks the full retrieval.
    """
    result = []
    for rec in records:
        try:
            result.append(model_class.model_validate(rec))
        except Exception as exc:
            logger.debug("override_retrieval: skipping malformed record — %s", exc)
    return result
