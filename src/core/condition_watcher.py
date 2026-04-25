"""Background condition watcher for APPROVED_IF verdicts (Phase 32 Part 2).

Polls all ExecutionRecord records with status=CONDITIONAL every POLL_INTERVAL_S
seconds.  For each record, runs the auto-checker on every unsatisfied
auto-checkable condition.  If all conditions on a record become satisfied, the
execution gateway promotes the record to manual_required automatically.

Human-required conditions (BLAST_RADIUS_CONFIRMED, OWNER_NOTIFIED,
DEPENDENCY_CONFIRMED) are skipped — they require an explicit API call.

Usage:
    watcher = ConditionWatcher(gateway)
    asyncio.create_task(watcher.run())     # started in FastAPI lifespan
    watcher.stop()                         # called during shutdown
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

POLL_INTERVAL_S: int = 60  # seconds between polls


class ConditionWatcher:
    """Async background task that drives auto-condition promotion."""

    def __init__(self, gateway) -> None:
        self._gateway = gateway
        self._running = False

    async def run(self) -> None:
        """Poll loop — runs until stop() is called."""
        self._running = True
        logger.info("ConditionWatcher: started (interval=%ds)", POLL_INTERVAL_S)
        while self._running:
            try:
                await self._poll()
            except Exception as exc:  # noqa: BLE001
                logger.error("ConditionWatcher: poll error — %s", exc)
            await asyncio.sleep(POLL_INTERVAL_S)
        logger.info("ConditionWatcher: stopped")

    def stop(self) -> None:
        """Signal the watcher loop to exit after the current sleep."""
        self._running = False

    async def _poll(self) -> None:
        """Check all conditional records and auto-promote where possible."""
        records = self._gateway.get_conditional_records()
        if not records:
            return

        for record in records:
            for idx, cond in enumerate(record.conditions):
                if cond.satisfied or not cond.auto_checkable:
                    continue
                try:
                    was_satisfied, _ = self._gateway.check_condition_auto(
                        record.execution_id, idx
                    )
                    if was_satisfied:
                        logger.info(
                            "ConditionWatcher: %s condition[%d] '%s' satisfied",
                            record.execution_id[:8], idx, cond.condition_type.value,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ConditionWatcher: %s condition[%d] check failed — %s",
                        record.execution_id[:8], idx, exc,
                    )
