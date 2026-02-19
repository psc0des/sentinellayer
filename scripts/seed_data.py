"""Seed demo data into Azure AI Search and Cosmos DB.

Run this script after deploying Azure services and configuring .env:
    python scripts/seed_data.py
"""

import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_json(filepath: str) -> list | dict:
    """Load JSON file from data directory."""
    data_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    with open(os.path.join(data_dir, filepath)) as f:
        return json.load(f)


def seed_incidents():
    """Seed incident history into Azure AI Search."""
    incidents = load_json("seed_incidents.json")
    print(f"📋 Loaded {len(incidents)} incidents from seed_incidents.json")

    # TODO: Implement Azure AI Search indexing
    # 1. Create index with vector field
    # 2. Generate embeddings for each incident
    # 3. Upload documents to index
    print("⏳ Azure AI Search seeding — not yet implemented")


def seed_resources():
    """Seed resource topology into Cosmos DB (Gremlin)."""
    resources = load_json("seed_resources.json")
    print(f"🖥️  Loaded {len(resources['resources'])} resources from seed_resources.json")

    # TODO: Implement Cosmos DB Gremlin graph seeding
    # 1. Create vertices for each resource
    # 2. Create edges for dependency relationships
    print("⏳ Cosmos DB graph seeding — not yet implemented")


def seed_policies():
    """Load and validate governance policies."""
    policies = load_json("policies.json")
    print(f"🏛️  Loaded {len(policies)} policies from policies.json")

    for p in policies:
        print(f"   ✓ {p['id']}: {p['name']} [{p['severity']}]")


if __name__ == "__main__":
    print("=" * 50)
    print("SentinelLayer — Seed Demo Data")
    print("=" * 50)
    print()

    seed_incidents()
    print()
    seed_resources()
    print()
    seed_policies()

    print()
    print("✅ Seed data loading complete.")
    print("   Implement Azure service connections to push to cloud.")
