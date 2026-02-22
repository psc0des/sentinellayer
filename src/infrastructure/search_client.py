"""Azure AI Search client — incident history search.

Mode selection
--------------
Mock mode (USE_LOCAL_MOCKS=true, or endpoint not set):
    Loads incidents from ``data/seed_incidents.json`` and performs simple
    keyword + field-filter matching in Python.  No network call needed.
    This mirrors the logic inside ``HistoricalPatternAgent`` so the two
    modes produce comparable (not identical) results.

Azure mode (USE_LOCAL_MOCKS=false + AZURE_SEARCH_ENDPOINT set):
    Uses the ``azure-search-documents`` SDK to perform full-text search
    (and optionally vector/semantic search) against the ``incident-history``
    index deployed in Azure AI Search.

Usage::

    from src.infrastructure.search_client import AzureSearchClient

    client = AzureSearchClient()
    hits = client.search_incidents(
        query="delete disaster recovery vm",
        action_type="delete_resource",
        top=3,
    )
    for hit in hits:
        print(hit["incident_id"], hit["severity"])
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.infrastructure.secrets import KeyVaultSecretResolver

logger = logging.getLogger(__name__)

_DEFAULT_INCIDENTS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_incidents.json"
)


class AzureSearchClient:
    """Search incident history via Azure AI Search or local JSON fallback.

    Args:
        cfg: Settings object (defaults to module singleton from ``src.config``).
        incidents_path: Override the local JSON path (used in tests).
    """

    def __init__(self, cfg=None, incidents_path: Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._incidents_path: Path = incidents_path or _DEFAULT_INCIDENTS_PATH
        self._secrets = KeyVaultSecretResolver(self._cfg)
        self._api_key = self._secrets.resolve(
            direct_value=self._cfg.azure_search_api_key,
            secret_name=getattr(self._cfg, "azure_search_api_key_secret_name", ""),
            setting_name="AZURE_SEARCH_API_KEY",
        )

        self._is_mock: bool = (
            self._cfg.use_local_mocks
            or not self._cfg.azure_search_endpoint
            or not self._api_key
        )

        if self._is_mock:
            if not self._cfg.use_local_mocks and self._cfg.azure_search_endpoint:
                logger.warning(
                    "AzureSearchClient: no API key available from env or Key Vault; "
                    "falling back to mock mode."
                )
            logger.info("AzureSearchClient: LOCAL MOCK mode (JSON at %s).", self._incidents_path)
            self._incidents: list[dict] = self._load_local_incidents()
            self._search_client = None
        else:
            from azure.core.credentials import AzureKeyCredential  # type: ignore[import]
            from azure.search.documents import SearchClient  # type: ignore[import]

            self._incidents = []
            self._search_client = SearchClient(
                endpoint=self._cfg.azure_search_endpoint,
                index_name=self._cfg.azure_search_index,
                credential=AzureKeyCredential(self._api_key),
            )
            logger.info(
                "AzureSearchClient: connected to %s index '%s'",
                self._cfg.azure_search_endpoint,
                self._cfg.azure_search_index,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_incidents(
        self,
        query: str,
        action_type: str | None = None,
        resource_type: str | None = None,
        top: int = 5,
    ) -> list[dict]:
        """Search for incidents similar to the given query.

        Args:
            query: Free-text search terms (action description, keywords).
            action_type: Optional filter — only incidents whose
                ``action_taken`` field contains this string are returned
                (e.g. ``"delete_resource"``).
            resource_type: Optional filter — only incidents of this Azure
                resource type (e.g. ``"Microsoft.Compute/virtualMachines"``).
            top: Maximum number of results to return (default 5).

        Returns:
            List of incident dicts ordered by relevance, at most ``top`` items.
            Each dict includes: ``incident_id``, ``description``,
            ``action_taken``, ``outcome``, ``lesson``, ``service``,
            ``severity``, ``date``, ``resource_type``.
        """
        if self._is_mock:
            return self._mock_search(query, action_type, resource_type, top)

        # Build OData filter string for field-level filters
        filters: list[str] = []
        if resource_type:
            filters.append(f"resource_type eq '{resource_type}'")
        filter_str = " and ".join(filters) if filters else None

        results = self._search_client.search(
            search_text=query,
            filter=filter_str,
            top=top,
            select=[
                "incident_id", "description", "action_taken", "outcome",
                "lesson", "service", "severity", "date", "resource_type",
            ],
        )
        hits = [dict(r) for r in results]

        # Post-filter by action_type (Azure Search OData doesn't support
        # substring matching on this field without a custom analyzer)
        if action_type:
            hits = [h for h in hits if action_type in h.get("action_taken", "")]

        return hits

    @property
    def is_mock(self) -> bool:
        """True if this client is running in local mock mode."""
        return self._is_mock

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    def _load_local_incidents(self) -> list[dict]:
        """Load seed incidents from the local JSON file."""
        try:
            with open(self._incidents_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("AzureSearchClient(mock): cannot load incidents: %s", exc)
            return []

    def _mock_search(
        self,
        query: str,
        action_type: str | None,
        resource_type: str | None,
        top: int,
    ) -> list[dict]:
        """Simple keyword + field-filter search over the local incidents list."""
        results = list(self._incidents)

        # Field filters (exact match on action_taken prefix / resource_type)
        if action_type:
            results = [r for r in results if action_type in r.get("action_taken", "")]
        if resource_type:
            results = [r for r in results if r.get("resource_type") == resource_type]

        # Keyword relevance: count how many query words appear in combined text
        query_words = query.lower().split()

        def _relevance(incident: dict) -> int:
            text = " ".join([
                incident.get("description", ""),
                incident.get("action_taken", ""),
                " ".join(incident.get("tags", [])),
                incident.get("lesson", ""),
            ]).lower()
            return sum(1 for word in query_words if word in text)

        results.sort(key=_relevance, reverse=True)
        return results[:top]
