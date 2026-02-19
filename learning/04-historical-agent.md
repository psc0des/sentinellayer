# 04 — Historical Pattern Agent (SRI:Historical)

## What We Built

`src/governance_agents/historical_agent.py` — the **HistoricalPatternAgent**.

This agent answers one question: *"Has something like this gone wrong before?"*

It loads `data/seed_incidents.json` (our mock for an Azure AI Search vector index),
scores each past incident's similarity to the proposed action, and computes an
**SRI:Historical score (0–100)** — how much past failure history resembles this action.

---

## The Problem It Solves

Without historical pattern matching, an AI governance engine has no institutional
memory. SentinelLayer remembers that the last time someone restarted `payment-api`
it caused a 2-hour outage, and it surfaces that information to a human reviewer
*before* the action is attempted again.

---

## How The Similarity Score Works

Each past incident is scored against the proposed action across 4 dimensions:

| Dimension | Weight | What It Checks |
|-----------|--------|----------------|
| Action type | 0.40 | Did the incident involve the same operation (delete, restart, etc.)? |
| Resource type | 0.30 | Is this the same Azure resource category? |
| Resource name | 0.20 | Is this the exact same resource? |
| Tag relevance | 0.10 | Do action-type keywords appear in incident tags? |

**Weights sum to 1.0** — so a perfect match scores 1.0, partial matches score less.

Only incidents scoring **≥ 0.30** are returned as "similar".

### Example — RESTART on payment-api vs INC-2025-0847

```
Action type: "restart_service" == "restart_service"  → +0.40
Resource:    managedClusters  == managedClusters      → +0.30
Name:        "payment-api" in "restart_service:payment-api" → +0.20
Tag:         "restart" in ["cascade-failure", "restart"]   → +0.10
                                                     Total = 1.00
```

---

## How The SRI Score Works

Similar incidents are converted to a risk score using severity weights:

| Severity | Weight |
|----------|--------|
| critical | 100 |
| high | 75 |
| medium | 40 |
| low | 10 |

**Formula:**
```
score = best.similarity × severity_weight[best.severity]
      + Σ each additional incident: (similarity × severity_weight × 0.20)
```
Capped at 100.

The **0.20 multiplier** for additional incidents is called "diminishing returns" —
each extra match adds less and less to the score. This prevents 5 low-quality partial
matches from inflating the score as much as one perfect match.

### Score Bands

| Score | Band | Decision |
|-------|------|----------|
| 0–25 | Low | APPROVED |
| 26–60 | Medium | ESCALATED |
| 61–100 | High | DENIED |

### Real Examples From Seed Data

| Action | Resource | Top Incident | Sim | Severity | Score |
|--------|----------|-------------|-----|----------|-------|
| RESTART | payment-api (AKS) | INC-2025-0847 | 1.0 | critical | 100 |
| MODIFY_NSG | nsg-east | INC-2025-0923 | 1.0 | critical | 100 |
| SCALE_DOWN | aks-prod | INC-2025-1089 | 1.0 | high | 75+ |
| UPDATE_CONFIG | sql-prod | INC-2025-0634 | 1.0 | medium | 40 |
| SCALE_UP | web-tier-01 (VM) | INC-2026-0012 | 0.8 | low | 8 |

---

## Python Concepts Used

### 1. List of Tuples — `list[tuple[float, dict]]`

```python
scored: list[tuple[float, dict]] = []
for incident in self._incidents:
    sim = self._compute_similarity(incident, action)
    if sim >= _SIMILARITY_THRESHOLD:
        scored.append((sim, incident))
```

A **tuple** is an immutable pair (or group) of values. Here each element is
`(similarity_score, incident_dict)`. We pair them together so we can sort by the
first element and still have access to the incident data in the second element.

`list[tuple[float, dict]]` is a **type hint** — it tells Python tools that this
list contains tuples where the first item is a `float` and the second is a `dict`.

### 2. Sorting with `key=` and a `lambda`

```python
scored.sort(key=lambda t: t[0], reverse=True)
```

`list.sort()` sorts in place. The `key=` argument tells it *what to sort by*.
`lambda t: t[0]` is an **anonymous function** — it takes a tuple `t` and returns
its first element (the similarity score). `reverse=True` sorts highest-first.

You can read it as: *"Sort the list by the first element of each tuple, biggest first."*

`lambda` is shorthand for a throwaway function you'd otherwise write as:
```python
def get_score(t):
    return t[0]
scored.sort(key=get_score, reverse=True)
```

### 3. Set Intersection — `set1 & set2`

```python
action_keywords = _ACTION_TYPE_TAGS.get(action.action_type, set())
incident_tags = {t.lower() for t in incident.get("tags", [])}
if action_keywords & incident_tags:   # non-empty = truthy
    score += _W_TAGS
```

`&` on sets returns the **intersection** — elements that exist in both.
`{"restart"} & {"cascade-failure", "restart", "circular-dependency"}` → `{"restart"}`.
A non-empty set is **truthy** in Python, so `if set_intersection:` is the idiomatic
way to ask "do these two sets share any elements?"

