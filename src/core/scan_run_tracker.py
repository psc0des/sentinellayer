"""Durable tracker for operational scan runs.

Stores scan lifecycle records for dashboard scan endpoints so status/history
survive browser refresh and API process restarts.

Storage mode:
- Live mode: Azure Cosmos DB container (default: ``governance-scan-runs``)
- Mock mode: local JSON files under ``data/scans/``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.config import settings as _default_settings
from src.infrastructure.secrets import KeyVaultSecretResolver

logger = logging.getLogger(__name__)

_DEFAULT_SCANS_DIR = Path(__file__).parent.parent.parent / "data" / "scans"


class ScanRunTracker:
    """Read/write scan-run records in Cosmos DB or local JSON files."""

    def __init__(self, cfg=None, scans_dir: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._scans_dir: Path = scans_dir or _DEFAULT_SCANS_DIR
        self._secrets = KeyVaultSecretResolver(self._cfg)
        self._cosmos_key = self._secrets.resolve(
            direct_value=self._cfg.cosmos_key,
            secret_name=getattr(self._cfg, "cosmos_key_secret_name", ""),
            setting_name="COSMOS_KEY",
        )

        self._container_name = getattr(
            self._cfg, "cosmos_container_scan_runs", "governance-scan-runs"
        )
        self._is_mock: bool = (
            self._cfg.use_local_mocks
            or not self._cfg.cosmos_endpoint
            or not self._cosmos_key
        )

        if self._is_mock:
            self._scans_dir.mkdir(parents=True, exist_ok=True)
            self._container = None
            if not self._cfg.use_local_mocks and self._cfg.cosmos_endpoint:
                logger.warning(
                    "ScanRunTracker: no key available from env or Key Vault; "
                    "falling back to mock mode."
                )
            logger.info("ScanRunTracker: LOCAL MOCK mode (%s).", self._scans_dir)
        else:
            from azure.cosmos import CosmosClient, PartitionKey  # type: ignore[import]

            try:
                client = CosmosClient(
                    url=self._cfg.cosmos_endpoint,
                    credential=self._cosmos_key,
                )
                db = client.get_database_client(self._cfg.cosmos_database)
                # Ensure scan-runs container exists in live mode.
                self._container = db.create_container_if_not_exists(
                    id=self._container_name,
                    partition_key=PartitionKey(path="/agent_type"),
                )
                logger.info(
                    "ScanRunTracker: connected to %s / %s",
                    self._cfg.cosmos_database,
                    self._container_name,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ScanRunTracker: failed to initialise Cosmos container '%s' "
                    "(%s). Falling back to local JSON scan storage.",
                    self._container_name,
                    exc,
                )
                self._is_mock = True
                self._container = None
                self._scans_dir.mkdir(parents=True, exist_ok=True)

    def upsert(self, record: dict[str, Any]) -> None:
        """Insert or update one scan-run record."""
        record = dict(record)
        record.setdefault("id", record.get("scan_id"))
        record.setdefault("scan_id", record.get("id"))
        if not record.get("id"):
            raise ValueError("ScanRunTracker.upsert requires 'id' or 'scan_id'.")

        if self._is_mock:
            path = self._scans_dir / f"{record['id']}.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
        else:
            self._container.upsert_item(record)

    def get(self, scan_id: str) -> dict[str, Any] | None:
        """Return one scan-run record by scan_id, or None if not found."""
        if self._is_mock:
            path = self._scans_dir / f"{scan_id}.json"
            if not path.exists():
                return None
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                return None

        query = "SELECT TOP 1 * FROM c WHERE c.id = @id"
        items = list(
            self._container.query_items(
                query=query,
                parameters=[{"name": "@id", "value": scan_id}],
                enable_cross_partition_query=True,
            )
        )
        return items[0] if items else None

    def get_latest_completed_by_agent_type(
        self, agent_type: str
    ) -> dict[str, Any] | None:
        """Return the latest completed scan for one agent type."""
        if self._is_mock:
            records = self._load_local_all()
            matches = [
                r
                for r in records
                if r.get("agent_type") == agent_type
                and r.get("status") in ("complete", "error")
            ]
            if not matches:
                return None
            return max(matches, key=lambda r: r.get("started_at", ""))

        query = (
            "SELECT TOP 1 * FROM c "
            "WHERE c.agent_type = @agent_type AND (c.status = 'complete' OR c.status = 'error') "
            "ORDER BY c.started_at DESC"
        )
        items = list(
            self._container.query_items(
                query=query,
                parameters=[{"name": "@agent_type", "value": agent_type}],
                enable_cross_partition_query=True,
            )
        )
        return items[0] if items else None

    def record_event(self, scan_id: str, timestamp: str) -> None:
        """Increment event_count and update last_event_at for a scan."""
        record = self.get(scan_id)
        if not record:
            return
        record["event_count"] = int(record.get("event_count", 0)) + 1
        record["last_event_at"] = timestamp
        self.upsert(record)

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to *limit* scan-run records, newest-first."""
        if self._is_mock:
            records = self._load_local_all()
            records.sort(key=lambda r: r.get("started_at", ""), reverse=True)
            return records[:limit]

        query = (
            f"SELECT TOP {limit} * FROM c ORDER BY c.started_at DESC"
        )
        return list(
            self._container.query_items(
                query=query,
                enable_cross_partition_query=True,
            )
        )

    def _load_local_all(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self._scans_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    records.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                logger.warning("ScanRunTracker(mock): skipping invalid file %s", path)
        return records
