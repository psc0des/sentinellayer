#!/usr/bin/env bash
# =============================================================================
# setup_env.sh - Auto-generate .env from Terraform outputs
# =============================================================================
# Default mode (recommended):
#   bash scripts/setup_env.sh
#   Writes endpoint values + Key Vault secret names (no plaintext keys).
#
# Local override mode:
#   bash scripts/setup_env.sh --include-keys
#   Also writes raw API keys into .env (convenient for local testing).
#
# Non-interactive mode:
#   bash scripts/setup_env.sh --no-prompt
#   Skips prompts and uses Azure CLI defaults when available.
# =============================================================================

set -euo pipefail

INCLUDE_KEYS=false
NO_PROMPT=false

for arg in "$@"; do
  case "$arg" in
    --include-keys)
      INCLUDE_KEYS=true
      ;;
    --no-prompt)
      NO_PROMPT=true
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TERRAFORM_DIR="$PROJECT_ROOT/infrastructure/terraform-core"
TERRAFORM_PROD_DIR="$PROJECT_ROOT/infrastructure/terraform-prod"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE_FILE="$PROJECT_ROOT/.env.example"

echo "Reading Terraform outputs from $TERRAFORM_DIR ..."
cd "$TERRAFORM_DIR"

if ! command -v terraform >/dev/null 2>&1; then
  echo "ERROR: terraform command not found in PATH for this shell."
  echo "Use a shell where terraform is installed, or run from PowerShell."
  exit 1
fi

if ! terraform output -raw foundry_endpoint >/dev/null 2>&1; then
  echo "ERROR: Terraform has no outputs available."
  echo "Run 'terraform apply' in infrastructure/terraform-core/ first,"
  echo "or run 'terraform init' if using remote state."
  exit 1
fi

tf_raw() {
  terraform output -raw "$1"
}

tf_raw_optional_from_dir() {
  local dir="$1"
  local key="$2"
  if [ -d "$dir" ] && [ -f "$dir/terraform.tfstate" ]; then
    (cd "$dir" && terraform output -raw "$key" 2>/dev/null) || true
  fi
}

az_tsv() {
  az account show --query "$1" -o tsv 2>/dev/null || true
}

# Non-sensitive outputs
FOUNDRY_ENDPOINT=$(tf_raw foundry_endpoint)
FOUNDRY_DEPLOYMENT=$(tf_raw foundry_deployment)
SEARCH_ENDPOINT=$(tf_raw search_endpoint)
SEARCH_INDEX=$(tf_raw search_index_name)
COSMOS_ENDPOINT=$(tf_raw cosmos_endpoint)
COSMOS_DATABASE=$(tf_raw cosmos_database)
COSMOS_CONTAINER=$(tf_raw cosmos_container_decisions)
LOG_WORKSPACE_ID=$(tf_raw log_analytics_workspace_id)
KEYVAULT_URL=$(tf_raw keyvault_url)
FOUNDRY_KEY_SECRET_NAME=$(tf_raw keyvault_secret_name_foundry_key)
SEARCH_KEY_SECRET_NAME=$(tf_raw keyvault_secret_name_search_key)
COSMOS_KEY_SECRET_NAME=$(tf_raw keyvault_secret_name_cosmos_key)

# Live topology defaults:
# Prefer terraform-prod workspace GUID when available (monitoring/demo alerts),
# otherwise fall back to core terraform output.
PROD_LOG_WORKSPACE_GUID="$(tf_raw_optional_from_dir "$TERRAFORM_PROD_DIR" "log_analytics_workspace_guid")"
LOG_WORKSPACE_SOURCE="core"
if [ -n "${PROD_LOG_WORKSPACE_GUID}" ]; then
  LOG_WORKSPACE_ID="${PROD_LOG_WORKSPACE_GUID}"
  LOG_WORKSPACE_SOURCE="prod"
fi
USE_LIVE_TOPOLOGY=true

# Optional plaintext keys
FOUNDRY_KEY=""
SEARCH_KEY=""
COSMOS_KEY=""
if [ "$INCLUDE_KEYS" = true ]; then
  echo "Including plaintext service keys in .env (local/dev mode)."
  FOUNDRY_KEY=$(terraform output -raw foundry_primary_key)
  SEARCH_KEY=$(terraform output -raw search_primary_key)
  COSMOS_KEY=$(terraform output -raw cosmos_primary_key)
fi

# Subscription and tenant defaults from the current Azure CLI account
DEFAULT_SUBSCRIPTION_ID=$(az_tsv id)
DEFAULT_TENANT_ID=$(az_tsv tenantId)

# Prompt only when attached to an interactive terminal and no --no-prompt flag
SUBSCRIPTION_ID="${DEFAULT_SUBSCRIPTION_ID}"
TENANT_ID="${DEFAULT_TENANT_ID}"

if [ "$NO_PROMPT" = false ] && [ -t 0 ]; then
  echo ""
  echo "Enter Azure account identifiers for .env (press Enter to accept defaults)."
  read -r -p "AZURE_SUBSCRIPTION_ID [${DEFAULT_SUBSCRIPTION_ID:-<your-subscription-id>}]: " INPUT_SUBSCRIPTION_ID
  read -r -p "AZURE_TENANT_ID [${DEFAULT_TENANT_ID:-<your-tenant-id>}]: " INPUT_TENANT_ID

  if [ -n "${INPUT_SUBSCRIPTION_ID}" ]; then
    SUBSCRIPTION_ID="${INPUT_SUBSCRIPTION_ID}"
  fi
  if [ -n "${INPUT_TENANT_ID}" ]; then
    TENANT_ID="${INPUT_TENANT_ID}"
  fi
