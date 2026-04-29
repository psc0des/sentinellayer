"""Universal Rules Engine — auto-loader and public API.

Import this package to load all rules and call evaluate_inventory().
Rule modules register themselves via the @rule decorator on import.
"""

import importlib
import logging
import pkgutil
from typing import Optional

from . import type_aware, universal  # noqa: F401 — triggers auto-load below
from .base import (
    Category,
    Finding,
    Severity,
    _LOAD_ERRORS,
    _REGISTRY,
    all_rules,
    rules_for_category,
)

logger = logging.getLogger(__name__)

__all__ = [
    "evaluate_inventory",
    "all_rules",
    "rules_for_category",
    "Category",
    "Severity",
    "Finding",
]


def _autoload(package) -> None:
    """Walk a package tree and import every module, registering rules via decorator."""
    for _, name, _ispkg in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            msg = f"rules auto-loader: failed to import {name!r}: {exc}"
            logger.error(msg)
            _LOAD_ERRORS.append(msg)


_autoload(universal)
_autoload(type_aware)


def evaluate_inventory(
    resources: list,
    category: Optional[Category] = None,
) -> list:
    """Run all rules (or one category) against an inventory; return findings.

    Rule failures are caught and logged — they never abort a scan.
    """
    from .inventory_index import InventoryIndex

    idx = InventoryIndex(resources)
    specs = rules_for_category(category) if category else all_rules()
    findings: list = []

    for spec in specs:
        if "*" in spec.applies_to:
            targets = idx.all()
        else:
            targets = [r for t in spec.applies_to for r in idx.by_type(t)]

        for resource in targets:
            try:
                f = spec.func(resource, idx)
                if f is not None:
                    findings.append(f)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "rule %s raised on resource %s: %s",
                    spec.rule_id,
                    resource.get("id", "<unknown>"),
                    exc,
                )

    return findings
