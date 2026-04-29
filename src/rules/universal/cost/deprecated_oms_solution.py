"""UNIV-COST-007 — Flag deprecated OMS/Log Analytics solutions.

Several OMS (Operations Management Suite) solutions are deprecated and replaced by
Azure Monitor features. They continue to bill while delivering no new capability.
Known deprecated: Security, SecurityCenterFree, AgentHealthAssessment, WireData.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_DEPRECATED_SOLUTIONS = {
    "security",
    "securitycenterfree",
    "wiredata",
    "wiredata2",
    "agentHealthAssessment".lower(),
    "omsgallerysolutionmanager",
    "networksecurityanalysis",
}


@rule(
    id="UNIV-COST-007",
    name="Deprecated OMS Solution",
    category=Category.COST,
    severity=Severity.LOW,
    applies_to=["microsoft.operationsmanagement/solutions"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    name = (resource.get("name") or "").lower()
    # Solution names are often in the form "SolutionName(WorkspaceName)"
    solution_base = name.split("(")[0].strip()
    if solution_base not in _DEPRECATED_SOLUTIONS:
        return None
    display = resource.get("name", "")
    return Finding(
        rule_id="UNIV-COST-007",
        rule_name="Deprecated OMS Solution",
        category=Category.COST,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=display,
        reason=(
            f"OMS solution '{display}' is deprecated and replaced by native Azure Monitor "
            "features. It is billing unnecessarily. Remove it and migrate to the built-in "
            "Microsoft Defender for Cloud or Azure Monitor equivalents."
        ),
        recommended_action="delete_resource",
        evidence={"solution_name": display},
    )
