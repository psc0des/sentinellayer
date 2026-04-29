"""Rules engine foundation — Finding model, rule decorator, and global registry."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Category(str, Enum):
    COST = "cost"
    SECURITY = "security"
    RELIABILITY = "reliability"
    HYGIENE = "hygiene"


@dataclass
class Finding:
    """A deterministic issue raised by a rule against a single resource."""
    rule_id: str
    rule_name: str
    category: Category
    severity: Severity
    resource_id: str
    resource_type: str
    resource_name: str
    reason: str
    recommended_action: str
    evidence: dict = field(default_factory=dict)
    estimated_savings_monthly: Optional[float] = None


@dataclass
class RuleSpec:
    rule_id: str
    name: str
    category: Category
    severity: Severity
    applies_to: list  # ["*"] or ["microsoft.compute/disks"]
    func: Callable    # (resource: dict, idx: InventoryIndex) -> Optional[Finding]


_REGISTRY: list[RuleSpec] = []
_LOAD_ERRORS: list[str] = []


def rule(*, id: str, name: str, category: Category, severity: Severity, applies_to: list):
    """Decorator — registers a rule function on import."""
    def _wrap(func):
        _REGISTRY.append(RuleSpec(id, name, category, severity, applies_to, func))
        return func
    return _wrap


def all_rules() -> list[RuleSpec]:
    return list(_REGISTRY)


def rules_for_category(category: Category) -> list[RuleSpec]:
    return [r for r in _REGISTRY if r.category == category]


def get_load_errors() -> list[str]:
    return list(_LOAD_ERRORS)
