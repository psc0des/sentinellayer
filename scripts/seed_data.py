"""Seed demo data into Azure AI Search.

Run this after deploying infrastructure and generating .env:

    bash scripts/setup_env.sh      # step 1 — writes .env from Terraform outputs
    python scripts/seed_data.py    # step 2 — creates index + uploads documents

What this script does
---------------------
seed_incidents  → creates the 'incident-history' index in Azure AI Search,
                  then uploads all 7 incidents from data/seed_incidents.json.
seed_resources  → reports resource count (no Azure upload — resources are
                  served from the JSON mock via ResourceGraphClient).
seed_policies   → validates policies.json and prints each policy.

Mock-mode behaviour
-------------------
If USE_LOCAL_MOCKS=true (default) or AZURE_SEARCH_ENDPOINT is not set,
the script skips the Azure upload and prints a reminder.  The JSON files
are still validated so you can run this safely before infrastructure exists.
"""

import os
import sys

# Add project root to path so `from src.config import settings` works
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

from src.config import settings  # reads .env automatically via pydantic-settings
from src.infrastructure.secrets import KeyVaultSecretResolver

_DATA_DIR = Path(__file__).parent.parent / "data"


def _load_json(filename: str) -> list | dict:
    """Load a JSON file from the data/ directory."""
    with open(_DATA_DIR / filename, encoding="utf-8") as fh:
        return json.load(fh)


# =============================================================================
# Azure AI Search — incident history index
# =============================================================================

def _create_index(index_client, index_name: str) -> None:
    """Create or replace the Azure AI Search index schema.

    Why create_or_update_index?
    Running this function twice is safe — it overwrites the schema without
    deleting existing documents.  Good for iterating on the index definition.

    Field types explained:
    - SimpleField     → stored and filterable, but NOT searched in full-text
    - SearchableField → included in full-text search AND can be stored/filtered
    - key=True        → the unique document ID (like a primary key in a database)
    - filterable=True → can be used in OData $filter expressions
    - facetable=True  → can be used in faceted navigation (count by category)
    - sortable=True   → can be used in $orderby clauses
    - collection=True → the field holds a list of values (tags is a list in JSON)
    """
    from azure.search.documents.indexes.models import (
        SearchableField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
    )

    fields = [
        # Key field — must be unique per document, used as the document ID
        SimpleField(
            name="incident_id",
            type=SearchFieldDataType.String,
            key=True,
        ),
        # Full-text searchable fields — queried by search_client.search_incidents()
        SearchableField(name="description",  type=SearchFieldDataType.String),
        SearchableField(name="action_taken", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="outcome",      type=SearchFieldDataType.String),
        SearchableField(name="lesson",       type=SearchFieldDataType.String),
        # Filter / facet fields — not full-text searched, but used in OData filters
        # resource_type filter is used by search_client.py in Azure mode
        SimpleField(name="resource_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="service",       type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="severity",      type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="date",          type=SearchFieldDataType.String, sortable=True),
        # Tags — a list of strings in JSON, maps to Collection(Edm.String) in Azure Search
        SearchableField(name="tags", type=SearchFieldDataType.String, collection=True),
    ]

    index = SearchIndex(name=index_name, fields=fields)
    index_client.create_or_update_index(index)


def seed_incidents() -> None:
    """Create the incident-history index and upload all seed incidents."""
    incidents = _load_json("seed_incidents.json")
    print(f"📋 Loaded {len(incidents)} incidents from data/seed_incidents.json")

    if settings.use_local_mocks or not settings.azure_search_endpoint:
        print("   Skipping Azure upload — USE_LOCAL_MOCKS=true or endpoint not set.")
        print("   To seed live: set USE_LOCAL_MOCKS=false and run setup_env.sh first.")
        return

    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient

    api_key = KeyVaultSecretResolver(settings).resolve(
        direct_value=settings.azure_search_api_key,
        secret_name=settings.azure_search_api_key_secret_name,
        setting_name="AZURE_SEARCH_API_KEY",
    )
    if not api_key:
        print("   Skipping Azure upload - no Azure Search API key in env or Key Vault.")
        print("   Ensure AZURE_KEYVAULT_URL + secret name are set, or set AZURE_SEARCH_API_KEY.")
        return

    credential = AzureKeyCredential(api_key)

    # Step 1 — create or update the index schema
    index_client = SearchIndexClient(
        endpoint=settings.azure_search_endpoint,
        credential=credential,
    )
    _create_index(index_client, settings.azure_search_index)
    print(f"   ✓ Index '{settings.azure_search_index}' created/updated")

    # Step 2 — upload documents
    # upload_documents returns one IndexingResult per document.
    # Each result has .succeeded (bool) and .key (the incident_id).
    search_client = SearchClient(
        endpoint=settings.azure_search_endpoint,
        index_name=settings.azure_search_index,
        credential=credential,
    )
    results = search_client.upload_documents(documents=incidents)

    succeeded = sum(1 for r in results if r.succeeded)
    failed    = len(incidents) - succeeded

    print(f"   ✓ Uploaded {succeeded}/{len(incidents)} incidents")
    if failed:
        failed_keys = [r.key for r in results if not r.succeeded]
        print(f"   ⚠️  {failed} upload(s) failed: {failed_keys}")


# =============================================================================
# Resource topology (no Azure upload — served from JSON mock)
# =============================================================================

def seed_resources() -> None:
    """Report resource count. No Azure upload needed.

    ResourceGraphClient serves resources from data/seed_resources.json
    in mock mode, and from Azure Resource Graph API in live mode.
    Neither path requires pre-seeding a database.
    """
    data = _load_json("seed_resources.json")
    resources = data.get("resources", [])
    print(f"🖥️  Loaded {len(resources)} resources from data/seed_resources.json")
    print("   Resources are served from JSON mock (no Azure upload needed).")


# =============================================================================
# Policies (no Azure upload — loaded from JSON at runtime)
# =============================================================================

def seed_policies() -> None:
    """Load and validate governance policies. No Azure upload needed."""
    policies = _load_json("policies.json")
    print(f"🏛️  Loaded {len(policies)} policies from data/policies.json")
    for p in policies:
        print(f"   ✓ {p['id']}: {p['name']} [{p['severity']}]")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    print("=" * 55)
    print("SentinelLayer — Seed Demo Data")
    print("=" * 55)
    print(f"Mode: {'MOCK (local JSON)' if settings.use_local_mocks else 'LIVE (Azure)'}")
    print()

    seed_incidents()
    print()
    seed_resources()
    print()
    seed_policies()

    print()
    print("✅ Done.")
