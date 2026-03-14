"""Infrastructure Deploy Agent — proposes deployment and configuration actions.

This is an operational agent (the governed subject). It proposes
infrastructure changes that RuriSkry evaluates before execution.

Phase 12 — Intelligent, evidence-driven agent
----------------------------------------------
The agent now investigates the real Azure environment before proposing changes:

1. Discovers NSGs with ``query_resource_graph``.
2. Inspects actual security rules with ``list_nsg_rules`` — checks for
   missing deny-all inbound rules using real NSG rule data.
3. Reviews recent change activity with ``query_activity_log`` — detects
   failed writes or suspicious recent modifications.
4. Checks resource tags and configuration with ``get_resource_details``.
5. Uses GPT-4.1 to reason about security posture before proposing actions.

The agent is environment-agnostic: accepts an optional
``target_resource_group`` parameter and can scan any Azure environment.

Microsoft Agent Framework tools (live mode)
--------------------------------------------
- ``query_resource_graph(kusto_query)`` — discover NSGs, VMs, storage, databases
- ``list_nsg_rules(nsg_resource_id)`` — inspect actual security rules
- ``get_resource_details(resource_id)`` — check tags, configuration, power state
- ``query_activity_log(resource_group, timespan)`` — review recent changes
- ``get_resource_health(resource_id)`` — Azure Platform availability signal
- ``list_advisor_recommendations(scope, category)`` — Azure Advisor Security tips
- ``propose_action(...)`` — submit a validated ProposedAction

In mock mode (USE_LOCAL_MOCKS=true) the deterministic ``_scan_rules()``
fallback runs — reads ``data/seed_resources.json`` and applies heuristics.
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_ID = "deploy-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Rule-based fallback thresholds (unchanged from Phase 8)
_SPARSE_TOPOLOGY_THRESHOLD: int = 3
_NSG_RESOURCE_TYPE = "Microsoft.Network/networkSecurityGroups"
# Tags used by the rule-based (mock/CI) path to detect lifecycle metadata.
# Classification tags (environment, criticality) are NOT lifecycle tags — they describe
# what a resource is, not how it is managed.  Any tag beyond these indicates that
# lifecycle management (backup, DR, owner, cost-centre, etc.) has been applied.
_CLASSIFICATION_TAGS: frozenset[str] = frozenset({"environment", "criticality"})

# System instructions for the framework agent.
_AGENT_INSTRUCTIONS = """\
You are a Senior Platform/Security Engineer conducting an enterprise
infrastructure security and configuration compliance review.
Every finding you submit is reviewed by the governance engine before action.

━━━ STEP 1: RESOURCE DISCOVERY ━━━
Use query_resource_graph to discover ALL security-relevant resources:
  NSGs:          Resources | where type == 'microsoft.network/networksecuritygroups'
                 | project id, name, resourceGroup, tags, properties
  VMs:           Resources | where type == 'microsoft.compute/virtualmachines'
                 | project id, name, resourceGroup, tags, sku, properties
  Storage:       Resources | where type == 'microsoft.storage/storageaccounts'
                 | project id, name, resourceGroup, tags, properties
  Databases:     Resources | where type in ('microsoft.documentdb/databaseaccounts',
                 'microsoft.sql/servers') | project id, name, resourceGroup, tags, properties
  Key Vaults:    Resources | where type == 'microsoft.keyvault/vaults'
                 | project id, name, resourceGroup, tags, properties
  Public IPs:    Resources | where type == 'microsoft.network/publicipaddresses'
                 | project id, name, resourceGroup, tags, properties, ipAddress

━━━ STEP 2: NETWORK SECURITY GROUP AUDIT ━━━
For each NSG, call list_nsg_rules to inspect every inbound Allow rule.
Check both port AND sourceAddressPrefix in each rule's nested properties.

CRITICAL (urgency=HIGH) — Internet-exposed management ports:
  sourceAddressPrefix is "*", "Any", or "Internet" AND port is:
  22 (SSH), 3389 (RDP), 5985/5986 (WinRM), 23 (Telnet), 21 (FTP), 1433 (SQL)
  → Propose modify_nsg. Reason: rule name, port, source prefix, risk description.

HIGH — Wildcard internet exposure:
  sourceAddressPrefix is "*"/"Any"/"Internet" AND destinationPortRange is "*"
  → All ports exposed to internet. Propose modify_nsg urgency=HIGH.

