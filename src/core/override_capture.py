"""Override capture helper — Phase 35A: Override Feedback Loop.

Every operator force-execute, dismiss, or satisfy-condition action calls
``capture_override()`` which writes a ``VerdictOverride`` record to the
``governance-overrides`` Cosmos container.

Phase 35B will read these records to inject relevant past overrides into
LLM prompts as in-context examples — teaching the system what its operators
actually do when they disagree with a verdict.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from src.core.models import ExecutionRecord, OverrideType, VerdictOverride

if TYPE_CHECKING:
    from src.infrastructure.cosmos_client import CosmosOverrideClient

logger = logging.getLogger(__name__)

# Minimum free-text length for override types that signal a serious governance
# bypass.  Enforced here AND in the endpoint so neither can be circumvented.
_MIN_REASON_LEN = 20

# Module-level singleton — replaced in tests via ``_override_client = ...``
_override_client: Optional["CosmosOverrideClient"] = None


def _get_override_client() -> "CosmosOverrideClient":
    global _override_client  # noqa: PLW0603
    if _override_client is None:
        from src.infrastructure.cosmos_client import CosmosOverrideClient

        _override_client = CosmosOverrideClient()
    return _override_client


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def compute_fingerprint_hash(
    action_type: str,
    resource_type: str,
    is_production: bool,
    is_critical: bool,
) -> str:
    """Deterministic 16-char hex hash used as Cosmos partition key.

    Two overrides with the same action_type, resource_type, production flag,
    and criticality flag produce the same hash.  This groups similar overrides
    together so Phase 35B can retrieve the top-K most-recent ones for prompt
    injection without cross-partition fan-out.

    The hash is stable across Python restarts and platform changes because it
    uses SHA-256 on a canonical pipe-delimited string.
    """
    key = f"{action_type.lower()}|{resource_type.lower()}|{is_production}|{is_critical}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def capture_override(
    execution_record: ExecutionRecord,
    override_type: OverrideType,
    operator_id: str,
    operator_reason: str,
    cosmos_client: Optional["CosmosOverrideClient"] = None,
) -> VerdictOverride:
    """Record an operator override as a structured ``VerdictOverride``.

    This is the sole write path for ``governance-overrides``.  Call it from
    every API endpoint that lets an operator override the system's verdict.

    Args:
        execution_record: The execution record being overridden.  Its
            ``verdict_snapshot`` provides the action and SRI data.
        override_type: The kind of override the operator is performing.
        operator_id: Username or email for the audit trail.
        operator_reason: Free-text justification.  Must be ≥20 characters for
            ``FORCE_EXECUTE`` and ``REVERSE_DENIAL`` override types.
        cosmos_client: Optional ``CosmosOverrideClient`` injected by tests.
            Defaults to the module-level singleton.

    Returns:
        The persisted ``VerdictOverride``.  If an override for this
        ``execution_id`` already exists, the existing record is returned
        without writing a new one (idempotent).

    Raises:
        ValueError: If ``operator_reason`` is too short for a high-severity
            override type.
    """
    if override_type in (OverrideType.FORCE_EXECUTE, OverrideType.REVERSE_DENIAL):
        if len(operator_reason.strip()) < _MIN_REASON_LEN:
            raise ValueError(
                f"operator_reason must be at least {_MIN_REASON_LEN} characters "
                f"for {override_type.value} overrides "
                f"(got {len(operator_reason.strip())})"
            )

    client = cosmos_client if cosmos_client is not None else _get_override_client()

    # Idempotency: the same execution_id must not produce two override records.
    # This protects against double-submits and retry storms.
    existing = client.get_by_execution_id(execution_record.execution_id)
    if existing:
        logger.debug(
            "override_capture: idempotent — returning existing record for %s",
            execution_record.execution_id[:8],
        )
        # Strip Cosmos system fields before deserialising
        clean = {k: v for k, v in existing.items() if k != "id"}
        return VerdictOverride(**clean)

    fields = _extract_from_verdict_snapshot(execution_record.verdict_snapshot)
    action_type = fields["action_type"]
    resource_type = fields["resource_type"]
    is_production = fields["is_production"]
    is_critical = fields["is_critical"]

    fingerprint = compute_fingerprint_hash(
        action_type, resource_type, is_production, is_critical
    )

    override = VerdictOverride(
        override_id=str(uuid.uuid4()),
        decision_id=execution_record.action_id,
        execution_id=execution_record.execution_id,
        action_id=execution_record.action_id,
        action_type=action_type,
        resource_type=resource_type,
        resource_id=fields["resource_id"],
        is_production=is_production,
        is_critical=is_critical,
        original_verdict=fields["original_verdict"],
        original_sri=fields["original_sri"],
        original_sri_breakdown=fields["original_sri_breakdown"],
        override_type=override_type,
        operator_id=operator_id,
        operator_reason=operator_reason,
        timestamp=datetime.now(timezone.utc),
        fingerprint_hash=fingerprint,
    )

    client.upsert(override.model_dump(mode="json"))
    logger.info(
        "override_capture: %s by '%s' on '%s' (exec %s, fingerprint %s)",
        override_type.value,
        operator_id,
        fields["resource_id"].split("/")[-1] or fields["resource_id"],
        execution_record.execution_id[:8],
        fingerprint,
    )
    return override


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _derive_is_production(resource_id: str, resource_group: str = "") -> bool:
    """Heuristic: is this resource in a production environment?

    Checks resource_id and resource_group for common production naming
    patterns.  Not authoritative — used only to compute the fingerprint
    for retrieval grouping.
    """
    combined = f"{resource_id}|{resource_group}".lower()
    return any(kw in combined for kw in ["/prod", "-prod", "_prod", "production"])


def _extract_from_verdict_snapshot(snapshot: dict) -> dict:
    """Pull action and SRI fields out of a stored GovernanceVerdict dump.

    ``ExecutionRecord.verdict_snapshot`` is a ``GovernanceVerdict.model_dump()``
    result.  This helper navigates the nested dict so ``capture_override``
    doesn't need to know the schema layout.
    """
    action = snapshot.get("proposed_action", {})
    target = action.get("target", {})
    sri = snapshot.get("skry_risk_index", {})

    resource_id: str = target.get("resource_id", "")
    resource_group: str = target.get("resource_group", "")

    # Fall back to extracting resource group from ARM ID when not explicit
    if not resource_group and "/resourceGroups/" in resource_id:
        try:
            resource_group = resource_id.split("/resourceGroups/")[1].split("/")[0]
        except IndexError:
            pass

    return {
        "action_type": action.get("action_type", "unknown"),
        "resource_type": target.get("resource_type", "unknown"),
        "resource_id": resource_id,
        "original_verdict": snapshot.get("decision", "unknown"),
        "original_sri": float(sri.get("sri_composite", 0.0)),
        "original_sri_breakdown": {
            "sri_infrastructure": float(sri.get("sri_infrastructure", 0.0)),
            "sri_policy": float(sri.get("sri_policy", 0.0)),
            "sri_historical": float(sri.get("sri_historical", 0.0)),
            "sri_cost": float(sri.get("sri_cost", 0.0)),
        },
        "is_production": _derive_is_production(resource_id, resource_group),
        # triage_tier == 3 → compliance-sensitive / high-criticality resource
        "is_critical": snapshot.get("triage_tier") == 3,
    }
