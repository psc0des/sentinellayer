"""Tests for Microsoft Teams notification integration (Phase 17).

Five tests verify the complete notification lifecycle:

1. DENIED verdict → POST to Teams webhook
2. ESCALATED verdict → POST to Teams webhook
3. APPROVED verdict → no POST
4. No webhook URL → no POST (silent skip)
5. Notification failure → pipeline still returns verdict

All tests mock httpx.AsyncClient so no real HTTP calls are made.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import (
    ActionTarget,
    ActionType,
    GovernanceVerdict,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
    Urgency,
)
from src.notifications.teams_notifier import send_teams_notification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(
    resource_id: str = "/subscriptions/demo/resourceGroups/prod"
    "/providers/Microsoft.Compute/virtualMachines/vm-dr-01",
) -> ProposedAction:
    return ProposedAction(
        agent_id="cost-optimization-agent",
        action_type=ActionType.DELETE_RESOURCE,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type="Microsoft.Compute/virtualMachines",
            current_monthly_cost=847.0,
        ),
        reason="VM idle for 30 days — estimated savings $847/month",
        urgency=Urgency.HIGH,
    )


def _make_verdict(
    decision: SRIVerdict = SRIVerdict.DENIED,
    composite: float = 77.0,
) -> GovernanceVerdict:
    return GovernanceVerdict(
        action_id="test-notif-001",
        timestamp=datetime.now(timezone.utc),
        proposed_action=_make_action(),
        sentinel_risk_index=SRIBreakdown(
            sri_infrastructure=65.0,
            sri_policy=100.0,
            sri_historical=62.0,
            sri_cost=45.0,
            sri_composite=composite,
        ),
        decision=decision,
        reason="DENIED — POL-DR-001 violation. SRI 77.0 > 60.",
        agent_results={
            "policy_compliance": {
                "violations": [
                    {
                        "policy_id": "POL-DR-001",
                        "name": "Disaster Recovery Protection",
                        "rule": "Cannot delete DR resources",
                        "severity": "critical",
                    }
                ]
            }
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTeamsNotification:
    """Teams notification tests — all mock httpx so no real HTTP calls."""

    @pytest.mark.asyncio
    async def test_teams_notification_sends_on_denied(self, monkeypatch):
        """A DENIED verdict should POST an Adaptive Card to the webhook URL."""
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_webhook_url",
            "https://fake.webhook.office.com/webhook/test",
        )
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_notifications_enabled",
            True,
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.notifications.teams_notifier.httpx.AsyncClient", return_value=mock_client):
            verdict = _make_verdict(SRIVerdict.DENIED)
            result = await send_teams_notification(verdict, _make_action())

        assert result is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://fake.webhook.office.com/webhook/test"

        # Verify the payload contains Adaptive Card structure
        payload = call_args[1]["json"]
        assert payload["type"] == "message"
        assert len(payload["attachments"]) == 1
        card = payload["attachments"][0]["content"]
        assert card["type"] == "AdaptiveCard"

        # Verify the verdict badge is in the card body
        body_texts = [b.get("text", "") for b in card["body"] if b.get("type") == "TextBlock"]
        assert any("DENIED" in t for t in body_texts)

    @pytest.mark.asyncio
    async def test_teams_notification_sends_on_escalated(self, monkeypatch):
        """An ESCALATED verdict should POST an Adaptive Card to the webhook URL."""
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_webhook_url",
            "https://fake.webhook.office.com/webhook/test",
        )
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_notifications_enabled",
            True,
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.notifications.teams_notifier.httpx.AsyncClient", return_value=mock_client):
            verdict = _make_verdict(SRIVerdict.ESCALATED, composite=45.0)
            result = await send_teams_notification(verdict, _make_action())

        assert result is True
        mock_client.post.assert_called_once()

        # Verify the payload contains ESCALATED badge
        payload = mock_client.post.call_args[1]["json"]
        card = payload["attachments"][0]["content"]
        body_texts = [b.get("text", "") for b in card["body"] if b.get("type") == "TextBlock"]
        assert any("ESCALATED" in t for t in body_texts)

    @pytest.mark.asyncio
    async def test_teams_notification_skips_on_approved(self, monkeypatch):
        """An APPROVED verdict should NOT trigger any POST."""
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_webhook_url",
            "https://fake.webhook.office.com/webhook/test",
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.notifications.teams_notifier.httpx.AsyncClient", return_value=mock_client):
            verdict = _make_verdict(SRIVerdict.APPROVED, composite=14.0)
            result = await send_teams_notification(verdict, _make_action())

        assert result is True
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_teams_notification_skips_when_no_webhook(self, monkeypatch):
        """When TEAMS_WEBHOOK_URL is empty, no POST should be made."""
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_webhook_url",
            "",
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.notifications.teams_notifier.httpx.AsyncClient", return_value=mock_client):
            verdict = _make_verdict(SRIVerdict.DENIED)
            result = await send_teams_notification(verdict, _make_action())

        assert result is True
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_teams_notification_does_not_block_pipeline(self, monkeypatch):
        """Even if the notification fails, pipeline.evaluate() must return a verdict."""
        from src.core.pipeline import SentinelLayerPipeline

        pipeline = SentinelLayerPipeline()

        # Set an invalid webhook URL so publish is attempted but will fail
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_webhook_url",
            "https://broken.webhook.test/fail",
        )
        monkeypatch.setattr(
            "src.notifications.teams_notifier.settings.teams_notifications_enabled",
            True,
        )

        # Mock httpx to always raise
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("Network down"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        action = ProposedAction(
            agent_id="test-agent",
            action_type=ActionType.DELETE_RESOURCE,
            target=ActionTarget(
                resource_id="/subscriptions/demo/resourceGroups/prod"
                "/providers/Microsoft.Compute/virtualMachines/vm-23",
                resource_type="Microsoft.Compute/virtualMachines",
                current_monthly_cost=847.0,
            ),
            reason="Pipeline resilience test",
            urgency=Urgency.HIGH,
        )

        with patch("src.notifications.teams_notifier.httpx.AsyncClient", return_value=mock_client):
            verdict = await pipeline.evaluate(action)

        # Pipeline must still return a valid verdict regardless of notification failure
        assert verdict is not None
        assert verdict.decision in (SRIVerdict.APPROVED, SRIVerdict.ESCALATED, SRIVerdict.DENIED)
        assert verdict.sentinel_risk_index.sri_composite >= 0
