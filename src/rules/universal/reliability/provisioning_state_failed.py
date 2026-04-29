"""UNIV-REL-001 — Flag resources in a Failed provisioning state.

A resource stuck in 'Failed' provisioningState is not operational. It may be
billing (for compute/storage) while delivering no service and blocking retries.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_FAILED_STATES = {"failed", "canceled"}


@rule(
    id="UNIV-REL-001",
    name="Resource in Failed Provisioning State",
    category=Category.RELIABILITY,
    severity=Severity.HIGH,
    applies_to=["*"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    state = (props.get("provisioningState") or "").lower()
    if state not in _FAILED_STATES:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-REL-001",
        rule_name="Resource in Failed Provisioning State",
        category=Category.RELIABILITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Resource '{name}' (type: {resource.get('type', '')}) is in "
            f"'{state}' provisioning state. The resource is not operational. "
            "Investigate the activity log, delete and recreate, or redeploy the template."
        ),
        recommended_action="update_config",
        evidence={"provisioningState": state},
    )
