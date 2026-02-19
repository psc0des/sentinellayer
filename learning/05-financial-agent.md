# 05 — Financial Impact Agent (SRI:Cost)

## What We Built

`src/governance_agents/financial_agent.py` — the **FinancialImpactAgent**.

This agent answers: *"What does this action cost, and does cutting that cost risk a much bigger loss?"*

It estimates the monthly cost delta of a proposed action, projects it over 90 days,
detects **over-optimisation** (saving $125/month at the risk of a $40,000 outage),
and computes an **SRI:Cost score (0–100)**.

---

## The Problem It Solves

Cost-optimisation agents are good at finding waste — but they can be too aggressive.
This agent is the financial conscience of SentinelLayer: it doesn't just measure how
much money an action saves; it asks whether those savings are worth the financial
*risk* they introduce.

---

## How Cost Is Estimated

The agent looks for cost data in this priority order:

| Priority | Source | Certain? |
|----------|--------|---------|
| 1 | `action.projected_savings_monthly` — the proposing agent told us | ✅ Exact |
| 2 | `action.target.current_monthly_cost` — stated in the action | ✅ Exact |
| 3 | `seed_resources.json` monthly_cost for DELETE | ✅ Exact |
| 4 | 30% of current cost for SCALE_DOWN | ⚠️ Estimated |
| 5 | 50% of current cost for SCALE_UP | ⚠️ Estimated |
| 6 | 0.0 for RESTART / MODIFY_NSG / UPDATE_CONFIG | ✅ Certain zero |

**Negative monthly change = savings. Positive = new spend.**

---

## How the SRI Score Works

### Step 1: Magnitude score

The absolute monthly change is bucketed into pts:

| `|change|` | Pts |
|---|---|
| ≥ $1,000 | 70 |
| $600–$999 | 50 |
| $300–$599 | 30 |
| $100–$299 | 15 |
| $0.01–$99 | 5 |
| $0 | 0 |

### Step 2: Action multiplier

The magnitude is scaled by how risky the action type is:

| Action | Multiplier |
|--------|-----------|
| DELETE_RESOURCE | 1.5 |
| SCALE_DOWN | 1.2 |
| UPDATE_CONFIG | 0.8 |
| SCALE_UP | 0.6 |
| CREATE_RESOURCE | 0.5 |
| RESTART / MODIFY_NSG | 0.3 |

### Step 3: Penalties

| Condition | Penalty |
|-----------|---------|
| Over-optimisation detected | +20 pts |
| Cost is an estimate (uncertain) | +10 pts |

### Step 4: Cap at 100

```
score = min(magnitude × multiplier + penalties, 100.0)
```

### Worked Examples

| Scenario | Magnitude | × Mult | +Penalties | Score | Band |
|----------|-----------|--------|-----------|-------|------|
| DELETE aks-prod ($2100, 4 svcs) | 70 | ×1.5=105 | +20 over-opt | **100** | DENIED |
| DELETE api-server-03 ($847, 3 deps) | 50 | ×1.5=75 | +20 over-opt | **95** | DENIED |
| SCALE_DOWN web-tier-01 explicit $126 | 15 | ×1.2=18 | — | **18** | APPROVED |
| SCALE_DOWN web-tier-01 estimated $126 | 15 | ×1.2=18 | +10 uncertain | **28** | ESCALATED |
| RESTART any resource | 0 | ×0.3=0 | — | **0** | APPROVED |

---

## Python Concepts Used

### 1. Returning multiple values with a `tuple`

```python
def _estimate_cost_change(
    self, action: ProposedAction, resource: dict | None
) -> tuple[float, bool]:
    ...
    return (-current_cost, False)   # (monthly_change, cost_uncertain)
```

Python functions can return multiple values at once by putting them in a **tuple**.
The caller unpacks them like this:

```python
monthly_change, cost_uncertain = self._estimate_cost_change(action, resource)
```

This is called **tuple unpacking** — Python assigns each returned value to its own
variable. It's much cleaner than returning a dict or a custom object when you only
need two values.

**Type hint:** `tuple[float, bool]` tells Python tools "this function returns a
2-element tuple where the first is a float and the second is a bool."

### 2. `list[tuple[float, float]]` as a lookup table with ordered matching

```python
_MAGNITUDE_THRESHOLDS: list[tuple[float, float]] = [
    (1000.0, 70.0),
    (600.0,  50.0),
    (300.0,  30.0),
    (100.0,  15.0),
    (0.01,    5.0),
]

@staticmethod
def _magnitude_score(abs_change: float) -> float:
    for threshold, pts in _MAGNITUDE_THRESHOLDS:
        if abs_change >= threshold:
            return pts
    return 0.0
```

This is a **look-up table with early exit**. We iterate from the highest threshold
down, and return the moment we find a match. This works because the list is ordered
from largest to smallest — so the first match IS the correct band.

The `for threshold, pts in _MAGNITUDE_THRESHOLDS:` line is **tuple unpacking in a
for loop** — each element of the list is a `(float, float)` tuple, and Python
automatically splits it into `threshold` and `pts` for you.

### 3. `f"{value:,.2f}"` — formatted number strings

```python
f"Saving ${monthly_savings:,.2f}/month risks ${recovery_cost:,.0f}..."
```

