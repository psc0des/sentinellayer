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
    container in the ``ruriskry`` Cosmos DB database.

Usage::

    from src.infrastructure.cosmos_client import CosmosDecisionClient

    client = CosmosDecisionClient()
    client.upsert({"id": "abc-123", "resource_id": "vm-23", "decision": "denied"})
    recent = client.get_recent(limit=5)

    from src.infrastructure.cosmos_client import CosmosInventoryClient

    inv = CosmosInventoryClient()
    inv.upsert({"id": "inv-abc123-20260408", "subscription_id": "abc123", ...})
    latest = inv.get_latest("abc123-...")
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

    def get_recent(self, limit: int = 10, offset: int = 0) -> list[dict]:
        """Return the most recent decisions, newest first.

        Args:
            limit: Maximum number of records to return (default 10).
            offset: Number of records to skip for pagination (default 0).

        Returns:
            List of decision dicts ordered by ``timestamp`` descending.
        """
        if self._is_mock:
            return self._mock_get_recent(limit, offset)

        query = f"SELECT * FROM c ORDER BY c._ts DESC OFFSET {offset} LIMIT {limit}"
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

    def _mock_get_recent(self, limit: int, offset: int = 0) -> list[dict]:
        records = self._load_local_all()
        records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return records[offset:offset + limit]

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


_DEFAULT_EXECUTIONS_DIR = (
    Path(__file__).parent.parent.parent / "data" / "executions"
)


class CosmosExecutionClient:
    """Read/write execution gateway records from Cosmos DB or local JSON files.

    Mirrors the CosmosDecisionClient pattern — local JSON in mock mode,
    Cosmos DB ``governance-executions`` container in live mode.

    Partition key: ``/resource_id``
    Document ID:   ``execution_id``
    """

    def __init__(self, cfg=None, executions_dir: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._executions_dir: Path = executions_dir or _DEFAULT_EXECUTIONS_DIR
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
            logger.info(
                "CosmosExecutionClient: LOCAL MOCK mode (JSON files at %s).",
                self._executions_dir,
            )
            self._executions_dir.mkdir(parents=True, exist_ok=True)
            self._container = None
        else:
            from azure.cosmos import CosmosClient  # type: ignore[import]

            client = CosmosClient(
                url=self._cfg.cosmos_endpoint,
                credential=self._cosmos_key,
            )
            db = client.get_database_client(self._cfg.cosmos_database)
            self._container = db.get_container_client(
                self._cfg.cosmos_container_executions
            )
            logger.info(
                "CosmosExecutionClient: connected to %s / %s",
                self._cfg.cosmos_database,
                self._cfg.cosmos_container_executions,
            )

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def upsert(self, record: dict) -> None:
        """Insert or update one execution record.

        The record must have ``execution_id`` (used as ``id``) and
        ``resource_id`` (used as partition key).
        """
        doc = {**record, "id": record["execution_id"]}
        if self._is_mock:
            path = self._executions_dir / f"{record['execution_id']}.json"
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            logger.debug("CosmosExecutionClient(mock): wrote %s", path.name)
        else:
            self._container.upsert_item(doc)
            logger.debug("CosmosExecutionClient: upserted %s", record["execution_id"])

    def get_all(self) -> list[dict]:
        """Return all execution records (used to warm the in-memory index on startup)."""
        if self._is_mock:
            records: list[dict] = []
            for path in self._executions_dir.glob("*.json"):
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "CosmosExecutionClient(mock): skipping %s (%s)", path.name, exc
                    )
            return records

        query = "SELECT * FROM c ORDER BY c._ts DESC"
        return list(
            self._container.query_items(query, enable_cross_partition_query=True)
        )

    def delete(self, execution_id: str, resource_id: str) -> None:
        """Delete a single execution record by ID + partition key."""
        if self._is_mock:
            path = self._executions_dir / f"{execution_id}.json"
            path.unlink(missing_ok=True)
        else:
            try:
                self._container.delete_item(item=execution_id, partition_key=resource_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "CosmosExecutionClient: delete %s failed — %s", execution_id, exc
                )


_DEFAULT_AZ_EXECUTIONS_DIR = (
    Path(__file__).parent.parent.parent / "data" / "az_executions"
)


