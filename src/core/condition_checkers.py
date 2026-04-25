"""Condition checkers for APPROVED_IF verdicts (Phase 32 Part 2).

Pure functions — no I/O, no agent state.  The condition watcher and execution
gateway call these to determine whether an ApprovalCondition is satisfied.

Auto-checkable condition types:
  - TIME_WINDOW       — current UTC time is inside the approved execution window
  - METRIC_THRESHOLD  — a current metric value is within a safe threshold

Human-required condition types (BLAST_RADIUS_CONFIRMED, OWNER_NOTIFIED,
DEPENDENCY_CONFIRMED) have no auto-checker; they are satisfied via the API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.models import ApprovalCondition


def check_time_window(condition: "ApprovalCondition", now: datetime | None = None) -> bool:
    """Return True if *now* is inside the approved execution window.

    Condition parameters:
        window_start: str  — "HH:MM" in UTC
        window_end:   str  — "HH:MM" in UTC (exclusive)
        tz:           str  — timezone name (only "UTC" supported in auto-check)

    Example:
        parameters = {"window_start": "00:00", "window_end": "06:00", "tz": "UTC"}
        → True between midnight and 06:00 UTC

    If window_start > window_end (e.g. 22:00–04:00), the window wraps midnight.
    """
    params = condition.parameters
    now = now or datetime.now(timezone.utc)
    current_minutes = now.hour * 60 + now.minute

    start_str = params.get("window_start", "00:00")
    end_str = params.get("window_end", "06:00")

    try:
        sh, sm = (int(x) for x in start_str.split(":"))
        eh, em = (int(x) for x in end_str.split(":"))
    except (ValueError, AttributeError):
        return False  # malformed params — fail safe (don't auto-promote)

    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes < end_minutes
    else:
        # Wraps midnight (e.g. 22:00–04:00)
        return current_minutes >= start_minutes or current_minutes < end_minutes


def check_metric_threshold(condition: "ApprovalCondition") -> bool:
    """Return True if the current metric is within the safe threshold.

    In live mode this would query Azure Monitor; in this implementation we
    evaluate deterministically from the condition parameters alone.  A real
    threshold check requires the resource ID and live metric data — the
    condition watcher can extend this with live reads when needed.

    Condition parameters:
        metric:        str   — metric name (informational; not queried here)
        max_threshold: float — safe upper bound for the metric value
        current_value: float — (optional) last-known metric value; if provided,
                               evaluated directly without an Azure API call

    Returns:
        True if current_value is set AND <= max_threshold, else False
        (unknown = fail safe — do not auto-promote without data).
    """
    params = condition.parameters
    max_threshold = params.get("max_threshold")
    current_value = params.get("current_value")

    if max_threshold is None or current_value is None:
        return False  # insufficient data — fail safe

    return float(current_value) <= float(max_threshold)


def check_condition(
    condition: "ApprovalCondition",
    now: datetime | None = None,
) -> bool:
    """Dispatch to the correct checker for *condition.condition_type*.

    Returns False for human-required conditions (no auto-checker available)
    and for any unknown condition type.
    """
    from src.core.models import ConditionType  # noqa: PLC0415 (avoid circular at top level)

    if not condition.auto_checkable:
        return False

    if condition.condition_type == ConditionType.TIME_WINDOW:
        return check_time_window(condition, now)
    if condition.condition_type == ConditionType.METRIC_THRESHOLD:
        return check_metric_threshold(condition)

    return False  # unknown type — fail safe
