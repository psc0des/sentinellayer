"""Adapter — maps a deterministic Finding to an existing ProposedAction.

This is the only place that couples the rules engine to the governance pipeline.
The rest of the system stays unchanged: it still consumes ProposedAction objects.
"""

import re
from src.rules.base import Finding, Severity
from src.core.models import ActionTarget, ActionType, EvidencePayload, ProposedAction, Urgency

_SEVERITY_TO_URGENCY: dict[Severity, Urgency] = {
    Severity.CRITICAL: Urgency.CRITICAL,
    Severity.HIGH: Urgency.HIGH,
    Severity.MEDIUM: Urgency.MEDIUM,
    Severity.LOW: Urgency.LOW,
}

_ACTION_MAP: dict[str, ActionType] = {
    "delete_resource": ActionType.DELETE_RESOURCE,
    "update_config": ActionType.UPDATE_CONFIG,
    "modify_nsg": ActionType.MODIFY_NSG,
    "restart_service": ActionType.RESTART_SERVICE,
    "scale_up": ActionType.SCALE_UP,
    "scale_down": ActionType.SCALE_DOWN,
    "create_resource": ActionType.CREATE_RESOURCE,
    "rotate_storage_key": ActionType.ROTATE_STORAGE_KEY,
}

_RG_RE = re.compile(r"/resourcegroups/([^/]+)/", re.IGNORECASE)


def _parse_resource_group(arm_id: str) -> str | None:
    m = _RG_RE.search(arm_id)
    return m.group(1) if m else None


def finding_to_proposal(finding: Finding, agent_id: str) -> ProposedAction:
    """Convert a deterministic Finding into a ProposedAction for the governance pipeline.

    Args:
        finding: The Finding raised by a rule.
        agent_id: ID of the operational agent that produced the scan.

    Returns:
        A ProposedAction ready for SRI™ scoring.

    Raises:
        ValueError: If finding.recommended_action is not a known ActionType string.
    """
    action_str = finding.recommended_action.lower()
    if action_str not in _ACTION_MAP:
        raise ValueError(
            f"Unknown recommended_action '{action_str}' in rule {finding.rule_id}. "
            f"Known values: {list(_ACTION_MAP)}"
        )

    urgency = _SEVERITY_TO_URGENCY[finding.severity]
    action_type = _ACTION_MAP[action_str]
    rg = _parse_resource_group(finding.resource_id)

    target = ActionTarget(
        resource_id=finding.resource_id,
        resource_type=finding.resource_type,
        resource_group=rg,
    )

    evidence = EvidencePayload(
        context=finding.evidence,
        severity=finding.severity.value,
    )
    if finding.estimated_savings_monthly is not None:
        evidence.metrics["estimated_savings_monthly"] = finding.estimated_savings_monthly

    reason = f"[{finding.rule_id}] {finding.reason}"

    return ProposedAction(
        agent_id=agent_id,
        action_type=action_type,
        target=target,
        reason=reason,
        urgency=urgency,
        projected_savings_monthly=finding.estimated_savings_monthly,
        evidence=evidence,
    )
