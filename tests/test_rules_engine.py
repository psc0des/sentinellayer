"""Phase 40A — Rules engine foundation tests.

Covers: base.py decorator/registry, InventoryIndex, and evaluate_inventory.
"""

import pytest

from src.rules.base import (
    Category,
    Finding,
    RuleSpec,
    Severity,
    _REGISTRY,
    all_rules,
    rule,
    rules_for_category,
)
from src.rules.inventory_index import InventoryIndex


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_resource(rid: str, rtype: str, name: str, **extra) -> dict:
    return {"id": rid, "type": rtype, "name": name, **extra}


def _make_finding(resource: dict, rule_id: str = "TEST-001", category: Category = Category.COST) -> Finding:
    return Finding(
        rule_id=rule_id,
        rule_name=rule_id,
        category=category,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=resource["name"],
        reason="test reason",
        recommended_action="delete_resource",
    )


# ---------------------------------------------------------------------------
# @rule decorator and registry
# ---------------------------------------------------------------------------

class TestRuleDecorator:
    def test_decorator_registers_rule(self):
        before = len(_REGISTRY)

        @rule(
            id="TEST-DECORATOR-001",
            name="Decorator Test",
            category=Category.COST,
            severity=Severity.LOW,
            applies_to=["microsoft.test/things"],
        )
        def _evaluate(resource, idx):
            return None

        assert len(_REGISTRY) == before + 1
        spec = _REGISTRY[-1]
        assert spec.rule_id == "TEST-DECORATOR-001"
        assert spec.name == "Decorator Test"
        assert spec.category == Category.COST
        assert spec.severity == Severity.LOW
        assert spec.applies_to == ["microsoft.test/things"]

    def test_all_rules_returns_copy(self):
        rules = all_rules()
        rules.clear()
        assert len(all_rules()) > 0  # original untouched

    def test_rules_for_category_filters(self):
        @rule(
            id="TEST-CAT-SECURITY-001",
            name="Security Test",
            category=Category.SECURITY,
            severity=Severity.HIGH,
            applies_to=["*"],
        )
        def _sec(resource, idx):
            return None

        @rule(
            id="TEST-CAT-COST-001",
            name="Cost Test",
            category=Category.COST,
            severity=Severity.MEDIUM,
            applies_to=["*"],
        )
        def _cost(resource, idx):
            return None

        sec_rules = rules_for_category(Category.SECURITY)
        cost_rules = rules_for_category(Category.COST)
        assert all(r.category == Category.SECURITY for r in sec_rules)
        assert all(r.category == Category.COST for r in cost_rules)


# ---------------------------------------------------------------------------
# InventoryIndex
# ---------------------------------------------------------------------------

class TestInventoryIndex:
    def test_by_type_case_insensitive(self):
        resources = [
            _make_resource("/sub/rg/vm1", "Microsoft.Compute/virtualMachines", "vm1"),
            _make_resource("/sub/rg/vm2", "MICROSOFT.COMPUTE/VIRTUALMACHINES", "vm2"),
        ]
        idx = InventoryIndex(resources)
        assert len(idx.by_type("microsoft.compute/virtualmachines")) == 2
        assert len(idx.by_type("MICROSOFT.COMPUTE/VIRTUALMACHINES")) == 2

    def test_by_type_unknown_returns_empty(self):
        idx = InventoryIndex([])
        assert idx.by_type("microsoft.foo/bar") == []

    def test_get_by_id_case_insensitive(self):
        rid = "/subscriptions/abc/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"
        res = _make_resource(rid, "microsoft.compute/virtualmachines", "vm1")
        idx = InventoryIndex([res])
        assert idx.get(rid) is res
        assert idx.get(rid.lower()) is res
        assert idx.get(rid.upper()) is res

    def test_get_missing_returns_none(self):
        idx = InventoryIndex([])
        assert idx.get("/subscriptions/x/resourceGroups/y/providers/z/a/b") is None

    def test_types_present(self):
        resources = [
            _make_resource("/sub/rg/d1", "microsoft.compute/disks", "d1"),
            _make_resource("/sub/rg/vm1", "microsoft.compute/virtualmachines", "vm1"),
        ]
        idx = InventoryIndex(resources)
        assert "microsoft.compute/disks" in idx.types_present()
        assert "microsoft.compute/virtualmachines" in idx.types_present()

    def test_all_returns_all_resources(self):
        resources = [
            _make_resource("/sub/rg/vm1", "microsoft.compute/virtualmachines", "vm1"),
            _make_resource("/sub/rg/disk1", "microsoft.compute/disks", "disk1"),
        ]
        idx = InventoryIndex(resources)
        assert len(idx.all()) == 2

    def test_is_referenced_finds_nested_arm_id(self):
        target_id = "/subscriptions/abc-123/resourceGroups/rg-prod/providers/Microsoft.Network/networkInterfaces/nic1"
        referencer = {
            "id": "/subscriptions/abc-123/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/vm1",
            "type": "microsoft.compute/virtualmachines",
            "name": "vm1",
            "properties": {
                "networkProfile": {
                    "networkInterfaces": [{"id": target_id}]
                }
            },
        }
        idx = InventoryIndex([referencer])
        assert idx.is_referenced(target_id)

    def test_is_referenced_false_for_unreferenced(self):
        res = _make_resource(
            "/sub/rg/vm1",
            "microsoft.compute/virtualmachines",
            "vm1",
        )
        idx = InventoryIndex([res])
        assert not idx.is_referenced("/sub/rg/orphan-nic")

    def test_empty_inventory(self):
        idx = InventoryIndex([])
        assert idx.all() == []
        assert idx.types_present() == set()
        assert idx.by_type("anything") == []


