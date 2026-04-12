"""Phase 1B — Detection architecture tests.

Verifies three invariants of the pre-scan → prompt injection → post-scan pipeline:

1. Pre-computed findings from Microsoft APIs are injected into the LLM scan prompt
   as ``findings_text`` so the LLM investigates confirmed issues first.
2. Findings missed by the LLM are auto-proposed by the post-scan safety net
   (belt-and-suspenders: API detection always wins even if LLM skips a finding).
3. Post-scan loop deduplicates: if multiple API sources flag the same resource,
   only one proposal is created.
"""
from __future__ import annotations

import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import ProposedAction, Urgency, ActionType
from src.operational_agents.cost_agent import CostOptimizationAgent
from src.operational_agents.monitoring_agent import MonitoringAgent


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

_VM_RID = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines"


def _advisor_rec_cost(
    resource_id: str, name: str, impact: str = "High", desc: str = "Idle VM"
) -> dict:
    """Minimal Advisor Cost recommendation dict."""
    return {
        "id": f"{resource_id}/providers/Microsoft.Advisor/recommendations/abc",
        "impactedValue": name,
        "impactedField": "Microsoft.Compute/virtualMachines",
        "impact": impact,
        "shortDescription": {"problem": desc},
    }


def _policy_violation(resource_id: str, name: str, policy: str = "cost-tagging") -> dict:
    """Minimal Azure Policy non-compliant resource dict."""
    return {
        "resourceId": resource_id,
        "resourceName": name,
        "policyDefinitionName": policy,
        "policyAssignmentName": "org-baseline",
    }


def _advisor_rec_mon(
    resource_id: str, name: str, impact: str = "High", desc: str = "Enable HA"
) -> dict:
    """Minimal Advisor HighAvailability recommendation dict."""
    return {
        "id": f"{resource_id}/providers/Microsoft.Advisor/recommendations/ha",
        "impactedValue": name,
        "impactedField": "Microsoft.Compute/virtualMachines",
        "impact": impact,
        "shortDescription": {"problem": desc},
    }


def _make_cost_agent() -> CostOptimizationAgent:
    data = {"resources": [], "dependency_edges": []}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    cfg = MagicMock()
    cfg.azure_openai_endpoint = "https://fake.openai.azure.com"
    cfg.azure_openai_deployment = "gpt-4o"
    cfg.llm_timeout = 600
    cfg.demo_mode = False
    return CostOptimizationAgent(resources_path=path, cfg=cfg)


def _make_monitoring_agent() -> MonitoringAgent:
    data = {"resources": [], "dependency_edges": []}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    cfg = MagicMock()
    cfg.azure_openai_endpoint = "https://fake.openai.azure.com"
    cfg.azure_openai_deployment = "gpt-4o"
    cfg.llm_timeout = 600
    cfg.demo_mode = False
    return MonitoringAgent(resources_path=path, cfg=cfg)


# ---------------------------------------------------------------------------
# Class 1: Findings injected into LLM prompt
# ---------------------------------------------------------------------------