**Set comprehension** `{t.lower() for t in tags}` is like list comprehension but
builds a set instead. Sets are unordered and contain no duplicates. They also support
O(1) membership checks (`in`) and fast intersection.

### 4. `@staticmethod`

```python
@staticmethod
def _to_similar_incident(incident: dict, similarity: float) -> SimilarIncident:
    ...
```

A `@staticmethod` is a method that belongs to the class but **doesn't need `self`**.
It can't access instance variables (`self._incidents`) or class variables. It's just
a function that logically belongs to this class.

We use it here because `_to_similar_incident` is a pure data-conversion function —
given a dict and a float, it always returns the same `SimilarIncident`. It has no
reason to touch the agent's state.

**When to use `@staticmethod`:** when the method could theoretically be a standalone
function, but you keep it in the class for organisational clarity.

### 5. `pytest.approx` — testing floating point

```python
assert sim == pytest.approx(_W_ACTION)  # 0.40
```

You can't use `==` for floats in tests because of floating point precision issues:
`0.1 + 0.2 == 0.3` is `False` in Python (it's `0.30000000000000004`).

`pytest.approx()` compares floats with a small tolerance. By default it uses a
relative tolerance of 1e-6 — good enough for any score that isn't astronomically
large.

---

## Design Patterns Applied

### Threshold Filtering

```python
if sim >= _SIMILARITY_THRESHOLD:
    scored.append((sim, incident))
```

Rather than returning all incidents with a score of 0, we apply a **relevance
threshold** (0.30) to filter out noise. This is the same pattern used by search
engines — results below a relevance cutoff are discarded, not returned with low scores.

### Diminishing Returns (Geometric Decay)

```python
score = best.similarity × severity_weight[best]
for inc in rest:
    score += (inc.similarity × severity_weight[inc]) × 0.20
```

The primary incident carries full weight. Each additional incident contributes only
20% of what it "would" add if it were the primary. This is **diminishing returns** —
a common pattern in scoring systems to prevent score inflation.

**Why:** 5 medium-quality matches shouldn't score the same as 1 perfect critical match.

### Data Transfer Object (DTO)

```python
@staticmethod
def _to_similar_incident(incident: dict, similarity: float) -> SimilarIncident:
    return SimilarIncident(
        incident_id=incident["incident_id"],
        ...
    )
```

The `_to_similar_incident` method is a **Data Transfer Object** converter. The raw
data from the JSON file is an untyped `dict`. The `SimilarIncident` Pydantic model is
a typed, validated object. The converter bridges the two — reading raw data in,
producing typed data out. This keeps raw data handling isolated to one place.

### Open/Closed for Backend Swap

The agent's public API is:
```python
def evaluate(self, action: ProposedAction) -> HistoricalResult
```

The internal similarity engine is pure Python. When we want to switch to Azure AI
Search vector embeddings, *only* the internals of `_compute_similarity` need to change.
The `evaluate()` signature, the `HistoricalResult` model, and all the tests remain
identical. This is the **Open/Closed Principle** — open for extension, closed for
modification of the public interface.

---

## What the Tests Cover

| Test Group | Tests | Purpose |
|------------|-------|---------|
| Return type & fields | 4 | Always returns valid model with correct field types |
| No-match scenario | 2 | Unknown resource types → score=0, correct reasoning |
| High-risk scenarios | 8 | Specific seed incidents found and score in correct band |
| Low-risk scenario | 4 | Low-severity precedent → score stays in APPROVED band |
| Output fields | 3 | most_relevant_incident, recommended_procedure correctness |
| SimilarIncident fields | 3 | similarity_score set, sorted desc, all fields populated |
| Reasoning | 2 | Incident ID and count mentioned in reasoning text |
| Similarity whitebox | 6 | Each dimension contributes the exact expected weight |
| Resource ID lookup | 1 | Full Azure path resolves to short name |
| Dependency injection | 2 | Custom path + empty file both handled correctly |

---

## Key Takeaways

1. **Build your index at `__init__` time, query it in `evaluate()`** — load the JSON
   file once, not on every call.
2. **`list[tuple[T1, T2]]` for associating a score with its source data** — lets you
   sort and iterate without losing the original object.
3. **`set1 & set2` for "do any tags match?" questions** — fast, Pythonic, readable.
4. **`@staticmethod` for pure data-conversion helpers** — no `self` needed = signal
   that the method is stateless.
5. **Diminishing returns prevent score inflation** — multiply each secondary item by a
   fraction so many weak matches don't overwhelm one strong match.

---

## What to Read Next

**Topic to explore:** Vector embeddings and semantic similarity search.

The current similarity engine is **rule-based** — it only matches exact action types,
resource types, and resource names. It would miss: *"Scaling down the order-processing
service"* matching *"Premature scale-down of the payment API"* because the action
types and resource names differ, even though the risk is identical.

Real production similarity search uses **vector embeddings** — each text document is
converted to a list of numbers (a vector) that captures its meaning. Similar
*concepts* end up close in vector space, even with different words.

Azure AI Search supports vector search. OpenAI's `text-embedding-3-small` model
generates embeddings. Read: [OpenAI embeddings guide](https://platform.openai.com/docs/guides/embeddings)
to understand how your future upgrade of this agent will work.
