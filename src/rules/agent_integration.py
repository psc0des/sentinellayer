"""Rules engine → agent bridge.

Shared helpers used by all three operational agents to:
1. Run the rules engine pre-pass against an inventory.
2. Convert findings to ProposedActions.
3. Deduplicate proposals after the LLM pass (keeping rule-derived ones).
4. Build the LLM prompt fragment describing deterministic findings.
"""

import logging
from typing import Optional

from src.rules.base import Category, Finding
from src.core.models import ProposedAction

logger = logging.getLogger(__name__)


def run_rules_prescan(
    inventory: list,
    categories: list[Category],
    agent_id: str,
) -> tuple[list[ProposedAction], list[Finding], str]:
    """Run the rules engine for the given categories and return proposals + prompt text.

    Args:
        inventory: Full resource list from the inventory builder.
        categories: Which rule categories to run (e.g. [Category.COST]).
        agent_id: Identifier of the calling agent (stamped on proposals).

    Returns:
        Tuple of:
        - proposals: list of ProposedAction already converted from findings
        - findings: the raw Finding objects (for coverage manifest)
        - findings_text: LLM-ready text block listing deterministic findings
    """
    from src.rules import evaluate_inventory
    from src.governance.finding_to_proposal import finding_to_proposal

    all_findings: list[Finding] = []
    for cat in categories:
        try:
            all_findings.extend(evaluate_inventory(inventory, category=cat))
        except Exception as exc:  # noqa: BLE001
            logger.warning("rules prescan failed for category %s: %s", cat, exc)

    proposals: list[ProposedAction] = []
    conversion_errors = 0
    for finding in all_findings:
        try:
            proposals.append(finding_to_proposal(finding, agent_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finding_to_proposal failed for rule %s: %s", finding.rule_id, exc
            )
            conversion_errors += 1

    by_category: dict[str, int] = {}
    for f in all_findings:
        by_category[f.category.value] = by_category.get(f.category.value, 0) + 1

    logger.info(
        "rules prescan: %d findings across %s; %d converted to proposals (%d errors)",
        len(all_findings),
        by_category,
        len(proposals),
        conversion_errors,
    )

    findings_text = _build_findings_text(all_findings)
    return proposals, all_findings, findings_text


def _build_findings_text(findings: list[Finding]) -> str:
    """Build a structured text block for injection into the LLM prompt."""
    if not findings:
        return (
            "=== DETERMINISTIC FINDINGS: 0 issues detected by the Universal Rules Engine ===\n"
            "No rule-matched findings. Conduct independent discovery.\n"
        )

    lines = [
        f"=== DETERMINISTIC FINDINGS: {len(findings)} issue(s) from the Universal Rules Engine ===",
        "Each finding below was raised by a deterministic rule. Your job is to ENRICH each one:",
        "  1. Add blast radius context: which downstream services are affected?",
        "  2. Add business context: what is the real-world risk if this is not fixed?",
        "  3. Write a clear, actionable human reason in the proposal (replace the rule template).",
        "  4. Confirm with a tool call if you need additional data.",
        "After enriching all findings, THEN look for additional issues the rules missed.",
        "",
    ]
    for i, f in enumerate(findings, 1):
        sev = f.severity.value.upper()
        lines.append(
            f"[{i}] [{f.rule_id}] [{sev}] {f.resource_name} ({f.resource_type})"
        )
        lines.append(f"     Issue: {f.reason[:200]}{'...' if len(f.reason) > 200 else ''}")
        lines.append(f"     Action: {f.recommended_action}")
        lines.append("")

    return "\n".join(lines)


def dedup_proposals(proposals: list[ProposedAction]) -> list[ProposedAction]:
    """Remove duplicate proposals by (resource_id, action_type).

    When both a rule-derived proposal and an LLM proposal target the same resource
    with the same action, the rule-derived one wins (has provenance, is deterministic).
    Rule-derived proposals are identified by '[UNIV-' or '[TYPE-' prefix in reason.
    """
    seen: dict[tuple[str, str], int] = {}  # key → index of kept proposal
    result: list[ProposedAction] = []

    for proposal in proposals:
        key = (
            proposal.target.resource_id.lower(),
            proposal.action_type.value,
        )
        is_rule_derived = _is_rule_derived(proposal)
        if key not in seen:
            seen[key] = len(result)
            result.append(proposal)
        elif is_rule_derived:
            # Rule-derived proposal wins — replace the LLM-generated one
            old_idx = seen[key]
            result[old_idx] = proposal

    removed = len(proposals) - len(result)
    if removed:
        logger.info("dedup_proposals: removed %d duplicate(s)", removed)
    return result


def _is_rule_derived(proposal: ProposedAction) -> bool:
    reason = proposal.reason or ""
    return reason.startswith("[UNIV-") or reason.startswith("[TYPE-")


def build_coverage_manifest(
    inventory: list,
    all_findings: list[Finding],
    categories_run: list[Category],
) -> dict:
    """Build a coverage_manifest dict suitable for appending to a scan record."""
    from src.rules import all_rules
    from src.rules.inventory_index import InventoryIndex

    idx = InventoryIndex(inventory)
    types_in_inventory = idx.types_present()

    all_rule_specs = all_rules()
    rules_applied = len(all_rule_specs)
    rules_matched = len(set(f.rule_id for f in all_findings))

    types_with_rule: set[str] = set()
    for spec in all_rule_specs:
        if "*" in spec.applies_to:
            types_with_rule.update(types_in_inventory)
        else:
            for t in spec.applies_to:
                if t.lower() in types_in_inventory:
                    types_with_rule.add(t.lower())

    types_uncovered = sorted(types_in_inventory - types_with_rule)

    by_cat: dict[str, dict] = {}
    for cat in categories_run:
        cat_specs = [s for s in all_rule_specs if s.category == cat]
        cat_findings = [f for f in all_findings if f.category == cat]
        by_cat[cat.value] = {
            "applied": len(cat_specs),
            "matched": len(cat_findings),
        }

    return {
        "rules_applied": rules_applied,
        "rules_matched": rules_matched,
        "categories": by_cat,
        "types_in_inventory": len(types_in_inventory),
        "types_with_at_least_one_rule": len(types_with_rule),
        "types_uncovered": types_uncovered[:50],  # cap at 50 for Cosmos doc size
    }