class TestFindingsInjectedIntoPrompt:
    """Pre-computed API findings appear in the scan prompt sent to the LLM."""

    async def test_cost_agent_advisor_findings_in_prompt(self):
        """Advisor HIGH finding description appears in the cost agent scan prompt."""
        rid = f"{_VM_RID}/vm-idle"
        agent = _make_cost_agent()
        captured: list[str] = []

        async def _capture_throttle(fn, prompt):
            captured.append(prompt)

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=_capture_throttle),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[_advisor_rec_cost(rid, "vm-idle", desc="VM is grossly overprovisioned")]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(target_resource_group="test-rg")

        assert len(captured) == 1, "run_with_throttle should be called exactly once"
        prompt = captured[0]
        assert "PRE-COMPUTED FINDINGS" in prompt, (
            "Prompt must contain the PRE-COMPUTED FINDINGS header"
        )
        assert "vm-idle" in prompt, "Finding resource name must appear in prompt"
        assert "VM is grossly overprovisioned" in prompt, "Finding description must appear in prompt"

    async def test_cost_agent_policy_findings_in_prompt(self):
        """Policy violation description appears in the cost agent scan prompt."""
        rid = f"{_VM_RID}/vm-policy"
        agent = _make_cost_agent()
        captured: list[str] = []

        async def _capture_throttle(fn, prompt):
            captured.append(prompt)

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=_capture_throttle),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[_policy_violation(rid, "vm-policy", "require-cost-tags")]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(target_resource_group="test-rg")

        assert len(captured) == 1
        prompt = captured[0]
        assert "PRE-COMPUTED FINDINGS" in prompt
        assert "vm-policy" in prompt
        assert "require-cost-tags" in prompt

    async def test_cost_agent_empty_findings_text_when_apis_return_nothing(self):
        """When APIs return nothing the prompt contains the 'no issues detected' placeholder."""
        agent = _make_cost_agent()
        captured: list[str] = []

        async def _capture_throttle(fn, prompt):
            captured.append(prompt)

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=_capture_throttle),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(target_resource_group="test-rg")

        assert len(captured) == 1
        prompt = captured[0]
        # When no findings: the placeholder text appears (not a list of resources)
        assert "No high-impact cost issues" in prompt or "No high-impact" in prompt, (
            "Empty findings should produce a 'no issues detected' placeholder, not raw findings"
        )

    async def test_monitoring_agent_advisor_findings_in_scan_mode_prompt(self):
        """Monitoring agent injects Advisor HA findings into the prompt in scan mode."""
        rid = f"{_VM_RID}/vm-ha"
        agent = _make_monitoring_agent()
        captured: list[str] = []

        async def _capture_throttle(fn, prompt):
            captured.append(prompt)

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=_capture_throttle),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[_advisor_rec_mon(rid, "vm-ha", desc="No availability zone configured")]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_defender_assessments_async",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(target_resource_group="test-rg")

        assert len(captured) == 1
        prompt = captured[0]
        assert "PRE-COMPUTED FINDINGS" in prompt
        assert "vm-ha" in prompt
        assert "No availability zone configured" in prompt

    async def test_monitoring_agent_findings_absent_in_alert_mode(self):
        """In alert mode the pre-computed findings are NOT injected into the prompt."""
        rid = f"{_VM_RID}/vm-alerted"
        agent = _make_monitoring_agent()
        captured: list[str] = []

        async def _capture_throttle(fn, prompt):
            captured.append(prompt)

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=_capture_throttle),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[_advisor_rec_mon(rid, "vm-alerted", desc="No HA zones set")]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_defender_assessments_async",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(
                alert_payload={"resource_id": rid, "metric": "Percentage CPU", "value": 99},
                target_resource_group="test-rg",
            )

        assert len(captured) == 1
        prompt = captured[0]
        # Alert mode is targeted investigation — subscription-wide findings would be noise
        assert "No HA zones set" not in prompt, (
            "Pre-computed findings must NOT appear in alert mode prompt"
        )
        assert "PRE-COMPUTED FINDINGS" not in prompt, (
            "PRE-COMPUTED FINDINGS header must NOT appear in alert mode prompt"
        )


# ---------------------------------------------------------------------------
# Class 2: Post-scan deduplication
# ---------------------------------------------------------------------------


