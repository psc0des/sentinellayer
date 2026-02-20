# 12 — The Action Interception Engine

## What We Built

We implemented `src/core/interception.py` — a file containing a single class
called `ActionInterceptor`.  This class is the **single front door** for every
governance decision in SentinelLayer.  No action gets evaluated unless it walks
through this door.

Think of it like the **reception desk at a hospital**:
- A patient (an action) arrives.
- The receptionist (ActionInterceptor) checks them in, sends them to the right
  department (the pipeline), collects the result (verdict), files the paperwork
  (audit trail), and hands the verdict back to whoever brought the patient.

---

## The Big Picture — Why Does This File Exist?

Before this file, the project had several separate components:

| Component | What It Does |
|---|---|
| `pipeline.py` | Runs 4 governance agents in parallel → GovernanceVerdict |
| `decision_tracker.py` | Writes verdicts to JSON files on disk |
| `mcp_server/server.py` | Provides an MCP interface for AI agents |

Each component did its job but they were wired together *inside the MCP server*.
That meant if another caller (a REST API, a CLI script, an operational agent)
wanted to evaluate an action, they'd have to duplicate the pipeline + tracker
wiring themselves.

The `ActionInterceptor` solves this by being **one place that does everything**:
- Calls the pipeline.
- Calls the tracker.
- Returns the verdict.

This is called the **Façade Pattern** — a single simple interface hiding several
complex components underneath.

---

## How The Code Works — Step by Step

### Step 1: The class is created with its dependencies

```python
class ActionInterceptor:
    def __init__(self, pipeline=None, tracker=None):
        self._pipeline = pipeline or SentinelLayerPipeline()
        self._tracker  = tracker  or DecisionTracker()
```

**What `__init__` is:**
`__init__` is Python's "constructor" — the code that runs automatically when you
write `ActionInterceptor()`.  It sets up the object and stores the things it
needs.

**What `self` is:**
`self` is the object itself.  When you write `self._pipeline = ...`, you are
storing a value *inside* that specific object so other methods can use it later.
The underscore (`_`) is a Python convention meaning "this is private — only this
class should use it."

**What `or` does here:**
`pipeline or SentinelLayerPipeline()` means:
- If someone passed a pipeline in, use that.
- If they passed `None` (or nothing), create a brand-new one.

This lets tests inject *fake* (mocked) pipelines without loading real data files.

---

### Step 2: The `intercept()` method — the Python entry point

```python
def intercept(self, action: ProposedAction) -> GovernanceVerdict:
    verdict = self._pipeline.evaluate(action)   # run 4 agents
    self._tracker.record(verdict)               # write audit record
    return verdict
```

**What a method is:**
A method is a function that belongs to a class.  `intercept` can only be called
on an `ActionInterceptor` object: `interceptor.intercept(action)`.

**What `-> GovernanceVerdict` means:**
The arrow `->` is a **type hint** — it tells Python (and anyone reading the code)
what type of value this method will return.  Python doesn't enforce this at
runtime; it's documentation that lives in the code itself.

**The flow in plain English:**
1. Give the action to the pipeline.  Four governance agents run in parallel.
2. The pipeline comes back with a `GovernanceVerdict` (approved/escalated/denied).
3. Hand the verdict to the tracker.  The tracker writes a JSON file to disk.
4. Return the verdict to whoever called `intercept()`.

---

### Step 3: The `intercept_from_dict()` method — the MCP entry point

```python
def intercept_from_dict(self, data: dict) -> dict:
    action = self._build_action_from_dict(data)   # validate + construct
    verdict = self.intercept(action)               # run the pipeline
    return self._format_verdict(verdict)           # flatten to plain dict
```

**Why a second entry point?**
The MCP protocol sends data as JSON.  JSON becomes a Python `dict` — a plain
mapping of text keys to values.  The problem is that our pipeline expects a
`ProposedAction` *object*, not a plain dict.  This method bridges that gap:

- Validates the dict's fields.
- Constructs a `ProposedAction` from them.
- Runs the same `intercept()` method.
- Converts the result back to a plain dict for JSON.

**What `dict` is:**
A dictionary in Python is a collection of key-value pairs:
```python
data = {
    "resource_id": "vm-23",
    "action_type": "delete_resource",
    "reason":      "VM idle for 30 days",
}
```
You access values with `data["resource_id"]` or `data.get("urgency", "medium")`
(the second form returns `"medium"` if the key is missing, instead of crashing).

---

### Step 4: The private helpers

```python
@staticmethod
def _build_action_from_dict(data: dict) -> ProposedAction:
    return ProposedAction(
        agent_id=data["agent_id"],
        action_type=ActionType(data["action_type"]),
        ...
    )
```

**What `@staticmethod` means:**
A static method is a function that lives inside a class but does **not** use
`self`.  It's like a helper function filed under the class name for organisation.
You call it like a regular method: `self._build_action_from_dict(data)` or
`ActionInterceptor._build_action_from_dict(data)`.

**What `ActionType(data["action_type"])` does:**
`ActionType` is an *Enum* (enumeration) — a fixed list of allowed values.
`ActionType("delete_resource")` converts the string `"delete_resource"` into
the enum value `ActionType.DELETE_RESOURCE`.  If the string is not in the list,
Python raises a `ValueError`.  This is how we validate user input.

---

### Step 5: The module-level singleton

```python
_interceptor: ActionInterceptor | None = None

def get_interceptor() -> ActionInterceptor:
    global _interceptor
    if _interceptor is None:
        _interceptor = ActionInterceptor()
    return _interceptor
```

