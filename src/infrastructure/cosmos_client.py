"""Cosmos DB client — governance decision audit trail.

Mode selection
--------------
Mock mode (USE_LOCAL_MOCKS=true, or endpoint not set):
    Reads and writes decision JSON files under ``data/decisions/``.
    This is identical to what ``DecisionTracker`` does internally —
    the CosmosDecisionClient is the infrastructure-layer abstraction
    that will be swapped for real Cosmos DB in production.

Azure mode (USE_LOCAL_MOCKS=false + COSMOS_ENDPOINT set):
    Uses ``azure-cosmos`` SDK to read/write to the ``governance-decisions``
    container in the ``sentinellayer`` Cosmos DB database.

Usage::

    from src.infrastructure.cosmos_client import CosmosDecisionClient

    client = CosmosDecisionClient()
    client.upsert({"id": "abc-123", "resource_id": "vm-23", "decision": "denied"})
    recent = client.get_recent(limit=5)
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.infrastructure.secrets import KeyVaultSecretResolver

logger = logging.getLogger(__name__)

_DEFAULT_DECISIONS_DIR = (
    Path(__file__).parent.parent.parent / "data" / "decisions"
)


class CosmosDecisionClient:
    """Read/write governance decisions from Cosmos DB or local JSON files.

    Args:
        cfg: Settings object (defaults to module singleton from ``src.config``).
        decisions_dir: Override the local JSON directory (used in tests to
            write to a temp directory instead of ``data/decisions/``).
    """

    def __init__(self, cfg=None, decisions_dir: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._decisions_dir: Path = decisions_dir or _DEFAULT_DECISIONS_DIR
        self._secrets = KeyVaultSecretResolver(self._cfg)
        self._cosmos_key = self._secrets.resolve(
            direct_value=self._cfg.cosmos_key,
            secret_name=getattr(self._cfg, "cosmos_key_secret_name", ""),
            setting_name="COSMOS_KEY",
        )

        self._is_mock: bool = (
            self._cfg.use_local_mocks
            or not self._cfg.cosmos_endpoint
            or not self._cosmos_key
        )

        if self._is_mock:
            if not self._cfg.use_local_mocks and self._cfg.cosmos_endpoint:
                logger.warning(
                    "CosmosDecisionClient: no key available from env or Key Vault; "
                    "falling back to mock mode."
                )
            logger.info("CosmosDecisionClient: LOCAL MOCK mode (JSON files at %s).", self._decisions_dir)
            self._decisions_dir.mkdir(parents=True, exist_ok=True)
            self._container = None
        else:
            from azure.cosmos import CosmosClient  # type: ignore[import]

            client = CosmosClient(
                url=self._cfg.cosmos_endpoint,
                credential=self._cosmos_key,
            )
            db = client.get_database_client(self._cfg.cosmos_database)
            self._container = db.get_container_client(
                self._cfg.cosmos_container_decisions
            )
            logger.info(
                "CosmosDecisionClient: connected to %s / %s",
                self._cfg.cosmos_database,
                self._cfg.cosmos_container_decisions,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert(self, record: dict) -> None:
        """Insert or update a governance decision record.

        The record must contain an ``"id"`` field.  When the same ``id``
        is upserted twice, the second write overwrites the first (idempotent).

        Args:
            record: Dict representing the decision.  Must have at minimum:
                ``id`` (str) and ``resource_id`` (str, used as partition key).
        """
        if self._is_mock:
            path = self._decisions_dir / f"{record['id']}.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
            logger.debug("CosmosDecisionClient(mock): wrote %s", path.name)
        else:
            self._container.upsert_item(record)
            logger.debug("CosmosDecisionClient: upserted %s", record.get("id"))

    def get_recent(self, limit: int = 10) -> list[dict]:
        """Return the most recent decisions, newest first.

        Args:
            limit: Maximum number of records to return (default 10).

        Returns:
            List of decision dicts ordered by ``timestamp`` descending.
        """
        if self._is_mock:
            return self._mock_get_recent(limit)

        query = f"SELECT TOP {limit} * FROM c ORDER BY c._ts DESC"
        return list(
            self._container.query_items(query, enable_cross_partition_query=True)
        )

    def get_by_resource(self, resource_id: str, limit: int = 10) -> list[dict]:
        """Return decisions for a specific resource, newest first.

        Matches any record where ``resource_id`` contains the given string
        as a substring (handles both short names and full Azure IDs).

        Args:
            resource_id: Full or partial Azure resource ID / short name.
            limit: Maximum records to return.

        Returns:
            Filtered list of decision dicts, newest first.
        """
        if self._is_mock:
            return self._mock_get_by_resource(resource_id, limit)

        query = (
            f"SELECT TOP {limit} * FROM c "
            "WHERE CONTAINS(c.resource_id, @rid) "
            "ORDER BY c._ts DESC"
        )
        params = [{"name": "@rid", "value": resource_id}]
        return list(
            self._container.query_items(
                query, parameters=params, enable_cross_partition_query=True
            )
        )

    @property
    def is_mock(self) -> bool:
        """True if this client is running in local mock mode."""
        return self._is_mock

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    def _mock_get_recent(self, limit: int) -> list[dict]:
        records = self._load_local_all()
        records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return records[:limit]

    def _mock_get_by_resource(self, resource_id: str, limit: int) -> list[dict]:
        all_records = self._load_local_all()
        matched = [r for r in all_records if resource_id in r.get("resource_id", "")]
        matched.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return matched[:limit]

    def _load_local_all(self) -> list[dict]:
        """Load every JSON file from the local decisions directory."""
        records: list[dict] = []
        for path in self._decisions_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    records.append(json.load(fh))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "CosmosDecisionClient(mock): skipping %s (%s)", path.name, exc
                )
        return records