MEDIUM — Missing explicit deny-all:
  NSG has inbound Allow rules but no deny-all rule
  (access=Deny, destinationPortRange="*", direction=Inbound).
  → Propose modify_nsg urgency=MEDIUM. This relies on Azure implicit deny only.

━━━ STEP 3: STORAGE ACCOUNT SECURITY ━━━
Call get_resource_details on each storage account. Flag:
  - allowBlobPublicAccess = true → propose update_config (HIGH urgency).
    Reason: "Public blob access enabled — any blob container can be made public."
  - supportsHttpsTrafficOnly = false → propose update_config (HIGH urgency).
    Reason: "HTTP traffic allowed — data transmitted in plaintext."
  - minimumTlsVersion < TLS1_2 → propose update_config (MEDIUM urgency).
  - No network ACL / defaultAction = Allow → propose update_config (MEDIUM).
    Reason: "Storage account accessible from all networks — restrict to known VNets."

━━━ STEP 4: DATABASE & KEY VAULT SECURITY ━━━
Call get_resource_details on each Cosmos DB account and SQL server. Flag:
  - publicNetworkAccess = Enabled with no private endpoint → update_config (MEDIUM).
    Reason: "Database accessible from public internet — use private endpoint."
  - Cosmos DB with no IP filter and no virtual network rule → update_config (MEDIUM).
  - SQL server with no firewall rules (allow all) → update_config (HIGH urgency).

Call get_resource_details on each Key Vault. Flag:
  - enableSoftDelete = false → update_config (HIGH urgency).
    Reason: "Soft delete disabled — accidental key/secret deletion is unrecoverable."
  - enablePurgeProtection = false → update_config (MEDIUM urgency).
  - publicNetworkAccess = Enabled → update_config (MEDIUM urgency).

━━━ STEP 5: VM SECURITY POSTURE ━━━
Call get_resource_details on each VM. Flag:
  - OS disk encryption status = unencrypted (no disk encryption set applied)
    → propose update_config (MEDIUM urgency).
    Reason: "VM OS disk not encrypted at rest — data exposed if disk is detached."
  - VM using password authentication instead of SSH keys (Linux VMs)
    → propose update_config (MEDIUM urgency).
  - VM accessible via public IP with no NSG attached → update_config (HIGH).

━━━ STEP 6: RECENT CONFIGURATION CHANGES ━━━
Call query_activity_log for the resource group to identify:
  - Failed write operations on security resources (NSGs, Key Vaults, firewalls).
  - Recent security rule modifications in the last 48h.
  - Any unauthorized or unexpected resource deletions.
  Flag suspicious changes with update_config (MEDIUM urgency) for human review.

━━━ STEP 7: RESOURCE GOVERNANCE ━━━
Resources with no lifecycle or ownership tags → propose update_config (LOW).
Do NOT check for specific tag key names — tag schemas vary by organisation.
Flag resources with ZERO tags only.

━━━ URGENCY SCALE ━━━
  HIGH:   Internet-exposed management port, public blob access, HTTP-only storage,
          SQL firewall open to all, soft delete disabled on Key Vault.
  MEDIUM: Missing deny-all NSG rule, database on public network, unencrypted disk,
          storage accessible from all networks, purge protection disabled.
  LOW:    Missing tags, configuration hygiene gaps.

IMPORTANT: Propose an action for EVERY finding. Do not group multiple findings
into one proposal — each security gap needs its own governance verdict.

━━━ YOUR ROLE AND BOUNDARIES ━━━
Your ONLY job is to inspect the live Azure environment and report what you find.
You are a detection tool, not a decision-maker about what is "new" or "already known".

NEVER skip or suppress a finding because you think:
  - It was flagged in a previous scan
  - It is already being handled
  - The user probably knows about it already
  - It has been reported before

You have no memory of previous scans. Every scan is a fresh, independent
inspection of the current state. If a security gap exists right now, report it.
The governance engine handles deduplication. The human operator decides whether
to act, dismiss, or escalate. That is not your decision to make.

