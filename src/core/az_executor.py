"""Phase 34E — Audited az CLI Executor with allowlist.

Safety guarantees (all mandatory — do not weaken):

1. **Allowlist** — every command must match a finite set of compiled regex
   patterns.  Adding new patterns requires a code change; nothing is
   configurable at runtime.
2. **No shell expansion** — args passed as a list to ``subprocess.run``,
   never as a string, ``shell=False`` always.  ``executable_args`` from the
   ``Playbook`` model is used directly — never the display ``az_command`` string.
3. **Timeout** — every subprocess call has a hard wall-clock cap.
4. **Audit on every call** — a ``AzPlaybookExecution`` record is written to
   Cosmos (or ``data/az_executions/`` in mock mode) for live runs, dry-runs,
   and allowlist rejections alike.
5. **dry_run mode** — validates argv against allowlist and writes the audit
   record without invoking ``subprocess.run``.  For commands where
   ``supports_native_what_if=True``, ``--what-if`` is injected and the
   command IS run (read-only intent query against Azure).
6. **Mock mode** — when ``cfg.use_local_mocks=True``, ``subprocess.run`` is
   never called; a synthetic "success" result is returned for tests and demos.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from src.core.models import AzPlaybookExecution, Playbook
from src.infrastructure.cosmos_client import CosmosAzExecutionClient

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class AllowlistDeniedError(PermissionError):
    """Raised (and surfaced as HTTP 403) when no allowlist pattern matches."""


# ---------------------------------------------------------------------------
# Allowlist patterns
#
# Each pattern matches the reconstructed command string: " ".join(argv)
# Patterns cover:
#   • Phase A SDK tools (as a fallback execution path)
#   • All 10 Phase D playbook templates
#
# Azure resource naming:
#   _RN  — resource name: alphanumeric, hyphens, underscores, periods (≤90 chars)
#   _SK  — SKU: alphanumeric only (e.g. P2v2, Premium, Standard, S3)
#   _KT  — key type: Primary / Secondary / key1 / key2
#   _NC  — node / retention count: 1–4 digits
#   _CL  — Cosmos consistency level: enumerated finite set
#   _BL  — boolean: true / false
# ---------------------------------------------------------------------------

_RN = r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,88}'
_SK = r'[A-Za-z0-9]+'
_KT = r'(?:Primary|Secondary|key1|key2)'
_NC = r'[0-9]{1,4}'
_CL = r'(?:BoundedStaleness|ConsistentPrefix|Eventual|Session|Strong)'
_BL = r'(?:true|false)'

_ALLOWLIST_PATTERNS: list[re.Pattern[str]] = [p for p in (re.compile(s) for s in [
    # ── App Service / Function App ────────────────────────────────────────
    rf'^az webapp restart --name {_RN} --resource-group {_RN}$',
    rf'^az functionapp restart --name {_RN} --resource-group {_RN}$',
    rf'^az appservice plan update --name {_RN} --resource-group {_RN} --sku {_SK}$',
    # ── AKS nodepool ─────────────────────────────────────────────────────
    rf'^az aks nodepool scale --name {_RN} --cluster-name {_RN} --resource-group {_RN} --node-count {_NC}$',
    # ── Storage account keys ─────────────────────────────────────────────
    rf'^az storage account keys renew --account-name {_RN} --resource-group {_RN} --key {_KT}$',
    # ── SQL Database ──────────────────────────────────────────────────────
    rf'^az sql db update --name {_RN} --server {_RN} --resource-group {_RN} --service-objective {_SK}$',
    # ── Redis Cache ───────────────────────────────────────────────────────
    rf'^az redis force-reboot --name {_RN} --resource-group {_RN} --reboot-type AllNodes$',
    rf'^az redis update --name {_RN} --resource-group {_RN} --sku {_SK} --vm-size {_SK}$',
    rf'^az redis regenerate-keys --name {_RN} --resource-group {_RN} --key-type {_KT}$',
    # ── Key Vault ─────────────────────────────────────────────────────────
    rf'^az keyvault update --name {_RN} --resource-group {_RN} --enable-soft-delete {_BL} --retention-days {_NC}$',
    # ── Container Registry ────────────────────────────────────────────────
    rf'^az acr update --name {_RN} --resource-group {_RN} --sku {_SK}$',
    # ── Cosmos DB ─────────────────────────────────────────────────────────
    rf'^az cosmosdb update --name {_RN} --resource-group {_RN} --default-consistency-level {_CL}$',
    # ── Service Bus ───────────────────────────────────────────────────────
    rf'^az servicebus namespace update --name {_RN} --resource-group {_RN} --sku {_SK}$',
])]


# ---------------------------------------------------------------------------
# Timeouts (seconds) — keyed on az subcommand substring
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT_S = 300

_TIMEOUT_OVERRIDES: dict[str, int] = {
    "redis update":              900,   # Premium tier migration
    "servicebus namespace update": 900,
    "aks nodepool scale":        600,
    "appservice plan update":    600,
    "sql db update":             600,
    "redis force-reboot":        300,
    "cosmosdb update":           300,
    "acr update":                120,
    "webapp restart":            120,
    "functionapp restart":       120,
}


def _get_timeout(args: list[str]) -> int:
    subcommand = " ".join(args[1:5])
    for key, t in _TIMEOUT_OVERRIDES.items():
        if key in subcommand:
            return t
    return _DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_command(args: list[str]) -> bool:
    """Return True if *args* matches at least one allowlist pattern.

    Reconstructs the command string as ``" ".join(args)`` and tests it
    against every compiled pattern.  Returns False on any mismatch.

    This is the sole gate between user-supplied input and subprocess.
    Do NOT bypass it.
    """
    if not args or args[0] != "az":
        return False
    cmd = " ".join(args)
    return any(pat.fullmatch(cmd) for pat in _ALLOWLIST_PATTERNS)


async def execute_playbook(
    playbook: Playbook,
    mode: str,
    approved_by: str,
    decision_id: str,
    cfg=None,
    _cosmos: CosmosAzExecutionClient | None = None,
    validator_brief_id: str | None = None,
    validator_brief_summary: str | None = None,
    validator_brief_caveats: list[str] | None = None,
) -> AzPlaybookExecution:
    """Execute (or dry-run) a playbook command through the audited az executor.

    Args:
        playbook: The :class:`~src.core.models.Playbook` to execute.
        mode: ``"live"`` or ``"dry_run"``.
        approved_by: Identity of the user who approved (for audit trail).
        decision_id: ``action_id`` UUID from the governance verdict.
        cfg: Settings override (defaults to module singleton).
        _cosmos: Cosmos client override — injected in tests to avoid disk I/O.

    Returns:
        :class:`~src.core.models.AzPlaybookExecution` audit record.

    Raises:
        AllowlistDeniedError: If the command does not match the allowlist.
            An audit record is still written before raising.
    """
    from src.config import settings as _settings  # noqa: PLC0415
    cfg = cfg or _settings

    exec_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    cosmos = _cosmos or CosmosAzExecutionClient(cfg=cfg)
    args = list(playbook.executable_args)

    # --- 1. Allowlist validation -----------------------------------------
    allowed = validate_command(args)
    if not allowed:
        record = AzPlaybookExecution(
            execution_id=exec_id,
            decision_id=decision_id,
            resource_id=playbook.resource_id,
            action_type=playbook.action_type,
            az_command=playbook.az_command,
            executable_args=args,
            mode=mode,
            approved_by=approved_by,
            allowlist_matched=False,
            created_at=now,
            notes="REJECTED — command did not match any allowlist pattern.",
            validator_brief_id=validator_brief_id,
            validator_brief_summary=validator_brief_summary,
            validator_brief_caveats=validator_brief_caveats,
        )
        _persist(cosmos, record)
        raise AllowlistDeniedError(
            f"Command not in allowlist: {' '.join(args[:6])}…  "
            "New allowlist patterns require a code change in az_executor.py."
        )

    # --- 2. Dry-run: --what-if variant or no-op audit --------------------
    if mode == "dry_run":
        if playbook.supports_native_what_if:
            # Inject --what-if and run (read-only intent query)
            what_if_args = args + ["--what-if"]
            exit_code, stdout, stderr, duration_ms, exec_at = await _run(
                what_if_args, _get_timeout(args), cfg
            )
            notes = "--what-if injected for dry-run"
        else:
            exit_code, stdout, stderr, duration_ms, exec_at = None, "", "", None, None
            notes = "dry_run: command validated but not executed (supports_native_what_if=False)"

        record = AzPlaybookExecution(
            execution_id=exec_id,
            decision_id=decision_id,
            resource_id=playbook.resource_id,
            action_type=playbook.action_type,
            az_command=playbook.az_command,
            executable_args=what_if_args if playbook.supports_native_what_if else args,
            mode=mode,
            approved_by=approved_by,
            allowlist_matched=True,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            created_at=now,
            executed_at=exec_at,
            notes=notes,
            validator_brief_id=validator_brief_id,
            validator_brief_summary=validator_brief_summary,
            validator_brief_caveats=validator_brief_caveats,
        )
        _persist(cosmos, record)
        return record

    # --- 3. Live execution -----------------------------------------------
    exit_code, stdout, stderr, duration_ms, exec_at = await _run(
        args, _get_timeout(args), cfg
    )

    notes = ""
    if exit_code != 0:
        notes = f"az exited with code {exit_code} — see stderr"

    record = AzPlaybookExecution(
        execution_id=exec_id,
        decision_id=decision_id,
        resource_id=playbook.resource_id,
        action_type=playbook.action_type,
        az_command=playbook.az_command,
        executable_args=args,
        mode=mode,
        approved_by=approved_by,
        allowlist_matched=True,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        created_at=now,
        executed_at=exec_at,
        notes=notes,
        validator_brief_id=validator_brief_id,
        validator_brief_summary=validator_brief_summary,
        validator_brief_caveats=validator_brief_caveats,
    )
    _persist(cosmos, record)
    return record


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _persist(cosmos: CosmosAzExecutionClient, record: AzPlaybookExecution) -> None:
    """Write audit record; swallow persistence errors so executor stays available."""
    try:
        cosmos.upsert(record.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        import logging  # noqa: PLC0415
        logging.getLogger(__name__).warning(
            "AzExecutor: could not persist audit record %s — %s",
            record.execution_id[:8], exc,
        )


async def _run(
    args: list[str],
    timeout_s: int,
    cfg,
) -> tuple[int | None, str, str, int | None, datetime | None]:
    """Run subprocess (or mock) and return (exit_code, stdout, stderr, duration_ms, exec_at).

    In mock mode (``cfg.use_local_mocks=True``) the subprocess is not called;
    a synthetic success result is returned so tests and demos work without az CLI.
    """
    import time  # noqa: PLC0415

    exec_at = datetime.now(timezone.utc)
    start = time.monotonic()

    if cfg.use_local_mocks:
        duration_ms = 50
        return (
            0,
            f"[mock] {' '.join(args[:4])} … (dry-run or mock mode — no actual az call)",
            "",
            duration_ms,
            exec_at,
        )

    # Check az is available before attempting
    import shutil  # noqa: PLC0415
    if not shutil.which("az"):
        duration_ms = int((time.monotonic() - start) * 1000)
        return (
            127,
            "",
            "az CLI not found — install azure-cli in this environment (see docs/SETUP.md)",
            duration_ms,
            exec_at,
        )

    import subprocess  # noqa: PLC0415
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,  # NEVER True — safety invariant
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return result.returncode, result.stdout, result.stderr, duration_ms, exec_at
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - start) * 1000)
        return (
            -1,
            "",
            f"az command timed out after {timeout_s}s",
            duration_ms,
            exec_at,
        )
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - start) * 1000)
        return -1, "", str(exc), duration_ms, exec_at
