"""InventoryIndex — query helpers over the full resource inventory.

Built once per scan and passed to every rule so rules can cross-reference
without re-scanning the list (O(1) lookups after O(n) construction).
"""

import re
from typing import Optional


_ARM_ID_RE = re.compile(
    r"/subscriptions/[^/]+/resourcegroups/[^/]+/providers/[^/]+/[^/]+/[^/\s\"']+",
    re.IGNORECASE,
)


def _walk_strings(obj, collected: set) -> None:
    """Recursively collect all string values from a nested dict/list."""
    if isinstance(obj, str):
        collected.add(obj.lower())
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_strings(v, collected)
    elif isinstance(obj, list):
        for item in obj:
            _walk_strings(item, collected)


class InventoryIndex:
    """Fast query helpers over the inventory list built once per scan."""

    def __init__(self, resources: list):
        self._resources = resources
        self._by_type: dict = {}
        self._by_id: dict = {}
        self._referenced_ids: set = set()
        self._build_indexes()

    def by_type(self, type_key: str) -> list:
        """Case-insensitive lookup by ARM resource type."""
        return self._by_type.get(type_key.lower(), [])

    def get(self, resource_id: str) -> Optional[dict]:
        return self._by_id.get(resource_id.lower())

    def is_referenced(self, resource_id: str) -> bool:
        """True if any other resource references this ID in its properties."""
        return resource_id.lower() in self._referenced_ids

    def all(self) -> list:
        return self._resources

    def types_present(self) -> set:
        return set(self._by_type.keys())

    def _build_indexes(self) -> None:
        all_string_values: set = set()
        for res in self._resources:
            rtype = (res.get("type") or "").lower()
            rid = (res.get("id") or "").lower()
            if rtype:
                self._by_type.setdefault(rtype, []).append(res)
            if rid:
                self._by_id[rid] = res
            # Walk properties for ARM ID cross-references (exclude id/type keys)
            props = res.get("properties") or {}
            _walk_strings(props, all_string_values)

        # Extract ARM IDs from collected string values
        for val in all_string_values:
            for match in _ARM_ID_RE.finditer(val):
                self._referenced_ids.add(match.group(0).lower())
