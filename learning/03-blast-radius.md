# 03 — Blast Radius Simulation Agent (SRI:Infrastructure)

## What We Built

`src/governance_agents/blast_radius_agent.py` — the **BlastRadiusAgent**.

This agent answers one question: *"If this action is executed, what else breaks?"*

It reads a resource dependency graph from `data/seed_resources.json` (our mock for
Azure Resource Graph), traverses the relationships around the target resource, and
produces an **SRI:Infrastructure score (0–100)** — the "blast radius" of the action.

---

## The Problem It Solves

Without blast radius analysis, an AI agent could propose "scale down aks-prod" without
knowing that `aks-prod` runs `payment-api`, `notification-service`, `order-processing`,
and `user-auth`. SentinelLayer's job is to surface that impact *before* execution.

---

## How The Score Works

| Component | Formula | Max |
|-----------|---------|-----|
| Action base | Fixed pts per action type | — |
| Criticality | Tag value → pts | 30 |
| Downstream dependents | 5 pts each | 25 |
| Hosted services | 5 pts each | 20 |
| Extra SPOFs in blast radius | 10 pts each | unlimited → cap 100 |

**All components sum together, capped at 100.**

### Score Bands

| Score | Decision |
|-------|----------|
| 0–25 | APPROVED (auto-execute) |
| 26–60 | ESCALATED (human review) |
| 61–100 | DENIED |

### Example: DELETE nsg-east

- `DELETE_RESOURCE` base: 40 pts
- `criticality: critical`: 30 pts
- `governs` 3 subnets × 5 pts: 15 pts
- **Total: 85 → DENIED** ✓

### Example: SCALE_DOWN web-tier-01

- `SCALE_DOWN` base: 15 pts
- `criticality: medium`: 10 pts
- 0 dependents: 0 pts
- **Total: 25 → APPROVED** ✓

---

## Python Concepts Used

### 1. `dict` as a lookup table (hash map)

```python
self._resources: dict[str, dict] = {
    r["name"]: r for r in data.get("resources", [])
}
```

This is a **dictionary comprehension**. Instead of a slow linear search through a list
every time we need a resource, we build a `name → resource` map once at startup.
Looking up `self._resources["nsg-east"]` is O(1) — instant, regardless of list size.

### 2. `dict.fromkeys()` for ordered deduplication

```python
return list(dict.fromkeys(affected))
```

Python dicts preserve insertion order (since Python 3.7). `dict.fromkeys(iterable)`
creates a dict where each item is a key — so duplicates are automatically removed while
the first occurrence order is preserved. It's a clean trick for "deduplicate a list
without sorting it".

### 3. `Path(__file__)` for portable file paths

```python
_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)
```

`__file__` is the path to the current `.py` file. `.parent` goes up one directory.
We go up 3 levels (agent → governance_agents → src → project root) then into `data/`.
This works regardless of what directory you run the script from — no hardcoded paths.

### 4. `str | Path | None` — Union types (Python 3.10+ syntax)

```python
def __init__(self, resources_path: str | Path | None = None) -> None:
```

`|` is the new union type syntax. This tells Python (and type checkers like mypy) that
`resources_path` can be a `str`, a `Path` object, or `None`. Before Python 3.10 you
would write `Optional[Union[str, Path]]`. The new syntax is much cleaner.

### 5. Guard clauses / early returns

```python
def _get_affected_resources(self, resource: dict | None) -> list[str]:
    if resource is None:
        return []
    ...
```

Instead of wrapping the whole function in `if resource is not None:`, we **return early**
when there's nothing to do. This keeps the happy path at the left margin and avoids
deep nesting. This is called a **guard clause** — a very common professional pattern.

### 6. `resource.get("key", default)` — safe dict access

```python
criticality = resource.get("tags", {}).get("criticality", "")
```

`dict["key"]` raises `KeyError` if the key is missing. `dict.get("key", default)` returns
the default instead. We chain two `.get()` calls: first to get the `tags` dict (defaulting
to `{}` if absent), then to get the `criticality` tag within it. Safe and clean.

---

## Design Patterns Applied

### Strategy Pattern (implicit)

