"""SentinelLayer configuration — loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    # --- Azure AI Foundry (accessed via the OpenAI SDK — same env var names) ---
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = "gpt-4o-mini"
    azure_openai_api_version: str = "2024-12-01-preview"

    # --- Azure AI Search ---
    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_search_index: str = "incident-history"

    # --- Azure Cosmos DB (SQL API — Decisions) ---
    cosmos_endpoint: str = ""
    cosmos_key: str = ""
    cosmos_database: str = "sentinellayer"
    cosmos_container_decisions: str = "governance-decisions"

    # --- Azure Cosmos DB (Gremlin — Dependency Graph) ---
    cosmos_gremlin_endpoint: str = ""
    cosmos_gremlin_database: str = "dependency-graph"
    cosmos_gremlin_container: str = "resources"

    # --- Azure Resource Graph ---
    azure_subscription_id: str = ""
    azure_tenant_id: str = ""

    # --- Azure Monitor ---
    log_analytics_workspace_id: str = ""

    # --- Azure Key Vault ---
    azure_keyvault_url: str = ""

    # --- SRI™ Thresholds ---
    sri_auto_approve_threshold: int = 25
    sri_human_review_threshold: int = 60

    # --- SRI™ Dimension Weights ---
    sri_weight_infrastructure: float = 0.30
    sri_weight_policy: float = 0.25
    sri_weight_historical: float = 0.25
    sri_weight_cost: float = 0.20

    # --- Dashboard API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Mock vs Azure mode ---
    # True  → all infrastructure clients use local JSON files (default, safe offline)
    # False → clients use real Azure SDKs (requires credentials set above)
    # If False but credentials are missing, each client falls back to mock automatically.
    use_local_mocks: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
