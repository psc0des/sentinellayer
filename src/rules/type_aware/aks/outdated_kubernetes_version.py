"""TYPE-AKS-002 — AKS clusters running an outdated Kubernetes version.

Microsoft announces Kubernetes version end-of-support dates. Clusters running
versions older than N-2 minor versions from the latest generally available release
are out of support and missing security patches.
We flag anything below 1.28 as a heuristic (update this threshold periodically).
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_MIN_SUPPORTED_MINOR = 28  # 1.28.x — update as new versions GA


def _parse_minor(version: str) -> int | None:
    try:
        parts = version.lstrip("v").split(".")
        return int(parts[1]) if len(parts) >= 2 else None
    except (ValueError, IndexError):
        return None


@rule(
    id="TYPE-AKS-002",
    name="AKS Cluster Running Outdated Kubernetes Version",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.containerservice/managedclusters"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    version = props.get("kubernetesVersion") or props.get("currentKubernetesVersion") or ""
    minor = _parse_minor(version)
    if minor is None or minor >= _MIN_SUPPORTED_MINOR:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-AKS-002",
        rule_name="AKS Cluster Running Outdated Kubernetes Version",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"AKS cluster '{name}' is running Kubernetes {version}, which is below the minimum "
            f"supported minor version (1.{_MIN_SUPPORTED_MINOR}). Outdated clusters miss "
            "security patches and CVE fixes. Upgrade using 'az aks upgrade'."
        ),
        recommended_action="update_config",
        evidence={"kubernetesVersion": version, "minSupported": f"1.{_MIN_SUPPORTED_MINOR}"},
    )
