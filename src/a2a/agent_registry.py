"""A2A Agent Registry — tracks operational agents that connect to SentinelLayer.

What is the registry?
---------------------
When an operational agent (cost-agent, monitoring-agent, deploy-agent) sends its
first A2A request to SentinelLayer, the registry records it.  After every
governance evaluation the registry updates that agent's counters (how many
actions were approved, denied, escalated).

This gives the dashboard a live view of which agents are active and how their
proposals are performing.

Storage modes
-------------
Mock mode  (USE_LOCAL_MOCKS=true):
    One JSON file per agent stored in ``data/agents/``.  Works offline.

Live mode  (USE_LOCAL_MOCKS=false + COSMOS_ENDPOINT set):
    Persists to a ``governance-agents`` container in Cosmos DB — same pattern
    as ``CosmosDecisionClient`` for the decision audit trail.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import settings as _default_settings
from src.infrastructure.secrets import KeyVaultSecretResolver

logger = logging.getLogger(__name__)

_DEFAULT_AGENTS_DIR = (
    Path(__file__).parent.parent.parent / "data" / "agents"
)


class AgentRegistry:
    """Tracks connected A2A agents and their governance statistics.

    Each agent entry holds:
    - ``name``                  — agent identifier (e.g. "cost-optimization-agent")
    - ``agent_card_url``        — where to fetch its Agent Card
    - ``last_seen``             — ISO-8601 UTC timestamp of most recent request
    - ``total_actions_proposed`` — cumulative proposals sent
    - ``approval_count``        — proposals that were APPROVED
    - ``denial_count``          — proposals that were DENIED
    - ``escalation_count``      — proposals that were ESCALATED

    Usage::

        registry = AgentRegistry()
        registry.register_agent("cost-optimization-agent", "http://cost-agent:9000")
        registry.update_agent_stats("cost-optimization-agent", decision="denied")
        stats = registry.get_agent_stats("cost-optimization-agent")
    """

    def __init__(self, cfg=None, agents_dir: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._agents_dir: Path = agents_dir or _DEFAULT_AGENTS_DIR

        # Resolve Cosmos key first — same pattern as CosmosDecisionClient
        _secrets = KeyVaultSecretResolver(self._cfg)
        self._cosmos_key = _secrets.resolve(
            direct_value=self._cfg.cosmos_key,
            secret_name=getattr(self._cfg, "cosmos_key_secret_name", ""),
            setting_name="COSMOS_KEY",
        )

        # Determine whether to use local JSON mock or Cosmos DB.
        # Mirrors CosmosDecisionClient: fall back to mock if key is missing.
        self._is_mock: bool = (
            self._cfg.use_local_mocks
            or not self._cfg.cosmos_endpoint
            or not self._cosmos_key
        )

        if self._is_mock:
            if not self._cfg.use_local_mocks and self._cfg.cosmos_endpoint and not self._cosmos_key:
                logger.warning(
                    "AgentRegistry: no COSMOS_KEY available — falling back to mock mode."
                )
            self._agents_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                "AgentRegistry: LOCAL MOCK mode (JSON files at %s)", self._agents_dir
            )
        else:
            # Live Cosmos DB
            try:
                from azure.cosmos import CosmosClient  # type: ignore[import]

                client = CosmosClient(
                    url=self._cfg.cosmos_endpoint, credential=self._cosmos_key
                )
                db = client.get_database_client(self._cfg.cosmos_database)
                self._container = db.get_container_client("governance-agents")
                logger.info(
                    "AgentRegistry: connected to Cosmos DB container 'governance-agents'"
                )
            except Exception as exc:
                logger.warning(
                    "AgentRegistry: Cosmos DB unavailable (%s) — falling back to mock.",
                    exc,
                )
                self._agents_dir.mkdir(parents=True, exist_ok=True)
                self._is_mock = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_agent(
        self,
        name: str,
        agent_card_url: str = "",
    ) -> dict[str, Any]:
        """Register a new agent or update the ``last_seen`` timestamp.

        If the agent is already registered, only ``last_seen`` is updated —
        existing counters are preserved.

        Args:
            name: Unique agent identifier (e.g. ``"cost-optimization-agent"``).
            agent_card_url: URL where the agent's Agent Card can be fetched.

        Returns:
            The full registry entry dict for this agent.
        """
        existing = self._load_entry(name)

        if existing:
            existing["last_seen"] = _now_iso()
            existing["agent_card_url"] = agent_card_url or existing.get("agent_card_url", "")
            self._save_entry(existing)
            logger.info("AgentRegistry: updated last_seen for '%s'", name)
            return existing

        entry: dict[str, Any] = {
            "id": name,  # Cosmos DB requires an "id" field
            "name": name,
            "agent_card_url": agent_card_url,
            "registered_at": _now_iso(),
            "last_seen": _now_iso(),
            "total_actions_proposed": 0,
            "approval_count": 0,
            "denial_count": 0,
            "escalation_count": 0,
        }
        self._save_entry(entry)
        logger.info("AgentRegistry: registered new agent '%s'", name)
        return entry

    def get_connected_agents(self) -> list[dict[str, Any]]:
        """Return all registered agents, sorted by most-recently-seen first.

        Returns:
            List of agent entry dicts.
        """
        agents = self._load_all()
        agents.sort(key=lambda a: a.get("last_seen", ""), reverse=True)
        return agents

    def get_agent_stats(self, name: str) -> dict[str, Any] | None:
        """Return the registry entry for one agent.

        Args:
            name: Agent identifier.

        Returns:
            Entry dict, or ``None`` if the agent is not registered.
        """
        return self._load_entry(name)

    def update_agent_stats(self, name: str, decision: str) -> None:
        """Increment counters after a governance evaluation completes.

        Args:
            name: Agent identifier.
            decision: One of ``"approved"``, ``"denied"``, ``"escalated"``.
        """
        entry = self._load_entry(name)
        if entry is None:
            # Auto-register unknown agents to avoid dropped updates
            entry = self.register_agent(name)

        entry["total_actions_proposed"] = entry.get("total_actions_proposed", 0) + 1
        entry["last_seen"] = _now_iso()

        decision_lower = decision.lower()
        if decision_lower == "approved":
            entry["approval_count"] = entry.get("approval_count", 0) + 1
        elif decision_lower == "denied":
            entry["denial_count"] = entry.get("denial_count", 0) + 1
        elif decision_lower == "escalated":
            entry["escalation_count"] = entry.get("escalation_count", 0) + 1

        self._save_entry(entry)
        logger.debug(
            "AgentRegistry: updated stats for '%s' — decision=%s total=%d",
            name,
            decision,
            entry["total_actions_proposed"],
        )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _save_entry(self, entry: dict[str, Any]) -> None:
        if self._is_mock:
            path = self._agents_dir / f"{entry['name']}.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, indent=2)
        else:
            self._container.upsert_item(entry)

    def _load_entry(self, name: str) -> dict[str, Any] | None:
        if self._is_mock:
            path = self._agents_dir / f"{name}.json"
            if not path.exists():
                return None
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("AgentRegistry: could not read %s — %s", path.name, exc)
                return None
        else:
            try:
                return self._container.read_item(item=name, partition_key=name)
            except Exception:
                return None

    def _load_all(self) -> list[dict[str, Any]]:
        if self._is_mock:
            entries: list[dict[str, Any]] = []
            for path in self._agents_dir.glob("*.json"):
                try:
                    with open(path, encoding="utf-8") as fh:
                        entries.append(json.load(fh))
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(
                        "AgentRegistry: skipping %s — %s", path.name, exc
                    )
            return entries
        else:
            query = "SELECT * FROM c ORDER BY c.last_seen DESC"
            return list(
                self._container.query_items(query, enable_cross_partition_query=True)
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
