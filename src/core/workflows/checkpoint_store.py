"""Cosmos-backed checkpoint storage for governance workflow runs (Phase 33C).

Mode selection
--------------
Mock mode (USE_LOCAL_MOCKS=true, or Cosmos not configured):
    Uses ``InMemoryCheckpointStorage`` — checkpoints are process-local and
    lost on restart.  Fine for local dev and CI.

Azure mode (USE_LOCAL_MOCKS=false + COSMOS_ENDPOINT set):
    Stores checkpoints in the ``governance-checkpoints`` Cosmos container.
    Each document has a 7-day TTL so completed checkpoints expire automatically.

Usage
-----
    store = CosmosCheckpointStore(scan_id="abc", action_key="vm-23:restart_service")
    # Pass to workflow.run():
    result = await workflow.run(inp, checkpoint_storage=store)
    # Later, on resume:
    result = await workflow.run(checkpoint_id=store.last_checkpoint_id, checkpoint_storage=store)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent_framework import InMemoryCheckpointStorage, WorkflowCheckpointException
from agent_framework._workflows._checkpoint import (
    CheckpointID,
    CheckpointStorage,
    WorkflowCheckpoint,
)

from src.config import settings as _default_settings

logger = logging.getLogger(__name__)

# 7-day TTL — completed checkpoints expire automatically; manual cleanup on scan done.
_CHECKPOINT_TTL_SECONDS = 7 * 24 * 3600


class CosmosCheckpointStore:
    """Checkpoint storage backed by Cosmos DB (or in-memory in mock mode).

    Each instance is bound to a specific (scan_id, action_key) pair.  Creating
    a fresh instance per proposal run isolates checkpoints and makes ``get_latest``
    deterministic for that run.

    Args:
        scan_id:    The enclosing scan UUID.
        action_key: Stable identifier for the proposal (``resource_id:action_type``).
        cfg:        Settings override (defaults to module singleton).
    """

    def __init__(
        self,
        scan_id: str,
        action_key: str,
        cfg=None,
    ) -> None:
        self._cfg = cfg or _default_settings
        self._scan_id = scan_id
        self._action_key = action_key
        self.last_checkpoint_id: str | None = None  # updated on every save()

        self._is_mock: bool = (
            self._cfg.use_local_mocks
            or not self._cfg.cosmos_endpoint
        )

        if self._is_mock:
            logger.debug(
                "CosmosCheckpointStore: mock mode (scan=%s action=%s)",
                scan_id[:8], action_key,
            )
            self._memory = InMemoryCheckpointStorage()
            self._container = None
        else:
            from azure.cosmos import CosmosClient  # type: ignore[import]

            from src.infrastructure.secrets import KeyVaultSecretResolver  # noqa: PLC0415

            resolver = KeyVaultSecretResolver(self._cfg)
            cosmos_key = resolver.resolve(
                direct_value=self._cfg.cosmos_key,
                secret_name=getattr(self._cfg, "cosmos_key_secret_name", ""),
                setting_name="COSMOS_KEY",
            )
            if not cosmos_key:
                logger.warning(
                    "CosmosCheckpointStore: no Cosmos key — falling back to in-memory."
                )
                self._memory = InMemoryCheckpointStorage()
                self._container = None
                self._is_mock = True
                return

            client = CosmosClient(self._cfg.cosmos_endpoint, credential=cosmos_key)
            db = client.get_database_client(self._cfg.cosmos_database)
            self._container = db.get_container_client(
                self._cfg.cosmos_container_checkpoints
            )
            self._memory = None  # type: ignore[assignment]
            logger.debug(
                "CosmosCheckpointStore: Cosmos mode (scan=%s action=%s container=%s)",
                scan_id[:8], action_key, self._cfg.cosmos_container_checkpoints,
            )

    # ------------------------------------------------------------------
    # CheckpointStorage protocol
    # ------------------------------------------------------------------

    async def save(self, checkpoint: WorkflowCheckpoint) -> CheckpointID:
        """Encode and persist a checkpoint; update ``last_checkpoint_id``."""
        self.last_checkpoint_id = checkpoint.checkpoint_id

        if self._is_mock:
            return await self._memory.save(checkpoint)

        import asyncio  # noqa: PLC0415

        from agent_framework._workflows._checkpoint_encoding import (  # noqa: PLC0415
            encode_checkpoint_value,
        )

        encoded: dict[str, Any] = encode_checkpoint_value(checkpoint.to_dict())
        doc = {
            "id": checkpoint.checkpoint_id,
            "workflow_name": checkpoint.workflow_name,
            "scan_id": self._scan_id,
            "action_key": self._action_key,
            "payload": encoded,
            "created_at": checkpoint.timestamp,
            "_ttl": _CHECKPOINT_TTL_SECONDS,
        }

        await asyncio.to_thread(self._container.upsert_item, doc)
        logger.debug("Checkpoint saved: %s", checkpoint.checkpoint_id)
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: CheckpointID) -> WorkflowCheckpoint:
        """Load and decode a checkpoint by ID."""
        if self._is_mock:
            return await self._memory.load(checkpoint_id)

        import asyncio  # noqa: PLC0415

        from agent_framework._workflows._checkpoint_encoding import (  # noqa: PLC0415
            decode_checkpoint_value,
        )

        try:
            doc = await asyncio.to_thread(
                self._container.read_item, item=checkpoint_id, partition_key=checkpoint_id
            )
        except Exception as exc:
            raise WorkflowCheckpointException(
                f"Checkpoint '{checkpoint_id}' not found: {exc}"
            ) from exc

        decoded: dict[str, Any] = decode_checkpoint_value(doc["payload"])
        return WorkflowCheckpoint.from_dict(decoded)

    async def list_checkpoints(
        self, *, workflow_name: str
    ) -> list[WorkflowCheckpoint]:
        """List all checkpoints for this (scan_id, action_key) pair."""
        if self._is_mock:
            return await self._memory.list_checkpoints(workflow_name=workflow_name)

        import asyncio  # noqa: PLC0415

        from agent_framework._workflows._checkpoint_encoding import (  # noqa: PLC0415
            decode_checkpoint_value,
        )

        query = (
            "SELECT * FROM c WHERE c.scan_id = @sid AND c.action_key = @ak"
        )
        params = [
            {"name": "@sid", "value": self._scan_id},
            {"name": "@ak", "value": self._action_key},
        ]

        def _query() -> list[WorkflowCheckpoint]:
            checkpoints: list[WorkflowCheckpoint] = []
            for doc in self._container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True,
            ):
                try:
                    decoded = decode_checkpoint_value(doc["payload"])
                    checkpoints.append(WorkflowCheckpoint.from_dict(decoded))
                except Exception as exc:
                    logger.warning("Failed to decode checkpoint %s: %s", doc.get("id"), exc)
            return checkpoints

        return await asyncio.to_thread(_query)

    async def delete(self, checkpoint_id: CheckpointID) -> bool:
        """Delete a checkpoint by ID."""
        if self._is_mock:
            return await self._memory.delete(checkpoint_id)

        import asyncio  # noqa: PLC0415

        try:
            await asyncio.to_thread(
                self._container.delete_item,
                item=checkpoint_id,
                partition_key=checkpoint_id,
            )
            return True
        except Exception:
            return False

    async def get_latest(
        self, *, workflow_name: str
    ) -> WorkflowCheckpoint | None:
        """Return the most recently saved checkpoint for this run."""
        if self._is_mock:
            return await self._memory.get_latest(workflow_name=workflow_name)

        checkpoints = await self.list_checkpoints(workflow_name=workflow_name)
        if not checkpoints:
            return None
        return max(checkpoints, key=lambda cp: cp.timestamp)

    async def list_checkpoint_ids(
        self, *, workflow_name: str
    ) -> list[CheckpointID]:
        checkpoints = await self.list_checkpoints(workflow_name=workflow_name)
        return [cp.checkpoint_id for cp in checkpoints]

    # ------------------------------------------------------------------
    # Scan-level helpers (not part of CheckpointStorage protocol)
    # ------------------------------------------------------------------

    async def delete_all_for_scan(self) -> int:
        """Delete all checkpoints belonging to this scan_id.  Returns deletion count."""
        if self._is_mock:
            return 0  # InMemory — GC handles it

        import asyncio  # noqa: PLC0415

        query = "SELECT c.id FROM c WHERE c.scan_id = @sid"
        params = [{"name": "@sid", "value": self._scan_id}]

        def _delete_all() -> int:
            count = 0
            for doc in self._container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True,
            ):
                try:
                    self._container.delete_item(
                        item=doc["id"], partition_key=doc["id"]
                    )
                    count += 1
                except Exception as exc:
                    logger.warning("Failed to delete checkpoint %s: %s", doc.get("id"), exc)
            return count

        count = await asyncio.to_thread(_delete_all)
        logger.debug("Deleted %d checkpoint(s) for scan %s", count, self._scan_id[:8])
        return count