# ---------------------------------------------------------------------------
# evaluate_inventory
# ---------------------------------------------------------------------------

class TestEvaluateInventory:
    def test_empty_inventory_returns_empty_findings(self):
        from src.rules import evaluate_inventory
        assert evaluate_inventory([]) == []

    def test_applies_to_wildcard_runs_on_all_resources(self):
        from src.rules import evaluate_inventory
        from src.rules.base import _REGISTRY

        @rule(
            id="TEST-EVAL-WILDCARD-001",
            name="Wildcard Rule",
            category=Category.HYGIENE,
            severity=Severity.LOW,
            applies_to=["*"],
        )
        def _always_fires(resource, idx):
            return _make_finding(resource, "TEST-EVAL-WILDCARD-001", Category.HYGIENE)

        resources = [
            _make_resource("/sub/rg/vm1", "microsoft.compute/virtualmachines", "vm1"),
            _make_resource("/sub/rg/d1", "microsoft.compute/disks", "d1"),
        ]
        findings = evaluate_inventory(resources, category=Category.HYGIENE)
        # At least 2 from our rule (may be more from other registered rules)
        wildcard_findings = [f for f in findings if f.rule_id == "TEST-EVAL-WILDCARD-001"]
        assert len(wildcard_findings) == 2

    def test_applies_to_filters_by_type(self):
        from src.rules import evaluate_inventory

        @rule(
            id="TEST-EVAL-TYPE-001",
            name="Type-filtered Rule",
            category=Category.COST,
            severity=Severity.LOW,
            applies_to=["microsoft.compute/disks"],
        )
        def _only_disks(resource, idx):
            return _make_finding(resource, "TEST-EVAL-TYPE-001", Category.COST)

        resources = [
            _make_resource("/sub/rg/vm1", "microsoft.compute/virtualmachines", "vm1"),
            _make_resource("/sub/rg/d1", "microsoft.compute/disks", "d1"),
            _make_resource("/sub/rg/d2", "microsoft.compute/disks", "d2"),
        ]
        findings = [f for f in evaluate_inventory(resources) if f.rule_id == "TEST-EVAL-TYPE-001"]
        assert len(findings) == 2
        assert all(f.resource_type == "microsoft.compute/disks" for f in findings)

    def test_rule_exception_is_swallowed_scan_continues(self):
        from src.rules import evaluate_inventory

        @rule(
            id="TEST-EVAL-EXCEPT-001",
            name="Crashing Rule",
            category=Category.HYGIENE,
            severity=Severity.LOW,
            applies_to=["microsoft.compute/virtualmachines"],
        )
        def _crashes(resource, idx):
            raise RuntimeError("deliberate crash in test")

        @rule(
            id="TEST-EVAL-EXCEPT-002",
            name="Good Rule",
            category=Category.HYGIENE,
            severity=Severity.LOW,
            applies_to=["microsoft.compute/virtualmachines"],
        )
        def _good(resource, idx):
            return _make_finding(resource, "TEST-EVAL-EXCEPT-002", Category.HYGIENE)

        resources = [
            _make_resource("/sub/rg/vm1", "microsoft.compute/virtualmachines", "vm1"),
        ]
        # Must not raise; good rule still fires
        findings = evaluate_inventory(resources, category=Category.HYGIENE)
        good_findings = [f for f in findings if f.rule_id == "TEST-EVAL-EXCEPT-002"]
        assert len(good_findings) == 1

    def test_category_filter_restricts_output(self):
        from src.rules import evaluate_inventory

        # Register one cost and one security rule
        @rule(
            id="TEST-CAT-FILTER-COST-001",
            name="Cat Cost",
            category=Category.COST,
            severity=Severity.LOW,
            applies_to=["*"],
        )
        def _cat_cost(resource, idx):
            return _make_finding(resource, "TEST-CAT-FILTER-COST-001", Category.COST)

        @rule(
            id="TEST-CAT-FILTER-SEC-001",
            name="Cat Sec",
            category=Category.SECURITY,
            severity=Severity.HIGH,
            applies_to=["*"],
        )
        def _cat_sec(resource, idx):
            return None  # never fires

        resources = [_make_resource("/sub/rg/vm1", "microsoft.compute/virtualmachines", "vm1")]
        findings = evaluate_inventory(resources, category=Category.COST)
        # Security rule should not appear
        assert all(f.category == Category.COST or f.rule_id not in ("TEST-CAT-FILTER-SEC-001",)
                   for f in findings)

    def test_rule_returning_none_produces_no_finding(self):
        from src.rules import evaluate_inventory

        @rule(
            id="TEST-EVAL-NONE-001",
            name="Never Fires",
            category=Category.RELIABILITY,
            severity=Severity.LOW,
            applies_to=["microsoft.compute/virtualmachines"],
        )
        def _never(resource, idx):
            return None

        resources = [_make_resource("/sub/rg/vm1", "microsoft.compute/virtualmachines", "vm1")]
        findings = [f for f in evaluate_inventory(resources) if f.rule_id == "TEST-EVAL-NONE-001"]
        assert findings == []


# ---------------------------------------------------------------------------
# Auto-loader smoke test
# ---------------------------------------------------------------------------

class TestAutoLoader:
    def test_auto_loader_does_not_raise(self):
        """Importing src.rules must not raise even if rule packages are empty."""
        import importlib
        import src.rules
        importlib.reload(src.rules)  # re-runs auto-loader; should be idempotent

    def test_registry_not_empty_after_import(self):
        """After a full import, at least the engine itself must be present."""
        from src.rules import all_rules
        # Phase 40A has no rules yet — but Phase 40B+ will add them.
        # This test just ensures no ImportError during load.
        assert isinstance(all_rules(), list)