Inside f-strings, `:` introduces a **format specification**:
- `,` — add thousands separator (e.g. `10000` → `10,000`)
- `.2f` — 2 decimal places, float (e.g. `847.0` → `847.00`)
- `.0f` — 0 decimal places, float (e.g. `10000.0` → `10000`)

These make financial figures human-readable. `$10,000` is much clearer than
`$10000.0`.

### 4. `_RECOVERY_COST_PER_SERVICE: float = 10_000.0` — underscore in numbers

Python allows underscores inside numeric literals as visual separators:
```python
10_000  ==  10000   # True
1_000_000  ==  1000000   # True
```

This makes large numbers much easier to read at a glance. It's purely cosmetic —
Python ignores the underscores when evaluating the number.

### 5. Priority-ordered `if` chain (early return pattern)

```python
def _estimate_cost_change(self, action, resource):
    # 1. Explicit savings — most accurate, return immediately
    if action.projected_savings_monthly is not None:
        return (-action.projected_savings_monthly, False)

    # 2. DELETE: use current cost
    if action.action_type == ActionType.DELETE_RESOURCE:
        ...
        return (...)

    # 3. SCALE_DOWN: estimate
    if action.action_type == ActionType.SCALE_DOWN:
        ...
        return (...)

    # 4. Default: zero
    return (0.0, False)
```

Each condition returns immediately — this is **early return** (same idea as guard
clauses from lesson 03). The priority order is baked into the code structure. The
first matching condition wins, and the function never reaches the others.

This is cleaner than `if/elif/elif/else` because each branch is a self-contained
block rather than a nested chain.

---

## Design Patterns Applied

### Separation of Concerns

The `evaluate()` method does no arithmetic — it orchestrates:

```python
monthly_change, cost_uncertain = self._estimate_cost_change(action, resource)
over_opt = self._detect_over_optimisation(action, resource, monthly_change)
projection = self._build_projection(monthly_change)
score = self._calculate_score(action, monthly_change, cost_uncertain, over_opt)
```

Each private method has one job. `_estimate_cost_change` only figures out the number.
`_detect_over_optimisation` only decides if risk is present. `_calculate_score` only
does arithmetic. This makes each piece individually testable and easy to modify.

### Data Lookup Table vs. `if/elif` Chain

```python
_ACTION_MULTIPLIER: dict[ActionType, float] = {
    ActionType.DELETE_RESOURCE: 1.5,
    ActionType.SCALE_DOWN:      1.2,
    ...
}
multiplier = _ACTION_MULTIPLIER.get(action.action_type, 1.0)
```

vs. the alternative:
```python
if action.action_type == ActionType.DELETE_RESOURCE:
    multiplier = 1.5
elif action.action_type == ActionType.SCALE_DOWN:
    multiplier = 1.2
...
```

The dict version is shorter, easier to read, and easier to tune — changing a
multiplier is a one-character edit in the dict, not hunting through a chain of `elif`.

### Over-Optimisation as a Concept

The term "over-optimisation" describes a real anti-pattern in cloud operations: an
automated agent cuts costs aggressively without understanding that the resources it
removes serve critical functions. SentinelLayer catches this by cross-referencing the
dependency graph when scoring financial actions.

---

## What the Tests Cover

| Test Group | Count | Purpose |
|------------|-------|---------|
| Return type & validity | 4 | Always returns valid model with bounded score |
| Zero-cost actions | 3 | RESTART and MODIFY_NSG produce 0 change and 0 score |
| DELETE cost estimation | 4 | Graph lookup, target cost, negative change, unknown resource |
| SCALE estimation | 3 | 30%/50% estimates, positive/negative direction |
| projected_savings priority | 2 | Explicit savings override graph + uncertainty diff = 10 pts |
| Scoring bands | 4 | High-cost deletes > 60, small savings ≤ 25 |
| Over-optimisation detection | 8 | Triggered/not-triggered cases + dict structure |
| Uncertainty penalty | 2 | Unrecognised resources get +10 |
| 90-day projection | 5 | Always populated, correct math |
| Resource lookup | 1 | Full Azure ID resolves to short name |
| Dependency injection | 1 | Custom path loads correctly |

---

## Key Takeaways

1. **Return a `tuple` for multiple related values** — `(change, is_uncertain)` is
   cleaner than two separate method calls or a helper class.
2. **Tuple unpacking in loops** — `for threshold, pts in list_of_tuples:` is idiomatic
   Python and very readable.
3. **Format numbers with `{:,.2f}`** in f-strings for human-readable currency output.
4. **Underscores in number literals** (`10_000`) are a readability tool with no
   runtime cost.
5. **Dict lookup tables beat `if/elif` chains** for mapping one value to another.
6. **"Over-optimisation"** is a real operational risk — the best cost-cutting
   decision in isolation can be the worst decision in context.

---

## What to Read Next

**Topic to explore:** Cost anomaly detection with time-series data.

The current agent uses static cost data. Real Azure Cost Management exposes daily and
monthly spending trends. An upgraded version of this agent could flag actions like
*"this resource's cost has been increasing for 30 days — scaling it down now is
especially risky."*

Read about **rolling averages** and **time-series anomaly detection** — simple
concepts (a rolling average is just the average of the last N data points) that power
real cost intelligence systems.