Each scoring component (`action_base`, `criticality`, `dependents`, etc.) is an
independent strategy. We could easily add a new scoring factor without changing the
others. The constants `_ACTION_BASE_SCORE` and `_CRITICALITY_SCORE` are **lookup tables
that act as configuration** — changing a score requires editing a single dict entry,
not hunting through logic.

### Single Responsibility Principle

Each private method has one job:

| Method | Responsibility |
|--------|---------------|
| `_find_resource` | Translate resource_id → resource dict |
| `_get_affected_resources` | Graph traversal |
| `_get_affected_services` | Service impact |
| `_detect_spofs` | Critical resource flagging |
| `_get_affected_zones` | Zone collection |
| `_calculate_score` | Arithmetic only |
| `_build_reasoning` | Text generation only |

`evaluate()` is the orchestrator — it calls each helper and assembles the result.
No single method does more than one thing.

### Dependency Injection

```python
def __init__(self, resources_path: str | Path | None = None) -> None:
```

The `resources_path` parameter lets tests inject a custom file:

```python
agent = BlastRadiusAgent(resources_path=tmp_path / "resources.json")
```

In production the default path is used. In tests we can point at a tiny fixture file.
This is called **dependency injection** — you "inject" the dependency (the file) from
outside rather than hardcoding it. It makes the class testable in isolation.

---

## Graph Traversal Strategy

The dependency graph has four relationship types in the JSON:

```
resource.dependencies  → things THIS resource needs (upstream)
resource.dependents    → things that need THIS resource (downstream)
resource.governs       → things this resource controls (e.g. NSG → subnets)
dependency_edges       → explicit directed edges (catches relationships the
                         object-level fields miss, e.g. circular dependencies)
```

We walk all four to build a complete blast radius. This is a **breadth-first-style**
traversal, but only one hop deep — we don't recursively follow dependencies of
dependencies. That's a deliberate simplification for now; a future version could do
full BFS/DFS on the graph.

---

## What the Tests Cover

| Test | What It Proves |
|------|---------------|
| `test_score_within_bounds_for_all_action_types` | Score is always 0–100, never negative, never > 100 |
| `test_delete_scores_higher_than_scale_up` | Destructive actions score worse than conservative ones |
| `test_delete_critical_nsg_scores_above_60` | High-blast-radius scenario lands in DENIED band |
| `test_scale_down_medium_resource_scores_low` | Low-blast-radius scenario lands in APPROVED band |
| `test_delete_api_server_includes_all_dependents` | Graph traversal finds downstream resources |
| `test_nsg_governed_subnets_appear_in_affected_resources` | `governs` relationship is traversed |
| `test_delete_aks_prod_surfaces_hosted_services` | `services_hosted` field is used |
| `test_critical_target_appears_in_spofs` | `criticality: critical` flags SPOFs |
| `test_unknown_resource_returns_valid_result_without_crash` | Graceful degradation for missing resources |
| `test_resource_lookup_works_with_full_azure_id` | Long Azure resource IDs are parsed correctly |
| `test_custom_resources_path` | Dependency injection lets tests use isolated fixture data |

---

## Key Takeaways

1. **Load data once in `__init__`, query many times in `evaluate()`** — avoid repeated
   file I/O on every evaluation call.
2. **Build a lookup dict from a list at startup** — O(1) lookups instead of O(n) scans.
3. **Guard clauses keep code flat** — return early rather than nest deeply.
4. **`dict.fromkeys()` deduplicates while preserving order** — a Python idiom worth
   memorising.
5. **Scoring as a sum of independent components** — easy to tune, easy to explain,
   easy to test each part independently.

---

## What to Read Next

**Topic to explore:** Graph data structures and BFS/DFS traversal algorithms.

The current agent only goes **one hop** in the dependency graph. Real infrastructure
has multi-hop chains: `web-tier-01 → api-server-03 → aks-prod → keyvault-prod`. A true
blast radius analysis would follow the chain recursively.

Read about **Breadth-First Search (BFS)** — it's the standard algorithm for finding
"everything reachable from node X" in a graph. Python's `collections.deque` is the
standard tool for BFS queues. Understanding this will help you upgrade this agent to
do full multi-hop traversal when we connect it to Azure Resource Graph.
