"""RuriSkry configuration — loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    # --- Microsoft Foundry — GPT-4.1 (Terraform-managed) ---
    # Endpoint and key are populated automatically by scripts/setup_env.sh
    # from Terraform outputs (foundry_endpoint, foundry_primary_key).
    # The openai SDK is used for all calls — Foundry exposes a compatible API.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_key_secret_name: str = "foundry-primary-key"
    azure_openai_deployment: str = "gpt-41"
    azure_openai_api_version: str = "2025-01-01-preview"

    # --- Azure AI Search ---
    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_search_api_key_secret_name: str = "search-primary-key"
    azure_search_index: str = "incident-history"

    # --- Azure Cosmos DB (SQL API — Decisions) ---
    cosmos_endpoint: str = ""
    cosmos_key: str = ""
    cosmos_key_secret_name: str = "cosmos-primary-key"
    cosmos_database: str = "ruriskry"
    cosmos_container_decisions: str = "governance-decisions"
    cosmos_container_scan_runs: str = "governance-scan-runs"

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
    azure_managed_identity_client_id: str = ""

    # --- Microsoft Teams Notifications ---
    teams_webhook_url: str = ""
    teams_notifications_enabled: bool = True
    dashboard_url: str = "http://localhost:5173"

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

    # --- Default scan scope ---
    # When POST /api/scan/* body omits resource_group, this value is used.
    # Empty string = scan the whole subscription.
    default_resource_group: str = ""

    # --- Mock vs Azure mode ---
    # True  → all infrastructure clients use local JSON files (default, safe offline)
    # False → clients use real Azure SDKs (requires credentials set above)
    # If False but credentials are missing, each client falls back to mock automatically.
    use_local_mocks: bool = True

    # --- Live topology queries (Phase 19) ---
    # True  → BlastRadiusAgent and FinancialImpactAgent query Azure Resource Graph for
    #          real dependency topology and SKU cost instead of data/seed_resources.json.
    # False → governance agents always use the local JSON snapshot (default, safe for tests).
    # Only effective when use_local_mocks=False AND azure_subscription_id is set.
    # Env var: USE_LIVE_TOPOLOGY=true
    use_live_topology: bool = False

    # --- Demo Mode ---
    # True → ops agents return 1-2 hardcoded realistic ProposedActions so the
    # full pipeline (SRI scoring, governance engine, audit trail) can be tested
    # locally without Azure OpenAI credentials.  NOT a mock fallback — the
    # governance layer still runs deterministically in its normal mock mode.
    # Env var: DEMO_MODE=true
    demo_mode: bool = False

    # --- LLM Rate Limiting ---
    # Maximum number of simultaneous LLM calls across all governance agents.
    # Azure OpenAI free-tier deployments typically allow ~3 RPM; higher tiers allow more.
    # All four governance agents share one process-level semaphore from llm_throttle.py.
    # Set to 1 to serialise all LLM calls (safest for very tight quota deployments).
    # Env var: LLM_CONCURRENCY_LIMIT=3
    llm_concurrency_limit: int = 3

    # When true, pipeline.py runs governance agents sequentially (one at a time) instead
    # of asyncio.gather(). Use this when the Azure OpenAI quota is so tight that the
    # semaphore alone does not help (e.g. quota is 1 RPM).
    # Env var: SEQUENTIAL_LLM=true
    sequential_llm: bool = False

    # --- Execution Gateway (Phase 21) ---
    # GitHub PAT with Contents + Pull requests read/write on iac_github_repo.
    # Required for Terraform PR generation.  Empty = PR creation disabled.
    # Env var: GITHUB_TOKEN
    github_token: str = ""

    # GitHub repo that owns the IaC (e.g. "psc0des/ruriskry").
    # Env var: IAC_GITHUB_REPO
    iac_github_repo: str = ""

    # Path within iac_github_repo to the Terraform config directory.
    # Env var: IAC_TERRAFORM_PATH
    iac_terraform_path: str = "infrastructure/terraform-prod"

    # Master on/off switch for the Execution Gateway.
    # False (default) = verdicts are informational only; no PRs are created.
    # Set to true to enable PR generation for APPROVED + IaC-managed verdicts.
    # Env var: EXECUTION_GATEWAY_ENABLED=true
    execution_gateway_enabled: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