fi

# Final fallback placeholders if Azure CLI is unavailable and no input provided
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-<your-subscription-id>}"
TENANT_ID="${TENANT_ID:-<your-tenant-id>}"

cd "$PROJECT_ROOT"

if [ ! -f "$ENV_EXAMPLE_FILE" ]; then
  echo "ERROR: .env.example not found at $ENV_EXAMPLE_FILE"
  exit 1
fi

declare -A OVERRIDES
OVERRIDES[USE_LOCAL_MOCKS]="false"
OVERRIDES[USE_LIVE_TOPOLOGY]="$USE_LIVE_TOPOLOGY"
OVERRIDES[DEMO_MODE]="false"
OVERRIDES[LLM_CONCURRENCY_LIMIT]="3"
OVERRIDES[SEQUENTIAL_LLM]="false"
OVERRIDES[AZURE_OPENAI_ENDPOINT]="$FOUNDRY_ENDPOINT"
OVERRIDES[AZURE_OPENAI_API_KEY]="$FOUNDRY_KEY"
OVERRIDES[AZURE_OPENAI_API_KEY_SECRET_NAME]="$FOUNDRY_KEY_SECRET_NAME"
OVERRIDES[AZURE_OPENAI_DEPLOYMENT]="$FOUNDRY_DEPLOYMENT"
OVERRIDES[AZURE_OPENAI_API_VERSION]="2025-01-01-preview"
OVERRIDES[AZURE_SEARCH_ENDPOINT]="$SEARCH_ENDPOINT"
OVERRIDES[AZURE_SEARCH_API_KEY]="$SEARCH_KEY"
OVERRIDES[AZURE_SEARCH_API_KEY_SECRET_NAME]="$SEARCH_KEY_SECRET_NAME"
OVERRIDES[AZURE_SEARCH_INDEX]="$SEARCH_INDEX"
OVERRIDES[COSMOS_ENDPOINT]="$COSMOS_ENDPOINT"
OVERRIDES[COSMOS_KEY]="$COSMOS_KEY"
OVERRIDES[COSMOS_KEY_SECRET_NAME]="$COSMOS_KEY_SECRET_NAME"
OVERRIDES[COSMOS_DATABASE]="$COSMOS_DATABASE"
OVERRIDES[COSMOS_CONTAINER_DECISIONS]="$COSMOS_CONTAINER"
OVERRIDES[COSMOS_CONTAINER_SCAN_RUNS]="governance-scan-runs"
OVERRIDES[AZURE_SUBSCRIPTION_ID]="$SUBSCRIPTION_ID"
OVERRIDES[AZURE_TENANT_ID]="$TENANT_ID"
OVERRIDES[DEFAULT_RESOURCE_GROUP]=""
OVERRIDES[LOG_ANALYTICS_WORKSPACE_ID]="$LOG_WORKSPACE_ID"
OVERRIDES[AZURE_KEYVAULT_URL]="$KEYVAULT_URL"
OVERRIDES[AZURE_MANAGED_IDENTITY_CLIENT_ID]=""

tmp_file="$(mktemp)"
declare -A SEEN_KEYS

while IFS= read -r line || [ -n "$line" ]; do
  if [[ "$line" =~ ^([A-Z0-9_]+)=.*$ ]]; then
    key="${BASH_REMATCH[1]}"
    if [[ -v OVERRIDES["$key"] ]]; then
      printf "%s=%s\n" "$key" "${OVERRIDES[$key]}" >> "$tmp_file"
      SEEN_KEYS["$key"]=1
    else
      printf "%s\n" "$line" >> "$tmp_file"
    fi
  else
    printf "%s\n" "$line" >> "$tmp_file"
  fi
done < "$ENV_EXAMPLE_FILE"

# Backward compatibility: append required generated keys if missing in .env.example.
for key in "${!OVERRIDES[@]}"; do
  if [[ ! -v SEEN_KEYS["$key"] ]]; then
    printf "%s=%s\n" "$key" "${OVERRIDES[$key]}" >> "$tmp_file"
  fi
done

mv "$tmp_file" "$ENV_FILE"

echo ""
echo ".env written to $ENV_FILE"
echo ""
echo "Configured values:"
echo "  AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID"
echo "  AZURE_TENANT_ID=$TENANT_ID"
echo "  LOG_ANALYTICS_WORKSPACE_ID=$LOG_WORKSPACE_ID ($LOG_WORKSPACE_SOURCE)"
echo ""
echo "ACTION REQUIRED:"
echo "  Ensure your runtime identity can read Key Vault secrets (Get/List)."
echo ""
if [ "$INCLUDE_KEYS" = false ]; then
  echo "Secure mode used: keys were NOT written to .env."
  echo "Use '--include-keys' only for local/dev fallback."
fi
echo ""
echo "Seed search data:"
echo "  python scripts/seed_data.py"
