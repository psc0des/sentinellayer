"""Risk Triage Foundation — Phase 26.

Classifies every ProposedAction into a processing tier *before* governance
evaluation.  This is the cornerstone of enterprise-scale efficiency: instead
of running 8 LLM calls for every action regardless of risk, the triage engine
routes the majority of routine, low-risk actions to cheaper evaluation paths.

Tiers
-----
  Tier 1 — Deterministic rules only (0 LLM calls, <1 ms)
            Reserved for clearly low-risk, non-production, isolated actions.
            The full rule-based scoring still runs; only the LLM synthesis is
            skipped.  Composite score and verdict are based on raw rules alone.

  Tier 2 — Single consolidated LLM call (Phase 27+)
            Moderate-risk actions that need *some* LLM reasoning but not the
            full four-agent deep-dive.  Phase 26 routes these as Tier 2 but
            still falls through to the full pipeline; Phase 27 will short-
            circuit here.

  Tier 3 — Full 4-agent pipeline (current default)
            All high-risk, network-security-touching, compliance-scoped, or
            irreversibly destructive actions.  Runs all four governance agents
            plus the decision engine — current behaviour unchanged.

ActionFingerprint
-----------------
A lightweight, LLM-free descriptor derived from ProposedAction + resource
metadata that captures the risk surface dimensions needed for tier routing.
``compute_fingerprint()`` runs in <1 ms with no I/O.

classify_tier()
---------------
Applies deterministic rule-based routing to the fingerprint.  Conservative
by design: unknown/ambiguous cases always default to Tier 3.  Only clearly
isolated, non-production actions are routed to Tier 1.

build_org_context()
-------------------
Constructs an :class:`OrgContext` from the global :data:`settings` object.
Called once at startup by the pipeline and cached for the process lifetime.
"""

import logging
from typing import Optional

from src.config import settings
from src.core.models import ActionFingerprint, OrgContext, ProposedAction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resource-type classification sets
# ---------------------------------------------------------------------------

# Resource types that affect network security policy.
# We use lowercase substrings so partial matches work (e.g. "nsg" matches
# "microsoft.network/networksecuritygroups").
_NETWORK_EXPOSURE_SUBTYPES: frozenset[str] = frozenset({
    "networksecuritygroup",
    "azurefirewall",
    "firewall",
    "publicipaddress",
    "virtualnetworkgateway",
    "applicationgateway",
    "frontdoor",
    "loadbalancer",
    # Short names used in seed_resources / test payloads
    "nsg",
    "pip",
})

# Resource types that carry persistent data.
_DATA_PLANE_SUBTYPES: frozenset[str] = frozenset({
    "storageaccount",
    "servers/databases",
    "databaseaccount",
    "vaults",
    "flexibleservers",
    "workspaces",
    "datalakestore",
    # Short names
    "storage",
    "sql",
    "cosmos",
    "keyvault",
    "postgres",
    "mysql",
    "synapse",
})

# Action types with no easy undo (IaC can recreate, but downtime is real).
_DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({"delete_resource"})

# Action types that are hard to roll back instantly but not permanent.
_SEMI_REVERSIBLE_ACTIONS: frozenset[str] = frozenset({
    "modify_nsg",
    "update_config",
    "scale_down",
})


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def build_org_context() -> OrgContext:
    """Construct an :class:`OrgContext` from the global application settings.

    Called once at pipeline startup.  The returned object is immutable and
    safe to share across all concurrent evaluations.

    Returns:
        :class:`~src.core.models.OrgContext` populated from environment variables.
    """
    frameworks = [
        f.strip()
        for f in settings.org_compliance_frameworks.split(",")
        if f.strip()
    ]
    critical_rgs = [
        rg.strip()
        for rg in settings.org_business_critical_rgs.split(",")
        if rg.strip()
    ]
    return OrgContext(
        org_name=settings.org_name,
        resource_count=settings.org_resource_count,
        compliance_frameworks=frameworks,
        risk_tolerance=settings.org_risk_tolerance,
        business_critical_rgs=critical_rgs,
    )