class CosmosAzExecutionClient:
    """Read/write Phase 34E az CLI execution audit records.

    Uses a separate mock directory (``data/az_executions/``) to avoid
    polluting ``ExecutionGateway._ensure_loaded()`` which reads from
    ``data/executions/``.  In live mode, writes to the same
    ``governance-executions`` Cosmos container tagged with
    ``record_type="az_execution"`` so the gateway skips them.

    Partition key: ``/resource_id``
    Document ID:   ``execution_id``
    """

    def __init__(self, cfg=None, az_executions_dir: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._dir: Path = az_executions_dir or _DEFAULT_AZ_EXECUTIONS_DIR
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
            self._dir.mkdir(parents=True, exist_ok=True)
            self._container = None
        else:
            from azure.cosmos import CosmosClient  # type: ignore[import]

            client = CosmosClient(
                url=self._cfg.cosmos_endpoint,
                credential=self._cosmos_key,
            )
            db = client.get_database_client(self._cfg.cosmos_database)
            self._container = db.get_container_client(
                self._cfg.cosmos_container_executions
            )

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def upsert(self, record: dict) -> None:
        """Write one az execution audit record."""
        doc = {**record, "id": record["execution_id"]}
        if self._is_mock:
            path = self._dir / f"{record['execution_id']}.json"
            path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        else:
            self._container.upsert_item(doc)

    def get_by_decision(self, decision_id: str) -> list[dict]:
        """Return all az execution records for a decision (newest-first)."""
        if self._is_mock:
            results: list[dict] = []
            for path in self._dir.glob("*.json"):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                    if rec.get("decision_id") == decision_id:
                        results.append(rec)
                except Exception:  # noqa: BLE001
                    pass
            return sorted(results, key=lambda r: r.get("created_at", ""), reverse=True)

        query = "SELECT * FROM c WHERE c.decision_id = @did ORDER BY c._ts DESC"
        return list(
            self._container.query_items(
                query,
                parameters=[{"name": "@did", "value": decision_id}],
                enable_cross_partition_query=True,
            )
        )


_DEFAULT_INVENTORY_DIR = (
    Path(__file__).parent.parent.parent / "data" / "inventory"
)


class CosmosInventoryClient:
    """Read/write resource inventory snapshots from Cosmos DB or local JSON files.

    Mirrors the CosmosExecutionClient pattern — local JSON in mock mode,
    Cosmos DB ``resource-inventory`` container in live mode.

    Partition key: ``/subscription_id``
    Document ID:   ``inv-<sub_short>-<timestamp>``
    """

    def __init__(self, cfg=None, inventory_dir: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._inventory_dir: Path = inventory_dir or _DEFAULT_INVENTORY_DIR
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
            logger.info(
                "CosmosInventoryClient: LOCAL MOCK mode (JSON files at %s).",
                self._inventory_dir,
            )
            self._inventory_dir.mkdir(parents=True, exist_ok=True)
            self._container = None
        else:
            from azure.cosmos import CosmosClient  # type: ignore[import]

            client = CosmosClient(
                url=self._cfg.cosmos_endpoint,
                credential=self._cosmos_key,
            )
            db = client.get_database_client(self._cfg.cosmos_database)
            self._container = db.get_container_client(
                self._cfg.cosmos_container_inventory
            )
            logger.info(
                "CosmosInventoryClient: connected to %s / %s",
                self._cfg.cosmos_database,
                self._cfg.cosmos_container_inventory,
            )

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def upsert(self, record: dict) -> None:
        """Insert or update one inventory snapshot.

        The record must have ``id`` and ``subscription_id`` fields.
        """
        if self._is_mock:
            # In mock mode write a single ``latest.json`` so get_latest() finds it.
            path = self._inventory_dir / "latest.json"
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            logger.debug("CosmosInventoryClient(mock): wrote %s", path.name)
        else:
            self._container.upsert_item(record)
            logger.debug("CosmosInventoryClient: upserted %s", record.get("id"))

    def get_latest(self, subscription_id: str) -> dict | None:
        """Return the most recent inventory snapshot for a subscription.

        Args:
            subscription_id: Azure subscription ID (full UUID).

        Returns:
            Inventory document dict, or ``None`` if no snapshot exists.
        """
        if self._is_mock:
            path = self._inventory_dir / "latest.json"
            if not path.exists():
                return None
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                # Filter by subscription if the file stores one
                if subscription_id and record.get("subscription_id"):
                    if not record["subscription_id"].startswith(subscription_id[:8]):
                        return None
                return record
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("CosmosInventoryClient(mock): could not read %s — %s", path, exc)
                return None

        query = (
            "SELECT TOP 1 * FROM c "
            "WHERE c.subscription_id = @sub "
            "ORDER BY c.refreshed_at DESC"
        )
        params = [{"name": "@sub", "value": subscription_id}]
        results = list(
            self._container.query_items(
                query, parameters=params, partition_key=subscription_id
            )
        )
        return results[0] if results else None

    def delete_old(self, subscription_id: str, keep: int = 5) -> None:
        """Delete old inventory snapshots, retaining only the N most recent.

        Args:
            subscription_id: Azure subscription ID.
            keep: Number of most-recent snapshots to retain.
        """
        if self._is_mock:
            return  # mock mode keeps one file — nothing to prune

        query = (
            "SELECT c.id FROM c "
            "WHERE c.subscription_id = @sub "
            "ORDER BY c.refreshed_at DESC "
            f"OFFSET {keep} LIMIT 1000"
        )
        params = [{"name": "@sub", "value": subscription_id}]
        old_ids = [
            r["id"] for r in self._container.query_items(
                query, parameters=params, partition_key=subscription_id
            )
        ]
        for doc_id in old_ids:
            try:
                self._container.delete_item(item=doc_id, partition_key=subscription_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "CosmosInventoryClient: delete %s failed — %s", doc_id, exc
                )


class CosmosAdminClient:
    """Persist the admin auth record in Cosmos DB (governance-agents container).

    Uses a single document with id="_admin_auth" and name="_system" (partition key).
    Falls back silently to no-op in mock mode or when Cosmos is unavailable —
    callers always check the local file first; Cosmos is the durable backup.

    Partition key: /name   (existing governance-agents container)
    Document ID:   _admin_auth
    """

    _ADMIN_ID = "_admin_auth"
    _ADMIN_PARTITION = "_system"

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg or _default_settings
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
        self._container = None
        if not self._is_mock:
            try:
                from azure.cosmos import CosmosClient  # type: ignore[import]

                client = CosmosClient(
                    url=self._cfg.cosmos_endpoint,
                    credential=self._cosmos_key,
                )
                db = client.get_database_client(self._cfg.cosmos_database)
                self._container = db.get_container_client(
                    self._cfg.cosmos_container_agents
                )
                logger.info("CosmosAdminClient: connected to %s / %s",
                            self._cfg.cosmos_database, self._cfg.cosmos_container_agents)
            except Exception as exc:  # noqa: BLE001
                logger.warning("CosmosAdminClient: init failed — %s; no Cosmos backup for admin auth", exc)
                self._container = None

    def load(self) -> dict | None:
        """Return the stored admin record, or None if not found."""
        if self._container is None:
            return None
        try:
            doc = self._container.read_item(
                item=self._ADMIN_ID, partition_key=self._ADMIN_PARTITION
            )
            # Strip Cosmos metadata before returning
            return {k: v for k, v in doc.items() if not k.startswith("_") and k != "name"}
        except Exception:  # noqa: BLE001
            return None

    def save(self, record: dict) -> None:
        """Upsert the admin record to Cosmos."""
        if self._container is None:
            return
        try:
            self._container.upsert_item({
                "id": self._ADMIN_ID,
                "name": self._ADMIN_PARTITION,
                **record,
            })
            logger.info("CosmosAdminClient: admin auth saved to Cosmos")
        except Exception as exc:  # noqa: BLE001
            logger.warning("CosmosAdminClient: save failed — %s", exc)

    def delete(self) -> None:
        """Delete the admin record from Cosmos (used by --reset-admin)."""
        if self._container is None:
            return
        try:
            self._container.delete_item(
                item=self._ADMIN_ID, partition_key=self._ADMIN_PARTITION
            )
            logger.info("CosmosAdminClient: admin auth deleted from Cosmos")
        except Exception as exc:  # noqa: BLE001
            logger.warning("CosmosAdminClient: delete failed — %s", exc)
