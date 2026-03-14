# Innovation Studio — Project Page Content

Use this file to copy-paste into the Innovation Studio project page fields.

---

## Tagline

Because autonomous AI needs accountable AI. RuriSkry intercepts, simulates, and scores every AI agent action before it touches your infrastructure. Built for Azure. Built on Azure.

---

## Keywords

AI Governance, Multiagent Systems, MCP, Infrastructure Safety, Agentic DevOps, Change Advisory Board, Azure OpenAI Foundry, SRE, Agent Framework

---

## Description

THE PROBLEM:
In every enterprise, production changes go through a CAB — a Change Advisory Board. Every infrastructure change needs a four-eyes review. Someone senior signs off before anything touches production. That's the standard. But when AI agents start managing infrastructure — who reviews them?

Sure, there are guardrails — token limits, permission scopes, hardcoded rules. But guardrails only say what an agent can't do. Nobody's simulating what happens if it does. Nobody's scoring the blast radius before the action runs.

A cost optimizer deletes a disaster recovery VM to save $800/month, not knowing it just broke a compliance requirement. An SRE agent restarts a payment service, unaware that identical restarts caused cascade failures three times before. A deployment agent opens SSH to the public internet. Capability without accountability is dangerous.

WHAT RuriSkry DOES:
RuriSkry is a governance engine that acts as the Change Advisory Board for AI agents. When an operational agent (SRE agent, cost optimizer, deployment agent) proposes an action, RuriSkry routes it through four specialized governance agents that evaluate risk across different dimensions, producing a Skry Risk Index (SRI™):

HOW IT WORKS:
1. An operational agent proposes an action (e.g., "Delete VM-23 to save $847/month")
2. RuriSkry intercepts the proposal (via A2A protocol, MCP, or direct API call)
3. Four governance agents evaluate the action in parallel:
   • SRI:Infrastructure — Simulates blast radius using Azure Resource Graph dependency data. How many downstream services break if this resource disappears?
   • SRI:Policy — Validates against organizational governance policies and compliance baselines. Does this action violate any rules?
   • SRI:Historical — Searches past incidents via Azure AI Search for similar actions that caused failures. Has this gone wrong before?
   • SRI:Cost — Forecasts financial impact including over-optimization risk. Are the savings worth the hidden costs?
4. The Governance Decision Engine calculates an SRI™ Composite score (0-100) and issues a verdict:
   • SRI ≤ 25 → Auto-Approve (safe, execute immediately)
   • SRI 26-60 → Escalate (moderate risk, human review required)
   • SRI > 60 → Deny (high risk, blocked with full explanation)
5. Every evaluation is logged to an immutable audit trail in Cosmos DB for enterprise compliance

WHAT MAKES IT DIFFERENT:
RuriSkry is not an SRE bot or agent. It's not a cost optimizer. It's AI governing AI — a supervisory intelligence layer that makes autonomous infrastructure management safe, auditable, and accountable. It doesn't replace your agents; it makes them trustworthy.

KEY FEATURES:
• LLM-as-Decision-Maker — gpt-5-mini actively adjusts risk scores with ±30 point guardrails (no hallucination dominance)
• Three-layer detection — hardcoded Python + Microsoft Defender/Azure Policy safety nets + LLM reasoning
• LLM-driven Execution Agent — plan → human review → execute → verify → one-click rollback
• Explainable AI — 6-section verdict drilldown with counterfactual analysis ("what would change this?")
• Real-time SSE streaming — live scan logs streamed to the dashboard
• Slack notifications — DENIED/ESCALATED verdicts + Azure Monitor alerts pushed in real-time
• 793 automated tests, fully deployed on Azure Container Apps + Static Web Apps

BUILT WITH:
Microsoft Agent Framework, Azure OpenAI Foundry (gpt-5-mini), A2A SDK, MCP (FastMCP), Azure Cosmos DB, Azure Resource Graph, Azure AI Search, Azure Monitor, Azure Key Vault, Azure Container Apps, Azure Static Web Apps, React, FastAPI, Terraform

GitHub: https://github.com/psc0des/ruriskry
Live Dashboard: https://agreeable-pond-05f59310f.2.azurestaticapps.net/overview