def compute_fingerprint(
    action: ProposedAction,
    resource_metadata: Optional[dict],
    org_context: Optional[OrgContext],
) -> ActionFingerprint:
    """Derive a risk fingerprint from a ProposedAction without any LLM call.

    All logic is pure Python — no I/O, no network calls, no randomness.
    Given identical inputs this function always returns the same fingerprint.

    Args:
        action: The proposed infrastructure action.
        resource_metadata: Tags and environment from the resource graph
            (``None`` when the resource is unknown — conservative defaults apply).
        org_context: Organizational knowledge for compliance scope detection.
            ``None`` → assume compliance scope applies (safe default).

    Returns:
        :class:`~src.core.models.ActionFingerprint` with all fields populated.
    """
    tags: dict = {}
    if resource_metadata:
        tags = resource_metadata.get("tags") or {}

    # ── Environment detection ─────────────────────────────────────────────
    environment = (tags.get("environment") or "").lower()
    if not environment:
        rid_lower = action.target.resource_id.lower()
        if "prod" in rid_lower:
            environment = "production"
        elif "stag" in rid_lower or "uat" in rid_lower:
            environment = "staging"
        elif any(k in rid_lower for k in ("dev", "test", "sandbox", "demo")):
            environment = "development"
        else:
            environment = "unknown"

    is_production = environment == "production"

    # ── Criticality ───────────────────────────────────────────────────────
    is_critical_resource = (
        tags.get("criticality", "").lower() in {"critical", "high"}
        or tags.get("purpose", "").lower() == "disaster-recovery"
        or tags.get("tier", "").lower() == "critical"
        or tags.get("managed_by", "").lower() == "terraform"  # IaC-managed = controlled
        # ↑ IaC-managed resources are tracked, not inherently critical.
        # We'll refine this heuristic once we have real tag taxonomies.
    )
    # Stricter: only truly critical tags count for routing
    is_critical_resource = (
        tags.get("criticality", "").lower() in {"critical", "high"}
        or tags.get("purpose", "").lower() == "disaster-recovery"
        or tags.get("tier", "").lower() == "critical"
    )

    # ── Network exposure detection ─────────────────────────────────────────
    resource_type_lower = action.target.resource_type.lower()
    has_network_exposure = any(
        sub in resource_type_lower for sub in _NETWORK_EXPOSURE_SUBTYPES
    )
    # modify_nsg always touches network policy, even if resource_type is vague
    if action.action_type.value == "modify_nsg":
        has_network_exposure = True
    # Opening a firewall port = network exposure regardless of direction metadata
    if getattr(action, "nsg_change_direction", None) == "open":
        has_network_exposure = True

    # ── Data plane detection ──────────────────────────────────────────────
    has_data_plane_impact = any(
        sub in resource_type_lower for sub in _DATA_PLANE_SUBTYPES
    )

    # ── Estimated blast radius ────────────────────────────────────────────
    # Rough heuristic; Phase 27 will refine using decision memory / topology.
    if has_network_exposure and is_production:
        estimated_blast_radius = "environment"
    elif is_critical_resource:
        estimated_blast_radius = "service"
    elif not is_production:
        estimated_blast_radius = "isolated"
    else:
        estimated_blast_radius = "service"

    # ── Change reversibility ──────────────────────────────────────────────
    action_type_val = action.action_type.value
    if action_type_val in _DESTRUCTIVE_ACTIONS:
        change_reversibility = "destructive"
    elif action_type_val in _SEMI_REVERSIBLE_ACTIONS:
        change_reversibility = "semi-reversible"
    else:
        change_reversibility = "reversible"

    # ── Compliance scope ──────────────────────────────────────────────────
    # Without org context, assume compliance scope applies (conservative).
    if org_context is None:
        compliance_scope = True
    else:
        rg = action.target.resource_group or ""
        in_critical_rg = rg in org_context.business_critical_rgs
        has_compliance_tag = bool(tags.get("compliance", "").strip())
        # Any compliance framework + production = in scope
        has_frameworks = bool(org_context.compliance_frameworks) and is_production
        compliance_scope = in_critical_rg or has_compliance_tag or has_frameworks

    return ActionFingerprint(
        action_type=action_type_val,
        resource_type=action.target.resource_type,
        environment=environment,
        compliance_scope=compliance_scope,
        has_network_exposure=has_network_exposure,
        has_data_plane_impact=has_data_plane_impact,
        is_production=is_production,
        is_critical_resource=is_critical_resource,
        estimated_blast_radius=estimated_blast_radius,
        change_reversibility=change_reversibility,
    )


def classify_tier(fp: ActionFingerprint) -> int:
    """Route an :class:`ActionFingerprint` to a processing tier (1, 2, or 3).

    Rules are evaluated in order; the first match wins.  All rules err on the
    side of *more* scrutiny — we never skip evaluation we might need.

    Rule ordering:
      1. Tier 3: network exposure AND compliance scope
         (Any firewall/NSG change in a regulated environment must go through
         full evaluation.  No shortcuts.)
      2. Tier 3: destructive + production + critical
         (Irreversible action on a critical production resource — full review.)
      3. Tier 1: not production AND isolated blast radius
         (Clearly safe: non-production resource with no downstream dependencies.
         Full deterministic scoring runs, but LLM synthesis is skipped.)
      4. Tier 2: production + service blast + no network exposure
         (Moderate risk — production but scoped to a single service.  Phase 27
         will short-circuit here with a single consolidated LLM call.)
      5. Default → Tier 3
         (Conservative baseline.  Everything we can't clearly classify is fully
         evaluated until Phase 27 builds enough decision memory to route safely.)

    Args:
        fp: :class:`~src.core.models.ActionFingerprint` from ``compute_fingerprint()``.

    Returns:
        Integer 1, 2, or 3.
    """
    # ── Rule 1: Network security change in compliance scope ────────────────
    if fp.has_network_exposure and fp.compliance_scope:
        logger.debug(
            "triage: Tier 3 — network exposure + compliance scope "
            "(action=%s resource=%s)",
            fp.action_type,
            fp.resource_type,
        )
        return 3

    # ── Rule 2: Irreversible action on production critical resource ────────
    if (
        fp.change_reversibility == "destructive"
        and fp.is_production
        and fp.is_critical_resource
    ):
        logger.debug(
            "triage: Tier 3 — destructive + production + critical "
            "(action=%s resource=%s)",
            fp.action_type,
            fp.resource_type,
        )
        return 3

    # ── Rule 3: Non-production with isolated blast radius ─────────────────
    if not fp.is_production and fp.estimated_blast_radius == "isolated":
        logger.debug(
            "triage: Tier 1 — non-production + isolated blast radius "
            "(action=%s env=%s)",
            fp.action_type,
            fp.environment,
        )
        return 1

    # ── Rule 4: Production, service-level, no network exposure ────────────
    if (
        fp.is_production
        and fp.estimated_blast_radius == "service"
        and not fp.has_network_exposure
    ):
        logger.debug(
            "triage: Tier 2 — production + service blast + no network exposure "
            "(action=%s resource=%s)",
            fp.action_type,
            fp.resource_type,
        )
        return 2

    # ── Default: full pipeline ─────────────────────────────────────────────
    logger.debug(
        "triage: Tier 3 (default) — action=%s resource=%s env=%s",
        fp.action_type,
        fp.resource_type,
        fp.environment,
    )
    return 3