If allow-ssh-anywhere is open to 0.0.0.0/0 today, propose modify_nsg today.
If it is still open tomorrow, propose modify_nsg tomorrow.
Report the reality you observe. Nothing more, nothing less.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DeployAgent:
    """Proposes infrastructure deployment and configuration actions.

    In live mode (USE_LOCAL_MOCKS=false) GPT-4.1 inspects real NSG rules,
    activity logs, and resource configuration before proposing changes.

    In mock mode only the deterministic ``_scan_rules()`` runs.

    Usage::

        agent = DeployAgent()
        proposals: list[ProposedAction] = await agent.scan()

        # Scope to a specific resource group:
        proposals = await agent.scan(target_resource_group="ruriskry-prod-rg")
    """

    def __init__(
        self,
        resources_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        self._resources: dict[str, dict] = {
            r["name"]: r for r in data.get("resources", [])
        }
        self._all_resources: list[dict] = data.get("resources", [])

        self._cfg = cfg or _default_settings
        self._use_framework: bool = bool(self._cfg.azure_openai_endpoint)
        self.scan_error: str | None = None  # populated if framework call fails

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        target_resource_group: str | None = None,
    ) -> list[ProposedAction]:
        """Investigate the Azure environment and return configuration proposals.

        Args:
            target_resource_group: Optional resource group to scope the scan.
                When ``None`` the agent scans across the subscription.

        Returns:
            List of :class:`~src.core.models.ProposedAction` objects.
        """
        if self._cfg.demo_mode:
            logger.info(
                "DeployAgent: DEMO_MODE enabled — returning sample proposals "
                "for pipeline testing (set DEMO_MODE=false for real Azure scanning)."
            )
            return self._demo_proposals()

        if not self._use_framework:
            logger.info(
                "DeployAgent: no Azure OpenAI endpoint configured — "
                "returning no proposals (set AZURE_OPENAI_ENDPOINT to enable live scanning)."
            )
            return []

        self.scan_error = None
        try:
            return await self._scan_with_framework(target_resource_group)
        except Exception as exc:  # noqa: BLE001
            self.scan_error = str(exc)
            logger.warning(
                "DeployAgent: framework call failed (%s) — returning no proposals "
                "(live-mode fallback to seed data would generate false positives).",
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _scan_with_framework(
        self, target_resource_group: str | None
    ) -> list[ProposedAction]:
        """Run GPT-4.1 with security investigation tools."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient
        from src.infrastructure.azure_tools import (
            query_resource_graph_async,
            list_nsg_rules_async,
            get_resource_details_async,
            query_activity_log_async,
            get_resource_health_async,
            list_advisor_recommendations_async,
        )

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires >=2025-03-01-preview
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        proposals_holder: list[ProposedAction] = []
        scan_notes: list[str] = []  # captured tool-call results for scan log visibility

        @af.tool(
            name="query_resource_graph",
            description=(
                "Query Azure Resource Graph with a Kusto (KQL) query to discover "
                "resources. Returns JSON array with id, name, type, resourceGroup, tags."
            ),
        )
        async def tool_query_resource_graph(kusto_query: str) -> str:
            """Discover NSGs, VMs, and other resources."""
            results = await query_resource_graph_async(kusto_query)
            names = [r.get("name", "?") for r in results[:8]]
            scan_notes.append(
                f"Resource Graph query → {len(results)} resource(s) found"
                + (f": {', '.join(names)}" if names else " (none)")
            )
            return json.dumps(results, default=str)

        @af.tool(
            name="list_nsg_rules",
            description=(
                "List the security rules for an Azure Network Security Group. "
                "Returns JSON array of rules with name, port, access (Allow/Deny), "
                "priority, and direction fields."
            ),
        )
        async def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            """Inspect actual NSG security rules to check for deny-all rules."""
            rules = await list_nsg_rules_async(nsg_resource_id)
            nsg_name = nsg_resource_id.split("/")[-1]

            def _props(r: dict) -> dict:
                """Flatten properties sub-dict if present (Azure live API format)."""
                p = r.get("properties", {})
                return {**r, **p} if p else r

            flat_rules = [_props(r) for r in rules]
            inbound_allow = [
                r for r in flat_rules
                if r.get("direction", "").lower() == "inbound"
                and r.get("access", "").lower() == "allow"
            ]
            open_ports = [
                str(r.get("destinationPortRange") or r.get("port") or "?")
                for r in inbound_allow[:10]
            ]
            has_deny_all = any(
                r.get("access", "").lower() == "deny"
                and r.get("destinationPortRange") in ("*", "Any")
                and r.get("direction", "").lower() == "inbound"
                for r in flat_rules
            )
            # Flag inbound Allow rules with broad source (internet-exposed)
            _broad_sources = {"*", "any", "internet", "0.0.0.0/0"}
            internet_exposed = [
                r for r in inbound_allow
                if str(r.get("sourceAddressPrefix") or r.get("properties", {}).get("sourceAddressPrefix") or "").lower()
                in _broad_sources
            ]
            _sensitive_ports = {"22", "3389", "5985", "5986", "23", "21", "*"}
            critical_rules = [
                r for r in internet_exposed
                if str(r.get("destinationPortRange") or r.get("properties", {}).get("destinationPortRange") or "")
                in _sensitive_ports
            ]

            # Show inbound-allow rules with source+port for clear LLM signal
            rule_detail = ", ".join(
                f"{r.get('name','?')}(src={r.get('sourceAddressPrefix') or r.get('properties',{}).get('sourceAddressPrefix','?')} port={r.get('destinationPortRange') or r.get('properties',{}).get('destinationPortRange','?')})"
                for r in inbound_allow[:10]
            )
            note = (
                f"NSG '{nsg_name}': {len(rules)} custom rules | inbound Allow: {len(inbound_allow)}"
                + (f" [{rule_detail}]" if rule_detail else "")
                + (f" | ⚠ INTERNET-EXPOSED rules: {len(internet_exposed)}" if internet_exposed else "")
                + (f" | 🚨 CRITICAL open management ports: {len(critical_rules)}" if critical_rules else "")
                + (" | explicit deny-all: YES" if has_deny_all else " | no explicit deny-all")
            )
            scan_notes.append(note)

            # ── Auto-propose for CRITICAL findings (deterministic path) ──────
            # The LLM must call propose_action after reading this tool result,
            # but LLM non-determinism means it can sometimes decide "no issues"
            # on re-runs (e.g. it reasons the issue is already known/flagged).
            # Internet-exposed management ports are too dangerous to leave to
            # LLM discretion — auto-propose here so they are ALWAYS captured.
            # tool_propose_action deduplicates so the LLM calling it too is safe.
            if critical_rules:
                already_proposed = any(
                    p.target.resource_id == nsg_resource_id
                    and p.action_type == ActionType.MODIFY_NSG
                    for p in proposals_holder
                )
                if not already_proposed:
                    rule_descriptions = "; ".join(
                        f"'{r.get('name','?')}' port={r.get('destinationPortRange') or r.get('properties',{}).get('destinationPortRange','?')} src={r.get('sourceAddressPrefix') or r.get('properties',{}).get('sourceAddressPrefix','?')}"
                        for r in critical_rules[:5]
                    )
                    proposals_holder.append(ProposedAction(
                        agent_id=_AGENT_ID,
                        action_type=ActionType.MODIFY_NSG,
                        target=ActionTarget(
                            resource_id=nsg_resource_id,
                            resource_type="Microsoft.Network/networkSecurityGroups",
                        ),
                        reason=(
                            f"CRITICAL: {len(critical_rules)} internet-exposed management "
                            f"port rule(s) on NSG '{nsg_name}': {rule_descriptions}. "
                            "Each rule exposes a management port to the open internet "
                            "(source=*/Any/Internet), creating unauthorized remote access "
                            "and RCE risk. Remove or restrict these rules immediately."
                        ),
                        urgency=Urgency.HIGH,
                        nsg_change_direction="restrict",
                    ))
                    logger.info(
                        "DeployAgent: auto-proposed CRITICAL NSG fix — %d rule(s) on %s",
                        len(critical_rules), nsg_name,
                    )

            # Auto-propose for missing deny-all (MEDIUM but important)
            if not has_deny_all and inbound_allow:
                already_deny = any(
                    p.target.resource_id == nsg_resource_id
                    and p.action_type == ActionType.MODIFY_NSG
                    for p in proposals_holder
                )
                if not already_deny:
                    proposals_holder.append(ProposedAction(
                        agent_id=_AGENT_ID,
                        action_type=ActionType.MODIFY_NSG,
                        target=ActionTarget(
                            resource_id=nsg_resource_id,
                            resource_type="Microsoft.Network/networkSecurityGroups",
                        ),
                        reason=(
                            f"NSG '{nsg_name}' has {len(inbound_allow)} inbound Allow rule(s) "
                            "but no explicit deny-all inbound rule. Relies on Azure's implicit "
                            "deny only — not auditable. Add a priority-4096 deny-all rule."
                        ),
                        urgency=Urgency.MEDIUM,
                        nsg_change_direction="restrict",
                    ))
                    logger.info(
                        "DeployAgent: auto-proposed deny-all fix on %s", nsg_name,
                    )

            return json.dumps(rules, default=str)

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for an Azure resource by its ARM resource ID or "
                "short name. Returns SKU, tags, location, properties (including "
                "encryption status, authentication config, network access settings, "
                "publicNetworkAccess, enableSoftDelete, enablePurgeProtection). "
                "For VMs, also returns powerState. Use this to assess security posture."
            ),
        )
        async def tool_get_resource_details(resource_id: str) -> str:
            """Check resource tags and configuration."""
            details = await get_resource_details_async(resource_id)

            # ── Deterministic security detection ─────────────────────────────
            # The LLM interprets this JSON and should call propose_action, but
            # LLM non-determinism means it can skip findings on any run.
            # Binary security checks are too important to leave to LLM discretion.
            # Auto-propose for HIGH/CRITICAL findings; the LLM adds context.
            # tool_propose_action deduplicates, so LLM calling it too is safe.
            props = details.get("properties", {})
            res_type = (details.get("type") or "").lower()
            res_name = (details.get("name") or resource_id.split("/")[-1])
            res_rg = ""
            if "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 4 and parts[3].lower() == "resourcegroups":
                    res_rg = parts[4]

            def _auto_propose(reason: str, urgency: Urgency = Urgency.HIGH) -> None:
                """Auto-propose update_config if not already proposed for this resource."""
                if any(
                    p.target.resource_id == resource_id
                    and p.action_type == ActionType.UPDATE_CONFIG
                    for p in proposals_holder
                ):
                    return
                proposals_holder.append(ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource_id,
                        resource_type=details.get("type", "unknown"),
                        resource_group=res_rg or None,
                    ),
                    reason=reason,
                    urgency=urgency,
                ))
                logger.info("DeployAgent: auto-proposed %s on %s", urgency.value, res_name)

            # Storage account security checks
            if "storageaccounts" in res_type:
                if props.get("allowBlobPublicAccess") is True:
                    _auto_propose(
                        f"HIGH: Storage account '{res_name}' has allowBlobPublicAccess=true. "
                        "Any container can be made publicly accessible, risking data exposure. "
                        "Disable public blob access immediately."
                    )
                if props.get("supportsHttpsTrafficOnly") is False:
                    _auto_propose(
                        f"HIGH: Storage account '{res_name}' allows HTTP (unencrypted) traffic. "
                        "Data is transmitted in plaintext. Enforce HTTPS-only."
                    )

            # Key Vault security checks
            if "vaults" in res_type and "keyvault" in res_type:
                if props.get("enableSoftDelete") is False:
                    _auto_propose(
                        f"HIGH: Key Vault '{res_name}' has soft-delete disabled. "
                        "Accidental deletion of keys/secrets is permanent and unrecoverable. "
                        "Enable soft-delete immediately."
                    )

            # Database security checks (Cosmos DB, SQL)
            if any(t in res_type for t in ("databaseaccounts", "sql/servers")):
                if str(props.get("publicNetworkAccess", "")).lower() == "enabled":
                    _auto_propose(
                        f"MEDIUM: Database '{res_name}' has publicNetworkAccess=Enabled. "
                        "The database is reachable from the public internet. "
                        "Restrict to private endpoints or VNet rules.",
                        urgency=Urgency.MEDIUM,
                    )

            return json.dumps(details, default=str)

        @af.tool(
            name="query_activity_log",
            description=(
                "Query Azure Monitor activity logs for a resource group. "
                "Returns recent operations with timestamp, operation name, status "
                "(Succeeded/Failed), caller, and resource. "
                "timespan uses ISO 8601 format (e.g. 'P7D' for last 7 days)."
            ),
        )
        async def tool_query_activity_log(
            resource_group: str,
            timespan: str = "P7D",
        ) -> str:
            """Review recent changes and look for failed operations."""
            entries = await query_activity_log_async(resource_group, timespan)
            return json.dumps(entries, default=str)

        @af.tool(
            name="get_resource_health",
            description=(
                "Get Azure Resource Health status for a specific resource. "
                "Returns availabilityState (Available/Unavailable/Degraded/Unknown), "
                "a human-readable summary, reasonType, and timestamps. "
                "Use this when you need authoritative platform-level health status "
                "independent of configuration or metrics."
            ),
        )
        async def tool_get_resource_health(resource_id: str) -> str:
            """Check Azure Platform health signal for a resource."""
            health = await get_resource_health_async(resource_id)
            return json.dumps(health, default=str)

        @af.tool(
            name="list_advisor_recommendations",
            description=(
                "List Azure Advisor recommendations for the subscription or a scoped resource group. "
                "Returns recommendations with category (Cost/Security/HighAvailability/Performance), "
                "impact (High/Medium/Low), impactedValue (resource name), shortDescription, "
                "and remediation guidance. "
                "scope (optional): filter by resource group name. "
                "category (optional): filter by one category — e.g. 'Security'."
            ),
        )
        async def tool_list_advisor_recommendations(
            scope: str = "", category: str = ""
        ) -> str:
            """Retrieve pre-computed Microsoft Advisor recommendations."""
            recs = await list_advisor_recommendations_async(
                scope=scope or None, category=category or None
            )
            return json.dumps(recs, default=str)

        @af.tool(
            name="propose_action",
            description=(
                "Submit a security or configuration governance proposal. "
                "action_type must be one of: modify_nsg, update_config, create_resource, "
                "scale_up, scale_down, delete_resource, restart_service. "
                "urgency must be one of: low, medium, high."
            ),
        )
        def tool_propose_action(
            resource_id: str,
            action_type: str,
            reason: str,
            urgency: str = "medium",
            resource_type: str = "",
            resource_group: str = "",
        ) -> str:
            """Validate and record a ProposedAction."""
            try:
                action_type_enum = ActionType(action_type.lower())
            except ValueError:
                valid = [e.value for e in ActionType]
                return f"ERROR: Invalid action_type '{action_type}'. Valid: {valid}"
            try:
                urgency_enum = Urgency(urgency.lower())
            except ValueError:
                urgency_enum = Urgency.MEDIUM

            # Deduplicate: CRITICAL NSG findings are auto-proposed by tool_list_nsg_rules.
            # If the LLM also calls propose_action for the same (resource_id, action_type),
            # skip it silently to avoid duplicate governance pipeline evaluations.
            if any(
                p.target.resource_id == resource_id and p.action_type == action_type_enum
                for p in proposals_holder
            ):
                name = resource_id.split("/")[-1]
                logger.debug("DeployAgent: deduped proposal — %s on %s", action_type, name)
                return f"Already proposed: {action_type} on {name} (governance dedup)"

            if not resource_group and "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 4 and parts[3].lower() == "resourcegroups":
                    resource_group = parts[4]
            if not resource_type and "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 7:
                    resource_type = f"{parts[6]}/{parts[7]}"

            proposal = ProposedAction(
                agent_id=_AGENT_ID,
                action_type=action_type_enum,
                target=ActionTarget(
                    resource_id=resource_id,
                    resource_type=resource_type or "unknown",
                    resource_group=resource_group or None,
                ),
                reason=reason,
                urgency=urgency_enum,
                # ADR (Phase 22F): DeployAgent is remediation-only — it detects dangerous
                # NSG rules (e.g. SSH open to 0.0.0.0/0) and proposes to restrict them.
                # "restrict" is always correct here. If future scope adds port-opening proposals,
                # this logic must be updated to derive direction from the actual proposed delta.
                nsg_change_direction="restrict" if action_type_enum == ActionType.MODIFY_NSG else None,
            )
            proposals_holder.append(proposal)
            name = resource_id.split("/")[-1]
            logger.info("DeployAgent: proposal submitted — %s on %s", action_type, name)
            return f"Proposal submitted: {action_type} on {name}"

        agent = client.as_agent(
            name="deploy-agent",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[
                tool_query_resource_graph,
                tool_list_nsg_rules,
                tool_get_resource_details,
                tool_query_activity_log,
                tool_get_resource_health,
                tool_list_advisor_recommendations,
                tool_propose_action,
            ],
        )

        rg_scope = (
            f"in resource group '{target_resource_group}'"
            if target_resource_group
            else "across the Azure environment"
        )
        from src.infrastructure.llm_throttle import run_with_throttle
        await run_with_throttle(
            agent.run,
            f"Conduct a full 7-domain security and configuration compliance audit {rg_scope}. "
            "Follow ALL steps in your instructions: "
            "(1) Discover ALL resource types — NSGs, VMs, storage accounts, databases, Key Vaults, public IPs. "
            "(2) Audit NSG rules for internet-exposed management ports and missing deny-all. "
            "(3) Review storage account security settings (public blob access, HTTPS, TLS, network ACLs). "
            "(4) Check database and Key Vault configuration (publicNetworkAccess, purge protection, soft delete). "
            "(5) Assess VM security posture (disk encryption, auth type, public IP with no NSG). "
            "(6) Query activity logs for suspicious or failed changes in the last 48h. "
            "(7) Flag resources with zero tags. "
            "Propose an action for EVERY finding you discover.",
        )

        # Expose tool-call notes for the dashboard live log.
        self.scan_notes: list[str] = scan_notes

        # ── Azure Advisor safety net ─────────────────────────────────────
        # Our hardcoded checks cover NSGs, storage, Key Vaults, and databases.
        # But Azure has 200+ service types — we can't write Python for each one.
        # Microsoft maintains security best-practice checks for ALL services
        # via Azure Advisor. We call it deterministically after the LLM scan
        # and auto-propose for every HIGH-impact Security recommendation the
        # LLM missed. This gives us full-service coverage without maintaining
        # per-service detection code.
        pre_advisor_count = len(proposals_holder)
        try:
            advisor_recs = await list_advisor_recommendations_async(
                scope=target_resource_group or None,
                category="Security",
            )
            advisor_high = [
                r for r in advisor_recs
                if str(r.get("impact", "")).lower() == "high"
            ]
            for rec in advisor_high:
                impacted = rec.get("impactedValue") or rec.get("impacted_resource") or ""
                rec_id = rec.get("id", "")
                short_desc = (
                    rec.get("shortDescription", {}).get("problem", "")
                    if isinstance(rec.get("shortDescription"), dict)
                    else str(rec.get("shortDescription", ""))
                ) or rec.get("description", "")

                # Build a resource_id — Advisor gives impactedValue (resource name)
                # and sometimes the full resource ID in the recommendation id.
                resource_id = ""
                if rec_id and "/providers/" in rec_id:
                    # Extract the resource portion from the recommendation ARM ID
                    idx = rec_id.find("/providers/Microsoft.Advisor")
                    if idx > 0:
                        resource_id = rec_id[:idx]
                if not resource_id:
                    resource_id = impacted or rec_id

                if not resource_id or not short_desc:
                    continue

                # Skip if LLM or hardcoded checks already proposed for this resource
                already = any(
                    p.target.resource_id == resource_id
                    or (impacted and impacted in (p.target.resource_id or ""))
                    for p in proposals_holder
                )
                if already:
                    continue

                proposals_holder.append(ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource_id,
                        resource_type=rec.get("impactedField", "unknown"),
                    ),
                    reason=f"ADVISOR-HIGH: {short_desc}",
                    urgency=Urgency.HIGH,
                ))

            advisor_added = len(proposals_holder) - pre_advisor_count
            if advisor_high:
                scan_notes.append(
                    f"Azure Advisor: {len(advisor_high)} HIGH-impact Security recommendation(s) "
                    f"({advisor_added} new, {len(advisor_high) - advisor_added} already covered)"
                )
        except Exception as exc:
            logger.warning("DeployAgent: Advisor safety net failed: %s", exc)
            scan_notes.append(f"Azure Advisor: unavailable ({exc})")

        # ── Post-scan integrity log ──────────────────────────────────────
        # Count how many proposals came from each detection path.
        # This lets operators audit whether the LLM is pulling its weight.
        auto_count = sum(
            1 for p in proposals_holder
            if p.reason.startswith(("CRITICAL:", "HIGH:", "MEDIUM:", "NSG '", "ADVISOR-HIGH:"))
        )
        llm_count = len(proposals_holder) - auto_count
        if proposals_holder:
            scan_notes.append(
                f"Scan complete — {len(proposals_holder)} proposal(s): "
                f"{auto_count} deterministic, {llm_count} LLM-originated"
            )
        else:
            scan_notes.append(
                "Scan complete — no actionable issues found in Azure environment."
            )

        return proposals_holder

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Demo mode — realistic sample proposals for pipeline testing
    # ------------------------------------------------------------------

    def _demo_proposals(self) -> list[ProposedAction]:
        """Return 2 realistic sample proposals for DEMO_MODE=true."""
        return [
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.MODIFY_NSG,
                target=ActionTarget(
                    resource_id="nsg-demo-prod",
                    resource_type="Microsoft.Network/networkSecurityGroups",
                ),
                reason=(
                    "[DEMO] Port 22 (SSH) open to 0.0.0.0/0 on nsg-demo-prod. "
                    "No deny-all inbound rule at priority 4096. "
                    "Recommend adding deny-all rule to reduce attack surface."
                ),
                urgency=Urgency.HIGH,
                nsg_change_direction="restrict",
            ),
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.UPDATE_CONFIG,
                target=ActionTarget(
                    resource_id="storage-demo-01",
                    resource_type="Microsoft.Storage/storageAccounts",
                ),
                reason=(
                    "[DEMO] Storage account storage-demo-01 has no lifecycle management tags "
                    "(no backup policy, owner, or DR designation). "
                    "Adding environment and criticality metadata recommended."
                ),
                urgency=Urgency.LOW,
            ),
        ]

    # Deterministic rule-based scan (fallback / mock mode)
    # ------------------------------------------------------------------

    def _scan_rules(self) -> list[ProposedAction]:
        """Run all detection rules and aggregate deployment proposals."""
        proposals: list[ProposedAction] = []
        proposals.extend(self._detect_nsg_without_deny_all())
        proposals.extend(self._detect_missing_lifecycle_tags())
        proposals.extend(self._detect_sparse_topology())
        logger.info(
            "DeployAgent: scan complete — %d deployment proposals generated", len(proposals)
        )
        return proposals

    # ------------------------------------------------------------------
    # Detection rules (rule-based path)
    # ------------------------------------------------------------------

    def _detect_nsg_without_deny_all(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            if resource.get("type") != _NSG_RESOURCE_TYPE:
                continue
            tags = resource.get("tags", {})
            has_deny_all = tags.get("deny_all_inbound") == "true"
            if has_deny_all:
                continue
            reason = (
                f"NSG '{resource['name']}' does not have an explicit deny-all "
                "inbound rule documented in its tags. "
                "Best practice requires a priority-4096 deny-all rule to ensure "
                "the security posture is intentional and auditable. "
                "Propose adding the deny-all rule via NSG modification."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.MODIFY_NSG,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_monthly_cost=resource.get("monthly_cost"),
                    ),
                    reason=reason,
                    urgency=Urgency.MEDIUM,
                    nsg_change_direction="restrict",
                )
            )
            logger.info("DeployAgent: NSG without deny-all — '%s'", resource["name"])
        return proposals

    def _detect_missing_lifecycle_tags(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            tags = resource.get("tags", {})
            if tags.get("environment") != "production":
                continue
            # A resource is considered adequately tagged if it has ANY tag beyond
            # the classification keys (environment, criticality).  Organisation-
            # specific lifecycle tag key names are intentionally not checked here.
            has_lifecycle_tag = any(k not in _CLASSIFICATION_TAGS for k in tags)
            if has_lifecycle_tag:
                continue
            reason = (
                f"Production resource '{resource['name']}' has no lifecycle "
                "management tags. Resources tagged only with classification keys "
                "(environment, criticality) lack ownership and lifecycle context "
                "(e.g. backup policy, DR designation, cost centre, owner). "
                "Propose adding lifecycle metadata via config update."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_monthly_cost=resource.get("monthly_cost"),
                    ),
                    reason=reason,
                    urgency=Urgency.LOW,
                )
            )
            logger.info("DeployAgent: missing lifecycle tags — '%s'", resource["name"])
        return proposals

    def _detect_sparse_topology(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        if len(self._all_resources) >= _SPARSE_TOPOLOGY_THRESHOLD:
            return proposals
        reason = (
            f"Resource topology contains only {len(self._all_resources)} resource(s), "
            f"below the observability threshold ({_SPARSE_TOPOLOGY_THRESHOLD}). "
            "No monitoring or observability resource is likely deployed. "
            "Propose creating a Log Analytics workspace to provide infrastructure "
            "visibility and enable alert-driven governance."
        )
        proposals.append(
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.CREATE_RESOURCE,
                target=ActionTarget(
                    resource_id="new-log-analytics-workspace",
                    resource_type="Microsoft.OperationalInsights/workspaces",
                ),
                reason=reason,
                urgency=Urgency.LOW,
            )
        )
        logger.info("DeployAgent: sparse topology — proposing observability resource")
        return proposals
