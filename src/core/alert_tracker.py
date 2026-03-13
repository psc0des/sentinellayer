"""Durable tracker for Azure Monitor alert investigations.

Stores alert lifecycle records so dashboard alert history survives
browser refresh and API process restarts.

Storage mode:
- Live mode: Azure Cosmos DB container (default: ``governance-alerts``)
- Mock mode: local JSON files under ``data/alerts/``

Alert record schema::

    {
        "id":              "<uuid>",
        "alert_id":        "<uuid>",
        "status":          "firing" | "investigating" | "resolved" | "error",
        "resource_id":     "/subscriptions/.../vm1",
        "resource_name":   "vm1",
        "metric":          "Percentage CPU",
        "value":           95.0,
        "threshold":       80.0,
        "severity":        "3",
        "resource_group":  "ruriskry-prod-rg",
        "fired_at":        "<iso>",
        "received_at":     "<iso>",
        "investigating_at":"<iso>",
        "resolved_at":     "<iso>",
        "proposals_count": 1,
        "proposals":       [...],
        "verdicts":        [...],
        "totals":          {"approved": 0, "escalated": 0, "denied": 0},
        "error":           null
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.config import settings as _default_settings
from src.infrastructure.secrets import KeyVaultSecretResolver

logger = logging.getLogger(__name__)

_DEFAULT_ALERTS_DIR = Path(__file__).parent.parent.parent / "data" / "alerts"


class AlertTracker:
    """Read/write alert investigation records in Cosmos DB or local JSON files."""

    def __init__(self, cfg=None, alerts_dir: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._alerts_dir: Path = alerts_dir or _DEFAULT_ALERTS_DIR
        self._secrets = KeyVaultSecretResolver(self._cfg)
        self._cosmos_key = self._secrets.resolve(
            direct_value=self._cfg.cosmos_key,
            secret_name=getattr(self._cfg, "cosmos_key_secret_name", ""),
            setting_name="COSMOS_KEY",
        )

        self._container_name = getattr(
            self._cfg, "cosmos_container_alerts", "governance-alerts"
        )
        self._is_mock: bool = (
            self._cfg.use_local_mocks
            or not self._cfg.cosmos_endpoint
            or not self._cosmos_key
        )

        if self._is_mock:
            self._alerts_dir.mkdir(parents=True, exist_ok=True)
            self._container = None
            if not self._cfg.use_local_mocks and self._cfg.cosmos_endpoint:
                logger.warning(
                    "AlertTracker: no key available from env or Key Vault; "
                    "falling back to mock mode."
                )
            logger.info("AlertTracker: LOCAL MOCK mode (%s).", self._alerts_dir)
        else:
            from azure.cosmos import CosmosClient, PartitionKey  # type: ignore[import]

            try:
                client = CosmosClient(
                    url=self._cfg.cosmos_endpoint,
                    credential=self._cosmos_key,
                )
                db = client.get_database_client(self._cfg.cosmos_database)
                self._container = db.create_container_if_not_exists(
                    id=self._container_name,
                    partition_key=PartitionKey(path="/severity"),
                )
                logger.info(
                    "AlertTracker: connected to %s / %s",
                    self._cfg.cosmos_database,
                    self._container_name,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AlertTracker: failed to initialise Cosmos container '%s' "
                    "(%s). Falling back to local JSON alert storage.",
                    self._container_name,
                    exc,
                )
                self._is_mock = True
                self._container = None
                self._alerts_dir.mkdir(parents=True, exist_ok=True)

    def upsert(self, record: dict[str, Any]) -> None:
        """Insert or update one alert record."""
        record = dict(record)
        record.setdefault("id", record.get("alert_id"))
        record.setdefault("alert_id", record.get("id"))
        if not record.get("id"):
            raise ValueError("AlertTracker.upsert requires 'id' or 'alert_id'.")

        if self._is_mock:
            path = self._alerts_dir / f"{record['id']}.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
        else:
            self._container.upsert_item(record)

    def get(self, alert_id: str) -> dict[str, Any] | None:
        """Return one alert record by alert_id, or None if not found."""
        if self._is_mock:
            path = self._alerts_dir / f"{alert_id}.json"
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
                parameters=[{"name": "@id", "value": alert_id}],
                enable_cross_partition_query=True,
            )
        )
        return items[0] if items else None

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to *limit* alert records, newest-first."""
        if self._is_mock:
            records = self._load_local_all()
            records.sort(key=lambda r: r.get("received_at", ""), reverse=True)
            return records[:limit]

        query = f"SELECT TOP {limit} * FROM c ORDER BY c.received_at DESC"
        return list(
            self._container.query_items(
                query=query,
                enable_cross_partition_query=True,
            )
        )

    def count_active(self) -> int:
        """Count alerts in firing or investigating status."""
        if self._is_mock:
            records = self._load_local_all()
            return sum(
                1 for r in records if r.get("status") in ("firing", "investigating")
            )

        query = (
            "SELECT VALUE COUNT(1) FROM c "
            "WHERE c.status = 'firing' OR c.status = 'investigating'"
        )
        items = list(
            self._container.query_items(
                query=query,
                enable_cross_partition_query=True,
            )
        )
        return items[0] if items else 0

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def _load_local_all(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self._alerts_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    records.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                logger.warning("AlertTracker(mock): skipping invalid file %s", path)
        return records