**What a singleton is:**
A singleton is a design pattern where *only one instance of a class ever exists
in the program*.  The first call to `get_interceptor()` creates the
`ActionInterceptor`.  Every subsequent call returns that same object — no new
pipeline is created, no new data files are loaded.

**Why this matters for performance:**
`SentinelLayerPipeline.__init__()` loads JSON files from disk and initialises
four agents.  If we created a new pipeline on every request, that disk I/O would
happen hundreds of times per minute.  With the singleton, it happens exactly once.

**What `global` does:**
`global _interceptor` tells Python "I want to modify the variable `_interceptor`
that lives at the top of this file, not create a new local variable."  Without
this line, the assignment inside the function would create a temporary local
variable that disappears when the function ends.

---

## Python Concepts Used

| Concept | Where | What It Is |
|---|---|---|
| `class` | `ActionInterceptor` | A blueprint for creating objects with shared behaviour |
| `__init__` | Constructor | Code that runs when an object is created |
| `self` | Every method | The object itself, giving methods access to stored data |
| `@staticmethod` | Helpers | Function inside a class that doesn't need `self` |
| Type hints (`->`, `:`) | Everywhere | Documentation embedded in code; not enforced at runtime |
| `dict` | `intercept_from_dict` | Python's key-value mapping; how JSON data arrives |
| `or` for defaults | `__init__` | `a or b` returns `b` when `a` is falsy (None, 0, "") |
| `global` | Singleton | Access and modify a module-level variable from inside a function |
| `Enum` validation | `_build_action_from_dict` | Convert strings to typed enum values; raises ValueError on bad input |
| `isoformat()` | `_format_verdict` | Convert a Python datetime object to an ISO 8601 string for JSON |

---

## Design Patterns Applied

### 1 — Façade Pattern
The `ActionInterceptor` is a façade.  It hides three components (pipeline,
tracker, formatter) behind one simple interface.  Callers don't need to know
about the complexity inside.

```
Caller → ActionInterceptor.intercept()
              ├── SentinelLayerPipeline.evaluate()  (4 agents)
              ├── DecisionTracker.record()
              └── returns GovernanceVerdict
```

### 2 — Dependency Injection
The pipeline and tracker are passed *in* to `__init__`, not created inside it
by default.  This makes the class *testable* — tests inject mocks, production
code uses real objects.

```python
# Tests inject mocks:
interceptor = ActionInterceptor(pipeline=mock_pipeline, tracker=mock_tracker)

# Production uses real objects (created automatically):
interceptor = ActionInterceptor()
```

### 3 — Singleton
`get_interceptor()` ensures only one `ActionInterceptor` is ever created.
Useful for expensive-to-initialise objects shared across many requests.

---

## How We Tested It

### Why We Mock
The tests use `unittest.mock.MagicMock` to replace the real pipeline and tracker
with fake objects.  The fake pipeline always returns a pre-built verdict; the
fake tracker silently records nothing.

**What a mock is:**
A mock is a stand-in object that records calls made to it but does not actually
do anything.  After the test, you can ask the mock: "Were you called?  How many
times?  With what arguments?"

```python
mock_pipeline = MagicMock()
mock_pipeline.evaluate.return_value = my_verdict  # always return this

interceptor = ActionInterceptor(pipeline=mock_pipeline, tracker=MagicMock())
interceptor.intercept(action)

mock_pipeline.evaluate.assert_called_once_with(action)  # verify it was called
```

### Test Groups
| Test Class | What It Checks |
|---|---|
| `TestActionInterceptorConstruction` | Object can be built with injected dependencies |
| `TestIntercept` | Pipeline and tracker are both called; verdict is returned unchanged |
| `TestInterceptFromDict` | Valid dicts work; required fields are enforced; result shape is correct |
| `TestGetInterceptorSingleton` | Same instance returned; pipeline built only once |
| `TestBuildActionFromDict` | Fields from the dict land in the correct ProposedAction fields |

### Run the Tests
```bash
pytest tests/test_interception.py -v
```

**Passing output looks like:**
```
44 passed, 1 warning in 0.16s
```

**If a test fails**, the output shows you exactly which assertion failed and what
the actual vs. expected values were.

---

## Key Takeaways

1. **The Façade Pattern** is your friend when you have several components that
   always need to work together — wrap them in one object.

2. **Dependency Injection** (passing objects in instead of creating them inside)
   makes code testable.  Always think: "Can I replace this with a fake for
   testing?"

3. **Singletons** are for expensive objects that should be shared — load data
   once, reuse forever.

4. **Type hints** are not enforced by Python but they are invaluable as
   documentation for you and for your editor's auto-complete.

5. **Mocking** lets you test one layer of code without relying on all the layers
   below it.  Fast, isolated, deterministic tests are the goal.

---

## What to Explore Next

**Read about:** Python's `unittest.mock` module — specifically `patch()`.

The `patch` decorator / context manager temporarily replaces a real object with
a mock for the duration of one test.  We used it to replace
`SentinelLayerPipeline` and `DecisionTracker` in the singleton tests so the test
didn't accidentally load real data files.  Understanding `patch()` deeply will
make you extremely effective at writing fast, isolated unit tests.

Search for: **"Python unittest mock patch tutorial"** and look for examples that
show `@patch("module.ClassName")` — notice how the path is the *import path*,
not the file path.  That subtle detail trips up most beginners.
