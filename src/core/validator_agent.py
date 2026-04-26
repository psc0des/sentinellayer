"""Phase 34F — A2 Validator Agent.

Produces a ValidatorBrief for a proposed remediation action before the user
commits to executing it.  The brief is a conservative critic assessment that
summarises what the az command will do, lists caveats, and assigns a risk level.
It is displayed in the ConfirmationModal before the user clicks "Confirm".

Safety constraints (mandatory — do not weaken):
  - Hard 5s asyncio.wait_for timeout, separate from global llm_timeout=600s.
    The global timeout is for multi-step agent loops; 5s is appropriate for a
    single critic call that a modal spinner is waiting on.
  - Never blocks execution.  validator_status="unavailable" shows a warning and
    enables the Confirm button so the user can still proceed.
  - Mock mode (cfg.use_local_mocks=True) returns a deterministic brief
    immediately without making any LLM call — tests and demos always work.
  - api_version="2025-03-01-preview" — matches all Foundry calls in the
    codebase.  DO NOT change.

Infrastructure note (Phase 34F):
  The validator uses DefaultAzureCredential, which picks up the Container App's
  SystemAssigned Managed Identity in production.  That identity already has the
  Reader role on the target subscription (from azurerm_role_assignment.subscription_reader
  in terraform-core/main.tf), so no additional infrastructure is needed.
  A dedicated read-only validator MI can be added later for tighter blast-radius
  isolation, but is not required for correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

from src.core.models import ProposedAction, ValidatorBrief

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a conservative Azure infrastructure safety reviewer.

Your role: Given a proposed az CLI command about to run against a production
Azure environment, produce a terse, honest safety brief.

Output ONLY valid JSON (no markdown fences, no preamble) with this exact schema:
{
  "summary": "<one sentence ≤20 words: what the command does in plain English>",
  "caveats": ["<caveat 1>", "<caveat 2>", ...],
  "risk_level": "low" | "medium" | "high"
}

Guidelines:
- Be specific to the resource type and operation — not generic.
- caveats: 2-5 items, each ≤15 words, actionable (what to check before/after).
- risk_level reflects blast radius, reversibility, and data-plane impact.
- Do not hallucinate resource names; use the ones provided.
- Do not include markdown in JSON values.
"""


def _user_prompt(action: ProposedAction, resolved_call: dict) -> str:
    argv = resolved_call.get("argv", [])
    cmd = " ".join(argv) if argv else resolved_call.get("sdk_method", str(resolved_call))
    return (
        f"Action type: {action.action_type.value}\n"
        f"Resource ID: {action.target.resource_id}\n"
        f"Resource type: {action.target.resource_type}\n"
        f"Reason: {action.reason}\n"
        f"Command to execute: {cmd}\n\n"
        "Produce the safety brief JSON."
    )


# ---------------------------------------------------------------------------
# Helper constructors
# ---------------------------------------------------------------------------

def _format_raw(summary: str, caveats: list[str], risk_level: str) -> str:
    cav_lines = "\n".join(f"- {c}" for c in caveats) if caveats else "- None identified."
    return (
        f"**Summary:** {summary}\n\n"
        f"**Caveats:**\n{cav_lines}\n\n"
        f"**Risk level:** {risk_level}"
    )


def _mock_brief(action: ProposedAction, resolved_call: dict) -> ValidatorBrief:
    """Deterministic brief for USE_LOCAL_MOCKS=True — no LLM call."""
    argv = resolved_call.get("argv", [])
    cmd_head = " ".join(argv[:5]) if argv else "az …"
    summary = f"Executes {cmd_head} on {action.target.resource_type}."
    caveats = [
        "Verify the resource exists and is in the expected state.",
        "Confirm no dependent services will be disrupted.",
        "Check the Azure activity log after execution.",
    ]
    risk_level = "medium"
    return ValidatorBrief(
        summary=summary,
        caveats=caveats,
        risk_level=risk_level,
        resource_state_at_validation={},
        validator_status="ok",
        raw_text=_format_raw(summary, caveats, risk_level) + "\n\n_[mock mode — no LLM call]_",
    )


def _unavailable_brief(reason: str = "") -> ValidatorBrief:
    note = f" ({reason})" if reason else ""
    return ValidatorBrief(
        summary="",
        caveats=[],
        risk_level="medium",
        resource_state_at_validation={},
        validator_status="unavailable",
        raw_text=f"⚠ Validator unavailable{note} — review the command carefully before proceeding.",
    )


# ---------------------------------------------------------------------------
# LLM call (runs inside asyncio.wait_for)
# ---------------------------------------------------------------------------

async def _call_llm(action: ProposedAction, resolved_call: dict, cfg) -> ValidatorBrief:
    from openai import AsyncAzureOpenAI  # noqa: PLC0415
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    # Slightly under 5s so asyncio.wait_for gets clean cancellation before HTTP timeout
    client = AsyncAzureOpenAI(
        azure_endpoint=cfg.azure_openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2025-03-01-preview",
        timeout=4.0,
    )

    resp = await client.chat.completions.create(
        model=cfg.azure_openai_deployment,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(action, resolved_call)},
        ],
        max_tokens=300,
        temperature=0.0,
    )

    raw = (resp.choices[0].message.content or "").strip()

    try:
        data = json.loads(raw)
        summary = str(data.get("summary", ""))
        caveats = [str(c) for c in data.get("caveats", [])]
        risk_level = data.get("risk_level", "medium")
        if risk_level not in ("low", "medium", "high"):
            risk_level = "medium"
        return ValidatorBrief(
            summary=summary,
            caveats=caveats,
            risk_level=risk_level,
            resource_state_at_validation={},
            validator_status="ok",
            raw_text=_format_raw(summary, caveats, risk_level),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        # LLM returned non-JSON — surface as raw_text
        return ValidatorBrief(
            summary="",
            caveats=[],
            risk_level="medium",
            resource_state_at_validation={},
            validator_status="ok",
            raw_text=raw or "⚠ Validator returned an unreadable response.",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def validate_proposed_action(
    action: ProposedAction,
    resolved_call: dict,
    decision: dict,
    cfg=None,
) -> ValidatorBrief:
    """Call the A2 Validator LLM and return a ValidatorBrief.

    Args:
        action: The ``ProposedAction`` that generated the playbook.
        resolved_call: ``{"argv": [...]}`` for Tier 3 CLI commands, or
                       ``{"sdk_method": ..., "args": {...}}`` for Tier 1 SDK.
        decision: Raw decision dict from the tracker or verdict_snapshot
                  (used for additional context; may be empty ``{}``).
        cfg: Settings override; defaults to the module singleton.

    Returns:
        :class:`~src.core.models.ValidatorBrief` — always returns, never raises.
        ``validator_status`` is ``"ok"`` on success, ``"unavailable"`` on timeout
        or any LLM error.  Execution must NOT be blocked on ``"unavailable"``.
    """
    from src.config import settings as _settings  # noqa: PLC0415
    cfg = cfg or _settings

    if cfg.use_local_mocks or not cfg.azure_openai_endpoint:
        return _mock_brief(action, resolved_call)

    try:
        return await asyncio.wait_for(
            _call_llm(action, resolved_call, cfg),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "ValidatorAgent: LLM timed out after 5s for %s %s",
            action.action_type.value,
            action.target.resource_id[:60],
        )
        return _unavailable_brief("timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ValidatorAgent: LLM error — %s", exc)
        return _unavailable_brief()