class TestPostScanDedup:
    """Post-scan loop deduplicates proposals when multiple sources flag the same resource."""

    async def test_same_resource_flagged_by_advisor_and_policy_produces_one_proposal(self):
        """Advisor rec + Policy violation for the same resource → exactly 1 auto-proposal.

        The post-scan loop adds a proposal for the Advisor finding (resource A).
        When it reaches the Policy finding for the same resource A it finds
        proposals_holder already contains it and skips, preventing a duplicate.
        """
        rid = f"{_VM_RID}/vm-shared"
        agent = _make_cost_agent()

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=AsyncMock()),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[_advisor_rec_cost(rid, "vm-shared", desc="Idle VM")]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[_policy_violation(rid, "vm-shared", "tagging")]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            proposals = await agent.scan(target_resource_group="test-rg")

        # Both sources flag vm-shared → only 1 proposal (not 2)
        assert len(proposals) == 1, (
            f"Expected 1 proposal for vm-shared (dedup), got {len(proposals)}: "
            f"{[p.target.resource_id for p in proposals]}"
        )

    async def test_different_resources_each_get_a_proposal(self):
        """Two different resources flagged by different sources each produce a proposal."""
        rid_a = f"{_VM_RID}/vm-a"
        rid_b = f"{_VM_RID}/vm-b"
        agent = _make_cost_agent()

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=AsyncMock()),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[_advisor_rec_cost(rid_a, "vm-a", desc="Idle")]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[_policy_violation(rid_b, "vm-b", "tagging")]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            proposals = await agent.scan(target_resource_group="test-rg")

        assert len(proposals) == 2, (
            "Two distinct resources should each produce one proposal"
        )
        ids = {p.target.resource_id for p in proposals}
        assert rid_a in ids, "vm-a proposal must be present"
        assert rid_b in ids, "vm-b proposal must be present"

    async def test_monitoring_same_resource_advisor_and_policy_one_proposal(self):
        """Monitoring agent also deduplicates when Advisor + Policy flag the same resource."""
        rid = f"{_VM_RID}/vm-mon-shared"
        agent = _make_monitoring_agent()

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=AsyncMock()),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[_advisor_rec_mon(rid, "vm-mon-shared", desc="No HA")]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_defender_assessments_async",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[_policy_violation(rid, "vm-mon-shared", "ha-tags")]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            proposals = await agent.scan(target_resource_group="test-rg")

        assert len(proposals) == 1, (
            f"Expected 1 proposal for vm-mon-shared (dedup), got {len(proposals)}"
        )


# ---------------------------------------------------------------------------
# Class 3: Scan notes reflect pre-scan API work
# ---------------------------------------------------------------------------


class TestScanNotesReflectPreScan:
    """scan_notes records the pre-scan API results so callers can audit what ran."""

    async def test_cost_agent_scan_notes_include_advisor_count(self):
        """scan_notes includes how many Advisor HIGH recs were found pre-scan."""
        rid = f"{_VM_RID}/vm-noted"
        agent = _make_cost_agent()

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=AsyncMock()),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[
                    _advisor_rec_cost(rid, "vm-noted", desc="Idle"),
                    _advisor_rec_cost(f"{_VM_RID}/vm-noted2", "vm-noted2", desc="Idle2"),
                ]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(target_resource_group="test-rg")

        notes_text = "\n".join(agent.scan_notes)
        assert "2" in notes_text, "scan_notes should record the count of Advisor HIGH recs"
        assert any("Advisor" in n or "advisor" in n.lower() for n in agent.scan_notes), (
            "At least one note should mention Advisor"
        )

    async def test_cost_agent_scan_notes_include_policy_count(self):
        """scan_notes includes how many Policy violations were found pre-scan."""
        rid = f"{_VM_RID}/vm-pol"
        agent = _make_cost_agent()

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=AsyncMock()),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[_policy_violation(rid, "vm-pol", "cost-tags")]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(target_resource_group="test-rg")

        assert any("Policy" in n or "policy" in n.lower() for n in agent.scan_notes), (
            "At least one note should mention Policy"
        )

    async def test_monitoring_agent_alert_mode_has_no_pre_scan_notes(self):
        """In alert mode pre-scan APIs are skipped — no Advisor/Defender/Policy notes."""
        rid = f"{_VM_RID}/vm-alert"
        agent = _make_monitoring_agent()

        with (
            patch("openai.AsyncAzureOpenAI"),
            patch("azure.identity.DefaultAzureCredential"),
            patch("azure.identity.get_bearer_token_provider"),
            patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
            patch("src.infrastructure.llm_throttle.run_with_throttle", new=AsyncMock()),
            patch(
                "src.infrastructure.azure_tools.list_advisor_recommendations_async",
                new=AsyncMock(return_value=[_advisor_rec_mon(rid, "vm-alert")]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_defender_assessments_async",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.infrastructure.azure_tools.list_policy_violations_async",
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_oir.return_value.as_agent.return_value = MagicMock(run=AsyncMock())
            await agent.scan(
                alert_payload={"resource_id": rid, "metric": "Heartbeat", "value": 0},
                target_resource_group="test-rg",
            )

        pre_scan_notes = [
            n for n in agent.scan_notes
            if any(k in n for k in ("Pre-scan:", "Advisor", "Defender", "Policy"))
        ]
        assert len(pre_scan_notes) == 0, (
            f"Alert mode should produce no pre-scan notes, got: {pre_scan_notes}"
        )
