"""UNIV-SEC-008 — Container registry anonymous pull must be disabled.

anonymousPullEnabled=true allows any unauthenticated client to pull images. This is
rarely intentional and exposes proprietary application images to the public internet.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-SEC-008",
    name="Container Registry Anonymous Pull Enabled",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.containerregistry/registries"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    if not props.get("anonymousPullEnabled"):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-008",
        rule_name="Container Registry Anonymous Pull Enabled",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Container registry '{name}' allows anonymous (unauthenticated) pulls. "
            "This exposes all images to the public internet without any identity verification. "
            "Disable anonymousPullEnabled and require authentication for all pulls."
        ),
        recommended_action="update_config",
        evidence={"anonymousPullEnabled": True},
    )
