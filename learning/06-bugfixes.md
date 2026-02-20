# Learning 06 — Bug Fixes: Four Real-World Python Pitfalls

## What We Fixed

Four separate bugs, each caused by a subtle language or tooling detail. This is the kind
of debugging that separates beginners from experienced developers — none of these bugs
crash immediately; they silently produce wrong answers.

---

## Bug 1 — Same-Day Change Window (policy_agent.py)

### What was wrong

The policy agent checks whether the current time falls inside a "change freeze" window
(e.g. "no deployments Friday 22:00 – Monday 06:00").

The old `else` branch handled *all* non-wrap-around windows with the same three checks:

```python
else:
    if s_day < wd < e_day:   # strictly between start and end day
        return True
    if wd == s_day and t_min >= s_min:   # ← BUG: matches ALL remaining time on start day
        return True
    if wd == e_day and t_min < e_min:   # ← BUG: matches ALL time before end time on end day
        return True
```

When `s_day == e_day` (e.g. Monday 17:00–20:00):
- `s_day < wd < e_day` → impossible (Monday < Monday < Monday)
- `wd == s_day and t_min >= s_min` → True for **any time from 17:00 to midnight**
- `wd == e_day and t_min < e_min` → True for **any time from midnight to 20:00**

Together those two checks block the **entire Monday**, not just 17:00–20:00.

### The fix

Add a dedicated branch for same-day windows *before* the multi-day branch:

```python
elif s_day == e_day:
    # Same-day window: must be within [s_min, e_min)
    if wd == s_day and s_min <= t_min < e_min:
        return True
else:
    # Multi-day forward window — original logic is correct here
    ...
```

### Why it matters

Off-by-one (or off-by-condition) errors in time-range logic are extremely common.
Always ask: "what happens when the boundaries are equal?" That edge case is almost
always the one that breaks.

### Python concept: chained comparisons

Python allows `s_min <= t_min < e_min` as a single expression. It is equivalent to
`s_min <= t_min and t_min < e_min` but more readable and less error-prone.

---

## Bug 2 — Falsy Zero Overwritten by `or` (financial_agent.py)

### What was wrong

```python
current_cost = action.target.current_monthly_cost or (
    resource.get("monthly_cost") if resource else None
)
```

In Python, `or` returns the **right side whenever the left side is falsy**.
`0.0` is falsy. So if a resource's cost is explicitly set to `0.0` (a perfectly
valid value — maybe a free tier), `or` would discard it and fall through to the
resource-graph lookup. This makes a free resource appear to have an unknown cost.

### The fix

```python
current_cost = (
    action.target.current_monthly_cost
    if action.target.current_monthly_cost is not None
    else (resource.get("monthly_cost") if resource else None)
)
```

`is not None` checks for *absence of data*, not for falsiness. `0.0` is not `None`,
so it is kept.

### Why it matters

Using `or` as a None-fallback is a common Python shortcut that works fine for strings
and objects, but silently breaks for numeric types where `0` is a meaningful value.
Always prefer `if x is not None` when the value can legitimately be zero.

### Python concept: truthiness vs. identity

- **Truthiness** (`if x`, `x or y`): `0`, `0.0`, `""`, `[]`, `None` are all *falsy*.
- **Identity** (`x is None`): only `None` is `None`. Use this when zero is valid data.

---

## Bug 3 — Deprecated `datetime.utcnow()` (models.py)

### What was wrong

```python
timestamp: datetime = Field(default_factory=datetime.utcnow)
```

`datetime.utcnow()` was deprecated in Python 3.12 and will be removed in Python 3.14.
The problem is subtle: it returns a **naive** datetime — a datetime with no timezone
attached. If you later compare it with a timezone-aware datetime you get a `TypeError`.

### The fix

```python
from datetime import datetime, timezone

timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

`datetime.now(timezone.utc)` returns a **timezone-aware** datetime. It knows it is UTC.

### Why the lambda?

`Field(default_factory=...)` expects a *callable with no arguments* (a zero-arg
function). `datetime.now` accepts an optional `tz` argument, so we can't pass it
directly. A `lambda: datetime.now(timezone.utc)` wraps it in a zero-arg callable.

### Python concept: naive vs. aware datetimes

| Type | Example | Timezone info? |
|------|---------|---------------|
| Naive | `datetime(2024, 1, 1, 12, 0)` | No — could be any timezone |
| Aware | `datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)` | Yes — explicitly UTC |

Always use aware datetimes in production code. Naive datetimes are an ambiguity
waiting to cause a bug.

---

## Bug 4 — `asyncio_mode = "auto"` Without Declared Dependency (pyproject.toml)

### What was wrong

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

This tells pytest to treat all `async def test_*` functions as async tests
automatically — but only if `pytest-asyncio` is installed. If it is not installed,
pytest ignores the option and silently runs async tests incorrectly (or prints a
warning as we saw: `PytestConfigWarning: Unknown config option: asyncio_mode`).

The package was already listed in `requirements.txt`, but `pyproject.toml` had no
corresponding entry. This means anyone installing via `pip install -e .` (the standard
way, without consulting `requirements.txt`) would not get `pytest-asyncio`.

### The fix

```toml
[project.optional-dependencies]
test = ["pytest-asyncio>=0.23.0"]
```

Now `pip install -e ".[test]"` installs the dependency, making the config option valid
and the warning disappear.

### Python concept: optional dependency groups in pyproject.toml

Modern Python packages declare their dependencies in `pyproject.toml`:

```toml
[project.dependencies]          # always required
pytest-asyncio = ...            # wrong place for test-only deps

[project.optional-dependencies]
test = ["pytest-asyncio>=0.23.0"]   # only installed when requested
dev  = ["ruff", "mypy"]
```

Install with: `pip install -e ".[test]"` or `pip install -e ".[test,dev]"`.

---

## Design Pattern: Defensive Boundary Checks

All four bugs share a theme: code that works for the *common case* but silently
fails at a *boundary*:

| Bug | Common case | Boundary that breaks |
|-----|-------------|---------------------|
| Window logic | Multi-day windows | Same-day window (`s_day == e_day`) |
| Cost fallback | Non-zero cost | Explicit zero cost |
| Datetime | Code runs before Python 3.12 | Python 3.12+ deprecation/removal |
| Pytest config | `pytest-asyncio` is installed | Fresh install without it |

**Always ask "what happens at the edges?"** — zero, None, equal boundaries, missing
packages, deprecated APIs.

---

## Key Takeaways

1. **`or` is not the same as `is not None`** — never use `x or fallback` when `x`
   can be `0`, `False`, or `""`.
2. **Same-day ranges need their own branch** — never assume `start < end` implies
   the naive multi-day logic handles `start == end` correctly.
3. **Always use timezone-aware datetimes** — `datetime.now(timezone.utc)` over
   `datetime.utcnow()`.
4. **Declare test dependencies in `pyproject.toml`** — `requirements.txt` is a
   convention, not a guarantee. `pyproject.toml` is the authoritative source.

## What to Explore Next

Read about Python's **`datetime` module and timezone handling** — specifically the
difference between naive and aware datetimes, and how to convert between timezones
using `zoneinfo` (Python 3.9+). Official docs:
https://docs.python.org/3/library/datetime.html#aware-and-naive-objects
