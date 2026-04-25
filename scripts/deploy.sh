#!/usr/bin/env bash
# =============================================================================
# deploy.sh — First-time deploy for RuriSkry
# =============================================================================
# Provisions all Azure infrastructure and deploys both the FastAPI backend
# and React dashboard in the correct order.
#
# Usage (from repo root):
#   bash scripts/deploy.sh                      # full first-time deploy
#   bash scripts/deploy.sh --stage2             # skip Stage 1 + Docker; use if Stage 1 succeeded but Stage 2 failed
#   bash scripts/deploy.sh --upgrade-providers  # re-resolve Terraform providers within version constraints
#   bash scripts/deploy.sh --reset-admin        # delete admin_auth.json from the Container App (lost password recovery)
#
# NOTE: This script is for FIRST-TIME deploys only.
# For subsequent code or infra changes, use the "Redeploy Workflows" section
# in infrastructure/terraform-core/deploy.md — those are faster and skip the
# Docker rebuild and full Terraform apply.
#
# Prerequisites:
#   1. Copy and fill in terraform.tfvars:
#        cp infrastructure/terraform-core/terraform.tfvars.example \
#           infrastructure/terraform-core/terraform.tfvars
#      Edit terraform.tfvars — set subscription_id and suffix at minimum.
#
#   2. Tools required: terraform, az (Azure CLI), docker, node, npm
#      Run `az login` before executing this script.
#
# Everything else (provider registration, remote state storage, backend.hcl,
# Docker build/push, Terraform apply, dashboard deploy) is fully automated.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$REPO_ROOT/infrastructure/terraform-core"
DASHBOARD_DIR="$REPO_ROOT/dashboard"

# ── Flags ─────────────────────────────────────────────────────────────────────
STAGE2_ONLY=false
UPGRADE_PROVIDERS=false
RESET_ADMIN=false
for arg in "$@"; do
  case "$arg" in
    --stage2)            STAGE2_ONLY=true ;;
    --upgrade-providers) UPGRADE_PROVIDERS=true ;;
    --reset-admin)       RESET_ADMIN=true ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${BLUE}▶  $*${NC}"; }
ok()   { echo -e "${GREEN}✓  $*${NC}"; }
warn() { echo -e "${YELLOW}⚠  $*${NC}"; }
die()  { echo -e "${RED}✗  $*${NC}" >&2; exit 1; }
step() { echo ""; echo -e "${BOLD}${BLUE}══ $* ══${NC}"; }

# Catch any unhandled command failure and print the line + command before exiting.
# This fires for unexpected errors only — intentional die() calls already print
# their own message and exit before the trap can fire.
trap 'echo -e "\n${RED}${BOLD}✗  Unexpected error on line $LINENO${NC}" >&2
      echo -e "${RED}   Failed command: $BASH_COMMAND${NC}" >&2
      echo -e "${RED}   Re-run with: bash -x scripts/deploy.sh  (for full trace)${NC}" >&2' ERR

# =============================================================================
# --reset-admin: wipe the admin account from both the local filesystem file
# AND the Cosmos DB backup record, so the setup screen reappears on the next
# dashboard visit (lost-password recovery).
#
# Admin credentials are stored in two layers (three-layer read on startup):
#   1. In-memory cache (cleared on restart)
#   2. /app/data/admin_auth.json (ephemeral — cleared by container revision)
#   3. Cosmos DB governance-agents container (durable — must be deleted explicitly)
# Both layers must be cleared for the setup screen to reappear.
# =============================================================================
if [[ "$RESET_ADMIN" == "true" ]]; then
  step "Admin password reset"
  cd "$TF_DIR"
  APP_NAME=$(terraform output -raw backend_container_app_name 2>/dev/null \
    || { die "Could not read backend_container_app_name from Terraform state. Run from the repo root after a successful deploy."; })
  RG_NAME=$(terraform output -raw resource_group_name 2>/dev/null \
    || die "Could not read resource_group_name from Terraform state.")

  log "Clearing admin auth from filesystem + Cosmos on Container App: $APP_NAME"

  # Step 1 — delete the local file AND the Cosmos record in one exec call
  az containerapp exec \
    --name "$APP_NAME" \
    --resource-group "$RG_NAME" \
    --command "python -c \"
import os, sys
sys.path.insert(0, '/app')
# Delete local file
try:
    os.remove('/app/data/admin_auth.json')
    print('Deleted admin_auth.json')
except FileNotFoundError:
    print('admin_auth.json not found (already gone)')

# Delete Cosmos record
try:
    from src.infrastructure.cosmos_client import CosmosAdminClient
    CosmosAdminClient().delete()
    print('Deleted Cosmos admin record')
except Exception as e:
    print(f'Cosmos delete failed (may be offline): {e}')
\"" \
    --output none 2>/dev/null \
  || warn "exec failed — the Container App may be stopped. Attempting revision restart to clear the file layer."

  # Step 2 — restart forces a fresh in-memory state (clears memory cache)
  az containerapp revision restart \
    --name "$APP_NAME" \
    --resource-group "$RG_NAME" \
    --revision "$(az containerapp revision list \
        --name "$APP_NAME" \
        --resource-group "$RG_NAME" \
        --query "[?properties.active].name | [0]" -o tsv)" \
    --output none
  ok "Admin account reset. Visit the dashboard — you will see the first-time setup screen again."
  exit 0
fi

# =============================================================================
# 0. Prerequisites
# =============================================================================
step "Checking prerequisites"

command -v terraform &>/dev/null \
  || die "terraform not found. Install: https://developer.hashicorp.com/terraform/downloads"
command -v az &>/dev/null \
  || die "Azure CLI not found. Install: https://aka.ms/installazurecli"
command -v docker &>/dev/null \
  || die "Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop"
command -v node &>/dev/null \
  || die "Node.js not found. Install: https://nodejs.org"
command -v npm &>/dev/null \
  || die "npm not found. Install: https://nodejs.org"

# npx ships with npm 5.2+ but verify it's available since the SWA deploy uses it
command -v npx &>/dev/null \
  || die "npx not found. Upgrade npm: npm install -g npm"

# Resolve python binary — 'python3' on Linux/macOS, often 'python' on Windows
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
[[ -n "$PYTHON" ]] \
  || die "Python not found. Install: https://www.python.org/downloads"

az account show &>/dev/null \
  || die "Not logged in to Azure. Run: az login"

if [[ "$STAGE2_ONLY" == false ]]; then
  docker info &>/dev/null \
    || die "Docker daemon not running. Start Docker Desktop and try again."
fi

[[ -f "$TF_DIR/terraform.tfvars" ]] \
  || die "terraform.tfvars not found.\n   Run: cp $TF_DIR/terraform.tfvars.example $TF_DIR/terraform.tfvars\n   Then fill in subscription_id and suffix."

# Read suffix early — needed for tfstate storage name
SUFFIX=$(grep -E '^suffix\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
[[ -n "$SUFFIX" && "$SUFFIX" != "replace-me" ]] \
  || die "suffix is not set in terraform.tfvars.\n   Open terraform.tfvars and set a short unique suffix (e.g. \"jd4821\")."

# Read subscription_id and explicitly set it — prevents SubscriptionNotFound errors
# on new subscriptions where az defaults can lag behind
SUBSCRIPTION_ID=$(grep -E '^subscription_id\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
[[ -n "$SUBSCRIPTION_ID" ]] \
  || die "subscription_id is not set in terraform.tfvars."
az account set --subscription "$SUBSCRIPTION_ID" \
  || die "Could not set subscription $SUBSCRIPTION_ID.\n   Run: az login\n   Then verify: az account list -o table"
ok "Active subscription: $SUBSCRIPTION_ID"

# =============================================================================
# 0. Pre-flight: Foundry quota check
# =============================================================================
# Check Foundry model quota BEFORE running Terraform.
# A quota of 0 causes a mid-apply failure that is confusing to diagnose.
# This check fails fast with clear instructions so the user knows what to do.

FOUNDRY_LOCATION=$(grep -E '^foundry_location\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
FOUNDRY_LOCATION=${FOUNDRY_LOCATION:-eastus2}

FOUNDRY_MODEL=$(grep -E '^foundry_model\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
FOUNDRY_MODEL=${FOUNDRY_MODEL:-gpt-4.1-mini}

FOUNDRY_SCALE_TYPE=$(grep -E '^foundry_scale_type\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
FOUNDRY_SCALE_TYPE=${FOUNDRY_SCALE_TYPE:-Standard}

CREATE_DEPLOYMENT=$(grep -E '^create_foundry_deployment\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*//;s/\s*#.*//' | tr -d '[:space:]')
CREATE_DEPLOYMENT=${CREATE_DEPLOYMENT:-true}

if [[ "$CREATE_DEPLOYMENT" == "true" && "$STAGE2_ONLY" == "false" ]]; then
  step "Pre-flight: checking Foundry quota"
  # Azure quota key naming is inconsistent:
  #   gpt-5.x series: key uses hyphen    → OpenAI.Standard.gpt-5-mini
  #   gpt-4.x series: key drops hyphen   → OpenAI.Standard.gpt4.1-mini  (NOT gpt-4.1-mini)
  # Try exact match first, then the gpt-4.x alternate (remove hyphen before version digit).
  QUOTA_KEY="OpenAI.${FOUNDRY_SCALE_TYPE}.${FOUNDRY_MODEL}"
  QUOTA_KEY_ALT="OpenAI.${FOUNDRY_SCALE_TYPE}.$(echo "$FOUNDRY_MODEL" | sed 's/gpt-\([0-9]\)/gpt\1/')"

  QUOTA=$(az cognitiveservices usage list \
    --location "$FOUNDRY_LOCATION" \
    --subscription "$SUBSCRIPTION_ID" \
    --query "[?name.value=='${QUOTA_KEY}'].limit" \
    -o tsv 2>/dev/null | head -1)

  # If exact key returned 0 or empty, try alternate key
  if [[ -z "$QUOTA" || "${QUOTA%.*}" == "0" ]]; then
    QUOTA_ALT=$(az cognitiveservices usage list \
      --location "$FOUNDRY_LOCATION" \
      --subscription "$SUBSCRIPTION_ID" \
      --query "[?name.value=='${QUOTA_KEY_ALT}'].limit" \
      -o tsv 2>/dev/null | head -1)
    if [[ -n "$QUOTA_ALT" && "${QUOTA_ALT%.*}" -gt 0 ]]; then
      QUOTA="$QUOTA_ALT"
      QUOTA_KEY="$QUOTA_KEY_ALT"
    fi
  fi

  QUOTA=${QUOTA:-0}
  # Strip decimal (0.0 → 0)
  QUOTA_INT=${QUOTA%.*}

  if [[ "${QUOTA_INT:-0}" -gt 0 ]]; then
    ok "Foundry quota: ${QUOTA} units for ${FOUNDRY_MODEL} in ${FOUNDRY_LOCATION}"
  else
    echo ""
    echo -e "${RED}${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}${BOLD}║  Foundry quota is 0 — deploy will fail without it       ║${NC}"
    echo -e "${RED}${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "  Model      : $FOUNDRY_MODEL"
    echo "  Scale type : $FOUNDRY_SCALE_TYPE"
    echo "  Region     : $FOUNDRY_LOCATION"
    echo "  Quota key  : $QUOTA_KEY  ← limit is 0"
    echo ""
    echo "  How to request quota (takes a few minutes to hours):"
    echo "  1. Go to https://ai.azure.com/quota"
    echo "  2. Select region: $FOUNDRY_LOCATION"
    echo "  3. Find '$FOUNDRY_MODEL — $FOUNDRY_SCALE_TYPE' → Request increase → enter 3"
    echo "     (3 units = 30,000 TPM, enough to run RuriSkry)"
    echo "  4. Wait for approval (usually minutes for small requests)"
    echo "  5. Re-run this script once approved"
    echo ""
    echo "  Check approval status:"
    echo "    az cognitiveservices usage list --location $FOUNDRY_LOCATION \\"
    echo "      --subscription $SUBSCRIPTION_ID \\"
    echo "      --query \"[?contains(name.value,'${FOUNDRY_MODEL}')].limit\" -o tsv"
    echo ""
    echo -e "  ${BOLD}Press Enter to continue anyway (if you know quota was just approved)${NC}"
    echo    "  or Ctrl+C to exit and request quota first."
    read -r _
    # Re-check after user confirms
    QUOTA2=$(az cognitiveservices usage list \
      --location "$FOUNDRY_LOCATION" \
      --subscription "$SUBSCRIPTION_ID" \
      --query "[?name.value=='${QUOTA_KEY}'].limit" \
      -o tsv 2>/dev/null | head -1)
    QUOTA2=${QUOTA2:-0}
    QUOTA2_INT=${QUOTA2%.*}
    if [[ "${QUOTA2_INT:-0}" -gt 0 ]]; then
      ok "Quota confirmed: ${QUOTA2} units — continuing"
    else
      warn "Quota still shows 0 — continuing anyway. Terraform may fail on the Foundry deployment."
      warn "If it does, fix quota then re-run: bash scripts/deploy.sh --stage2"
    fi
  fi
fi

ok "All prerequisites satisfied"

# =============================================================================
# 0. Pre-flight: purge soft-deleted resources from previous failed deploys
# =============================================================================
# If a previous deploy partially succeeded and the resource group was deleted,
# Azure soft-deletes Cognitive Services accounts and Key Vault secrets.
# Terraform cannot recreate them until purged — this causes confusing 409 errors.
# This step detects and purges them automatically so re-deploys work cleanly.

FOUNDRY_ACCOUNT_NAME=$(grep -E '^foundry_account_name\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
# If blank, derive the name the same way Terraform does: <prefix>-foundry-<suffix>
# prefix = "ruriskry-${env}" where env defaults to "core"
ENV_VAL=$(grep -E '^env\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
ENV_VAL=${ENV_VAL:-core}
if [[ -z "$FOUNDRY_ACCOUNT_NAME" ]]; then
  FOUNDRY_ACCOUNT_NAME="ruriskry-${ENV_VAL}-foundry-${SUFFIX}"
fi

KV_NAME="ruriskry-${ENV_VAL}-kv-${SUFFIX}"

step "Pre-flight: checking for soft-deleted resources"

# Check for soft-deleted Foundry / Cognitive Services account
DELETED_FOUNDRY=$(az cognitiveservices account list-deleted \
  --subscription "$SUBSCRIPTION_ID" \
  --query "[?name=='${FOUNDRY_ACCOUNT_NAME}'].name" \
  -o tsv 2>/dev/null)
if [[ -n "$DELETED_FOUNDRY" ]]; then
  log "Purging soft-deleted Foundry account: $FOUNDRY_ACCOUNT_NAME"
  az cognitiveservices account purge \
    --location "$FOUNDRY_LOCATION" \
    --resource-group "$(grep -E '^resource_group_name\s*=' "$TF_DIR/terraform.tfvars" | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]' || echo "ruriskry-${ENV_VAL}-engine-rg")" \
    --name "$FOUNDRY_ACCOUNT_NAME" \
    --subscription "$SUBSCRIPTION_ID" 2>/dev/null || true
  ok "Foundry account purged"
else
  ok "No soft-deleted Foundry account found"
fi

# Check for soft-deleted Key Vault
DELETED_KV=$(az keyvault list-deleted \
  --subscription "$SUBSCRIPTION_ID" \
  --query "[?name=='${KV_NAME}'].name" \
  -o tsv 2>/dev/null)
if [[ -n "$DELETED_KV" ]]; then
  log "Purging soft-deleted Key Vault: $KV_NAME"
  az keyvault purge --name "$KV_NAME" --location "$FOUNDRY_LOCATION" \
    --subscription "$SUBSCRIPTION_ID" 2>/dev/null || true
  ok "Key Vault purged"
else
  ok "No soft-deleted Key Vault found"
fi

# =============================================================================
# 0a. Register required Azure providers
# =============================================================================
step "Registering Azure providers"

for PROVIDER in Microsoft.Storage Microsoft.App Microsoft.ContainerService Microsoft.OperationalInsights Microsoft.KeyVault Microsoft.DocumentDB Microsoft.CognitiveServices Microsoft.Search Microsoft.Web Microsoft.AlertsManagement; do
  STATE=$(az provider show --namespace "$PROVIDER" --subscription "$SUBSCRIPTION_ID" --query "registrationState" -o tsv 2>/dev/null || echo "NotRegistered")
  if [[ "$STATE" == "Registered" ]]; then
    ok "$PROVIDER already registered"
  else
    log "Registering $PROVIDER (this may take a minute)..."
    az provider register --namespace "$PROVIDER" --subscription "$SUBSCRIPTION_ID" --wait
    ok "$PROVIDER registered"
  fi
done

# The APR and its resource group are created in the TARGET subscription (azurerm.target
# provider alias). If target_subscription_id differs from subscription_id, that sub also
# needs Microsoft.AlertsManagement registered — otherwise Terraform gets a 409 Conflict.
TARGET_SUB_EARLY=$(grep -E '^target_subscription_id\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]' || true)
if [[ -n "$TARGET_SUB_EARLY" && "$TARGET_SUB_EARLY" != "$SUBSCRIPTION_ID" ]]; then
  log "target_subscription_id ($TARGET_SUB_EARLY) differs from infra sub — registering Microsoft.AlertsManagement there too"
  for PROVIDER in Microsoft.AlertsManagement Microsoft.Insights; do
    STATE=$(az provider show --namespace "$PROVIDER" --subscription "$TARGET_SUB_EARLY" --query "registrationState" -o tsv 2>/dev/null || echo "NotRegistered")
    if [[ "$STATE" == "Registered" ]]; then
      ok "$PROVIDER already registered in target sub"
    else
      log "Registering $PROVIDER in target sub (this may take a minute)..."
      az provider register --namespace "$PROVIDER" --subscription "$TARGET_SUB_EARLY" --wait
      ok "$PROVIDER registered in target sub"
    fi
  done
fi

# =============================================================================
# 0b. Terraform remote state — auto-create if missing
# =============================================================================
TFSTATE_RG="ruriskry-tfstate-rg"
TFSTATE_SA="ruriskrytf${SUFFIX}"
TFSTATE_LOCATION="eastus2"

if [[ ! -f "$TF_DIR/backend.hcl" ]]; then
  step "Setting up Terraform remote state"

  # Create resource group if it doesn't exist
  if ! az group show --name "$TFSTATE_RG" --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
    log "Creating resource group: $TFSTATE_RG"
    az group create --name "$TFSTATE_RG" --location "$TFSTATE_LOCATION" \
      --subscription "$SUBSCRIPTION_ID" --output none
    ok "Resource group created: $TFSTATE_RG"
  else
    ok "Resource group already exists: $TFSTATE_RG"
  fi

  # Create storage account if it doesn't exist
  # Retry up to 4 times with 20s wait — new subscriptions have a propagation delay
  # where the Storage API doesn't recognise the subscription for ~60s after creation.
  if ! az storage account show --name "$TFSTATE_SA" --resource-group "$TFSTATE_RG" \
       --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
    log "Creating storage account: $TFSTATE_SA"
    SA_CREATED=false
    SA_ERR=""
    for attempt in 1 2 3 4; do
      # Capture stderr so the Azure error is available for the die message.
      # --output none suppresses the JSON success body; 2>&1 routes any error
      # message into SA_ERR instead of swallowing it with 2>/dev/null.
      if SA_ERR=$(az storage account create \
           --name "$TFSTATE_SA" \
           --resource-group "$TFSTATE_RG" \
           --location "$TFSTATE_LOCATION" \
           --subscription "$SUBSCRIPTION_ID" \
           --sku Standard_LRS \
           --allow-blob-public-access false \
           --min-tls-version TLS1_2 \
           --output none 2>&1); then
        SA_CREATED=true
        break
      fi
      if [[ "$attempt" -lt 4 ]]; then
        warn "Storage account creation failed (attempt $attempt/4) — waiting 20s for subscription to propagate..."
        sleep 20
      fi
    done
    [[ "$SA_CREATED" == true ]] \
      || die "Could not create storage account after 4 attempts.\n   Azure error: $SA_ERR"
    az storage container create \
      --name tfstate \
      --account-name "$TFSTATE_SA" \
      --auth-mode login \
      --output none
    ok "Storage account created: $TFSTATE_SA"
  else
    ok "Storage account already exists: $TFSTATE_SA"
  fi

  # Generate backend.hcl
  cat > "$TF_DIR/backend.hcl" <<EOF
resource_group_name  = "$TFSTATE_RG"
storage_account_name = "$TFSTATE_SA"
container_name       = "tfstate"
key                  = "terraform-core.tfstate"
EOF
  ok "backend.hcl created"
else
  ok "backend.hcl already exists — skipping remote state setup"
fi

# =============================================================================
# 1. Terraform init
# =============================================================================
step "Initialising Terraform"

cd "$TF_DIR"

BACKEND_CONFIG="-backend-config=backend.hcl"

if [[ "$UPGRADE_PROVIDERS" == true ]]; then
  terraform init -upgrade $BACKEND_CONFIG
else
  terraform init $BACKEND_CONFIG
fi
ok "Terraform initialised"

# =============================================================================
# 2. Stage 1 — Create ACR, Managed Identity, and role assignment only
# =============================================================================
# Why a targeted apply first?
#   The Container App requires the Docker image to already exist in ACR at
#   creation time (Azure validates the image on first revision). We need to:
#     a) create ACR first so we have a registry to push to
#     b) create the User-Assigned Managed Identity and its AcrPull role
#        assignment so permission is propagated before the Container App starts
#     c) wait 90 seconds for the role assignment to propagate globally
#   Only then do we push the Docker image and run the full apply.
#
# Skip with --stage2 if Stage 1 already succeeded on a previous run and you
# only need to retry Stage 2 onwards (e.g. after a transient Azure error).

if [[ "$STAGE2_ONLY" == true ]]; then
  warn "--stage2 flag set — skipping Stage 1 and Docker build"
  warn "Assuming ACR already exists from a previous Stage 1 run."
  ACR_NAME=$(terraform output -raw acr_name 2>/dev/null || true)
  [[ -n "$ACR_NAME" ]] \
    || die "Terraform state has no acr_name output.\n   Stage 1 has not completed yet.\n   Re-run without --stage2 to run the full deploy."
  ACR_SERVER=$(terraform output -raw acr_login_server)
  # Parse full repo:tag from backend_image var — default "ruriskry-backend:latest"
  BACKEND_IMAGE_FULL=$(grep -E '^backend_image\s*=' terraform.tfvars 2>/dev/null \
    | sed 's/.*=\s*"\([^"]*\)".*/\1/')
  BACKEND_IMAGE_FULL=${BACKEND_IMAGE_FULL:-ruriskry-backend:latest}
  BACKEND_IMAGE_REPO="${BACKEND_IMAGE_FULL%%:*}"
  BACKEND_IMAGE_TAG="${BACKEND_IMAGE_FULL##*:}"
  # Guard: image must already exist since --stage2 skips Docker build
  if ! az acr repository show-tags \
         --name "$ACR_NAME" \
         --repository "$BACKEND_IMAGE_REPO" \
         --query "[?@=='$BACKEND_IMAGE_TAG']" \
         --output tsv 2>/dev/null | grep -q "$BACKEND_IMAGE_TAG"; then
    die "Image $BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG not found in ACR $ACR_NAME.\n   Docker push did not complete. Re-run without --stage2 to build and push the image."
  fi
  ok "Image $BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG confirmed in ACR"
else
  step "Stage 1 — Provisioning ACR, Managed Identity, and role assignment"
  # Stage 1 creates only the resources needed before Docker push:
  #   - ACR (registry must exist before we can push the image)
  #   - User-Assigned Managed Identity + AcrPull role assignment
  #
  # No propagation sleep needed — the Container App starts with a public MCR
  # placeholder image (no ACR auth). deploy.sh swaps in the real ACR image
  # after Stage 2, by which time 15+ minutes have passed since role assignment.
  #
  # The Static Web App is created in Stage 2 alongside the Container App.
  # Terraform resolves the SWA → Container App dependency automatically:
  # it creates the SWA first, reads default_host_name, and passes the exact
  # URL into DASHBOARD_URL — no tfvars patching or re-apply needed.

  terraform apply -auto-approve \
    -target=azurerm_resource_group.ruriskry \
    -target=azurerm_container_registry.ruriskry \
    -target=azurerm_user_assigned_identity.acr_pull \
    -target=azurerm_role_assignment.acr_pull

  ACR_NAME=$(terraform output -raw acr_name)
  ACR_SERVER=$(terraform output -raw acr_login_server)
  # Parse full repo:tag from backend_image var — default "ruriskry-backend:latest"
  BACKEND_IMAGE_FULL=$(grep -E '^backend_image\s*=' terraform.tfvars 2>/dev/null \
    | sed 's/.*=\s*"\([^"]*\)".*/\1/')
  BACKEND_IMAGE_FULL=${BACKEND_IMAGE_FULL:-ruriskry-backend:latest}
  BACKEND_IMAGE_REPO="${BACKEND_IMAGE_FULL%%:*}"
  BACKEND_IMAGE_TAG="${BACKEND_IMAGE_FULL##*:}"
  ok "ACR ready: $ACR_SERVER"

  # =============================================================================
  # 3. Docker build + push
  # =============================================================================
  # Check if image already exists — skip rebuild if re-running after a partial failure
  IMAGE_EXISTS=false
  if az acr repository show-tags \
       --name "$ACR_NAME" \
       --repository "$BACKEND_IMAGE_REPO" \
       --query "[?@=='$BACKEND_IMAGE_TAG']" \
       --output tsv 2>/dev/null | grep -q "$BACKEND_IMAGE_TAG"; then
    IMAGE_EXISTS=true
  fi

  if [[ "$IMAGE_EXISTS" == true ]]; then
    warn "Image $BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG already exists in ACR — skipping Docker build."
    warn "To force a rebuild, delete the tag first:"
    warn "  az acr repository delete --name $ACR_NAME --image $BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG --yes"
  else
    step "Building and pushing Docker image"

    cd "$REPO_ROOT"

    log "Logging in to ACR..."
    az acr login --name "$ACR_NAME"

    log "Building backend image..."
    docker build -t "$ACR_SERVER/$BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG" .

    log "Pushing to ACR..."
    docker push "$ACR_SERVER/$BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG"
    ok "Image pushed: $ACR_SERVER/$BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG"
  fi
fi

# =============================================================================
# 4. Stage 2 — Full terraform apply (all remaining resources)
# =============================================================================
step "Stage 2 — Provisioning remaining infrastructure"

# Wait for Foundry project to reach terminal state before terraform apply.
# On re-apply, the project may still be in Creating/Updating from a prior run.
# terraform apply would fail with 409 "provisioning state is not terminal".
FOUNDRY_ACCOUNT=$(grep -E '^foundry_account_name\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
FOUNDRY_PROJECT=$(grep -E '^foundry_project_name\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
FOUNDRY_PROJECT=${FOUNDRY_PROJECT:-ruriskry}

if [[ -n "$FOUNDRY_ACCOUNT" ]]; then
  _RG=$(grep -E '^resource_group_name\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
    | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]' || echo "ruriskry-core-engine-rg")
  FOUNDRY_PROJECT_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${_RG}/providers/Microsoft.CognitiveServices/accounts/${FOUNDRY_ACCOUNT}/projects/${FOUNDRY_PROJECT}"
  log "Checking Foundry project provisioning state..."
  for i in $(seq 1 10); do
    STATE=$(az rest --method get \
      --url "https://management.azure.com${FOUNDRY_PROJECT_ID}?api-version=2025-04-01-preview" \
      --query "properties.provisioningState" -o tsv 2>/dev/null || echo "NotFound")
    if [[ "$STATE" == "Succeeded" || "$STATE" == "Failed" || "$STATE" == "Canceled" || "$STATE" == "NotFound" ]]; then
      [[ "$STATE" == "Succeeded" ]] && ok "Foundry project is ready (${STATE})"
      [[ "$STATE" == "NotFound" ]] && log "Foundry project not yet created — will be created by Terraform"
      [[ "$STATE" == "Failed" || "$STATE" == "Canceled" ]] && warn "Foundry project in ${STATE} state — Terraform will attempt recovery"
      break
    fi
    log "Foundry project still provisioning (${STATE}) — waiting 30s... (attempt $i/10)"
    sleep 30
  done
fi

cd "$TF_DIR"
terraform apply -auto-approve
ok "Infrastructure fully provisioned"

BACKEND_URL=$(terraform output -raw backend_url)
RG_NAME=$(terraform output -raw resource_group_name)
KV_NAME=$(terraform output -raw keyvault_name 2>/dev/null || echo "")
APP_NAME=$(terraform output -raw backend_container_app_name)
ACR_NAME=$(terraform output -raw acr_name)
ACR_SERVER=$(terraform output -raw acr_login_server)
ok "Backend URL: $BACKEND_URL"

# Parse image repo:tag (needed for --stage2 path where it wasn't set earlier)
if [[ -z "${BACKEND_IMAGE_REPO:-}" ]]; then
  BACKEND_IMAGE_FULL=$(grep -E '^backend_image\s*=' terraform.tfvars 2>/dev/null \
    | sed 's/.*=\s*"\([^"]*\)".*/\1/')
  BACKEND_IMAGE_FULL=${BACKEND_IMAGE_FULL:-ruriskry-backend:latest}
  BACKEND_IMAGE_REPO="${BACKEND_IMAGE_FULL%%:*}"
  BACKEND_IMAGE_TAG="${BACKEND_IMAGE_FULL##*:}"
fi

# =============================================================================
# 4a. Swap placeholder image → real ACR image
# =============================================================================
# Terraform creates the Container App with a public MCR placeholder image
# (no ACR auth needed). Now that Stage 2 is done — and 15+ minutes have passed
# since the AcrPull role was assigned in Stage 1 — the role is guaranteed
# propagated.  We update the Container App to pull from ACR.
step "Updating Container App image to ACR"

log "Swapping placeholder → $ACR_SERVER/$BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG"
az containerapp update \
  --name "$APP_NAME" \
  --resource-group "$RG_NAME" \
  --image "$ACR_SERVER/$BACKEND_IMAGE_REPO:$BACKEND_IMAGE_TAG" \
  --output none
ok "Container App now running real backend image"

# =============================================================================
# 4a-ii. Enable sticky sessions
# =============================================================================
# sticky_sessions_affinity was removed from the azurerm provider in ~4.63.0 and
# can no longer be set via Terraform.  We apply it here via CLI instead.
#
# WHY this is required: SSE queues and in-progress scan state are stored
# in-memory per replica.  Without sticky sessions a scan started on Replica A
# has its SSE stream routed to Replica B on the next request — which has no
# queue — causing "Scan log unavailable" errors for every multi-minute scan.
#
# The setting persists on the Container App across subsequent Terraform applies
# because ingress is in lifecycle { ignore_changes = [ingress] } in main.tf.
# This step is idempotent — safe to re-run on subsequent deploys.
step "Enabling sticky sessions on Container App"
az containerapp ingress sticky-sessions set \
  --name "$APP_NAME" \
  --resource-group "$RG_NAME" \
  --affinity sticky \
  --output none
ok "Sticky sessions enabled (affinity=sticky)"

# =============================================================================
# 4b. GitHub PAT — store in Key Vault if use_github_pat = true
# =============================================================================
# The Container App needs the github-pat KV secret to exist before it can start.
# We prompt here so the user doesn't have to do it manually after deployment.
USE_GITHUB_PAT=$(grep -cE '^use_github_pat\s*=\s*true' terraform.tfvars 2>/dev/null || true)
USE_GITHUB_PAT=${USE_GITHUB_PAT:-0}
if [[ "$USE_GITHUB_PAT" -gt 0 && -n "$KV_NAME" ]]; then
  if az keyvault secret show --vault-name "$KV_NAME" --name github-pat &>/dev/null; then
    ok "github-pat already exists in Key Vault — skipping"
  else
    echo ""
    warn "use_github_pat = true but github-pat is not in Key Vault."
    warn "The Container App cannot start without it."
    # Check env var first — allows non-interactive CI/CD deploys:
    #   export GITHUB_PAT="github_pat_xxx..."
    #   bash scripts/deploy.sh
    if [[ -z "${GITHUB_PAT:-}" ]]; then
      echo -e "  ${BOLD}Enter your GitHub PAT${NC} (fine-grained or classic with 'repo' scope)."
      echo    "  Or export GITHUB_PAT=... before running to skip this prompt."
      echo    "  Press Enter to skip and disable Execution Gateway for now."
      echo -n "  GitHub PAT: "
      read -r GITHUB_PAT
    else
      log "Using GITHUB_PAT from environment variable."
    fi
    if [[ -n "$GITHUB_PAT" ]]; then
      az keyvault secret set \
        --vault-name "$KV_NAME" \
        --name github-pat \
        --value "$GITHUB_PAT" \
        --output none
      ok "GitHub PAT stored in Key Vault: $KV_NAME"
      log "Restarting Container App to pick up the new secret..."
      az containerapp update \
        --name "$APP_NAME" \
        --resource-group "$RG_NAME" \
        --output none
      ok "Container App restarted"
    else
      warn "Skipping PAT — setting use_github_pat = false and re-applying Container App..."
      sed -i 's/^use_github_pat\s*=\s*true/use_github_pat = false/' terraform.tfvars
      terraform apply -auto-approve -target=azurerm_container_app.backend
      ok "Execution Gateway disabled — re-enable by setting use_github_pat = true and re-running terraform apply"
    fi
  fi
fi

# =============================================================================
# 5. Dashboard build + deploy
# =============================================================================
step "Building React dashboard"

log "Writing dashboard/.env.production..."
echo "VITE_API_URL=$BACKEND_URL" > "$DASHBOARD_DIR/.env.production"

cd "$DASHBOARD_DIR"
log "Installing dependencies..."
if [[ -f "package-lock.json" ]]; then
  npm ci --loglevel error
else
  npm install --loglevel error
fi

log "Building..."
npm run build
ok "Dashboard built"

step "Deploying dashboard to Azure Static Web Apps"

cd "$TF_DIR"
DEPLOY_TOKEN=$(terraform output -raw dashboard_deployment_token)
DASHBOARD_URL=$(terraform output -raw dashboard_url)

cd "$DASHBOARD_DIR"
npx @azure/static-web-apps-cli deploy ./dist \
  --deployment-token "$DEPLOY_TOKEN" \
  --env production
ok "Dashboard deployed: $DASHBOARD_URL"

# =============================================================================
# 6. Health check
# =============================================================================
step "Verifying deployment"

log "Checking backend health (up to 3 attempts — Container App may need a cold start)..."
HEALTH_URL="$BACKEND_URL/health"
# Note: the /health endpoint is at the root, not under /api.
HEALTH="unreachable after 3 attempts — check Container App logs"
for attempt in 1 2 3; do
  RESULT=$("$PYTHON" -c "
import urllib.request, json
try:
    with urllib.request.urlopen('$HEALTH_URL', timeout=15) as r:
        print(json.load(r).get('status', '?'))
except Exception:
    pass
" 2>/dev/null || true)
  if [[ -n "$RESULT" ]]; then
    HEALTH="$RESULT"
    break
  fi
  if [[ "$attempt" -lt 3 ]]; then
    warn "Attempt $attempt failed — retrying in 15s (Container App cold start)..."
    sleep 15
  fi
done
if [[ "$HEALTH" == "ok" ]]; then
  ok "Backend health: ok"
else
  warn "Backend health: $HEALTH"
  warn "The infrastructure is deployed. The Container App may still be starting."
  warn "Check logs: az containerapp logs show --name \$(terraform -chdir=$TF_DIR output -raw backend_container_app_name) --resource-group $RG_NAME --follow"
fi

# =============================================================================
# 7. Summary
# =============================================================================
KV_NAME=${KV_NAME:-"ruriskry-kv-<suffix>"}

echo ""
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✅ RuriSkry deployed successfully!${NC}"
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Dashboard  →  ${BOLD}$DASHBOARD_URL${NC}"
echo -e "  Backend    →  ${BOLD}$BACKEND_URL${NC}"
echo ""
echo "  Next steps:"
echo "  1. Generate local .env for development:"
echo "       cd $REPO_ROOT && bash scripts/setup_env.sh"
echo ""
echo "  2. Run test suite (mock mode — no Azure credentials needed):"
echo "       cd $REPO_ROOT && python -m pytest tests/ -q"
echo ""
echo "  3. (Optional) Set up Slack notifications:"
echo "       Guide: docs/slack-setup.md"
echo "       Short: api.slack.com/apps → Create App → Incoming Webhooks → copy URL"
echo "       Then:  add slack_webhook_url = \"...\" to terraform.tfvars and re-run terraform apply"
echo ""
echo "  (GitHub PAT was handled during deploy. To rotate it later:"
echo "   az keyvault secret set --vault-name $KV_NAME --name github-pat --value 'github_pat_xxx')"
echo ""
echo "  For subsequent code or infra changes, see:"
echo "  infrastructure/terraform-core/deploy.md § Redeploy Workflows"
echo ""

# =============================================================================
# 8. Wire demo environment to this backend (if deployed)
# =============================================================================
# If the user has also deployed infrastructure/terraform-demo, this step
# injects the backend URL into the demo environment's Azure Monitor action
# group so alerts flow into RuriSkry automatically.
#
# Skipped if:
#   - terraform-demo/terraform.tfvars doesn't exist (demo not set up yet)
#   - terraform-demo/.terraform doesn't exist (demo not initialised/applied)
#   - alert_webhook_url is already set in demo terraform.tfvars

DEMO_DIR="$REPO_ROOT/infrastructure/terraform-demo"

if [[ -f "$DEMO_DIR/terraform.tfvars" && -d "$DEMO_DIR/.terraform" ]]; then
  step "Wiring demo environment to RuriSkry backend"

  CURRENT_WEBHOOK=$(grep -E '^alert_webhook_url\s*=' "$DEMO_DIR/terraform.tfvars" 2>/dev/null \
    | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]' || echo "")

  if [[ -z "$CURRENT_WEBHOOK" ]]; then
    log "Injecting alert_webhook_url = $BACKEND_URL/api/alert-trigger"

    # Replace existing line if present, otherwise append
    if grep -qE '^alert_webhook_url\s*=' "$DEMO_DIR/terraform.tfvars"; then
      sed -i "s|^alert_webhook_url\s*=.*|alert_webhook_url = \"$BACKEND_URL/api/alert-trigger\"|" \
        "$DEMO_DIR/terraform.tfvars"
    else
      printf '\nalert_webhook_url = "%s/api/alert-trigger"\n' "$BACKEND_URL" \
        >> "$DEMO_DIR/terraform.tfvars"
    fi

    ok "alert_webhook_url injected into terraform-demo/terraform.tfvars"
    warn "terraform-demo is managed separately — apply it yourself to activate the wiring:"
    warn "  cd $DEMO_DIR && terraform apply -auto-approve -target=azurerm_monitor_action_group.prod"
  else
    ok "Demo environment already wired ($CURRENT_WEBHOOK) — no changes needed"
  fi
else
  if [[ ! -f "$DEMO_DIR/terraform.tfvars" ]]; then
    log "Demo environment not configured — skipping webhook wiring."
    log "To wire it later: set alert_webhook_url = \"$BACKEND_URL/api/alert-trigger\" in terraform-demo/terraform.tfvars and run terraform apply"
  else
    log "Demo environment not initialised — run 'terraform init && terraform apply' in terraform-demo first."
  fi
fi

# =============================================================================
# 9. Alert Processing Rule — owned by Terraform, no manual wiring needed
# =============================================================================
# The APR (azurerm_monitor_alert_processing_rule_action_group) is created by
# Terraform in Stage 1. It is tied to no personal identity — it is an Azure
# resource owned by Terraform state. Staff changes, subscription ownership
# transfers, and re-deploys do not affect it.
#
# If terraform apply failed at the APR resource (AuthorizationFailed), the
# deploying identity lacked Monitoring Contributor on the target subscription.
# Grant it once and re-run: terraform apply -target=azurerm_monitor_alert_processing_rule_action_group.ruriskry
step "Checking alert processing rule status"

APR_NAME="apr-ruriskry-governance-fanout"
# APR lives in the monitor RG in the TARGET subscription (not the infra RG)
APR_RG=$(terraform -chdir="$TF_DIR" output -raw monitor_resource_group_name 2>/dev/null || true)
TARGET_SUB=$(grep -E '^target_subscription_id\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]' || true)
ALERT_SUB="${TARGET_SUB:-$SUBSCRIPTION_ID}"

APR_STATE=$(az monitor alert-processing-rule show \
  --name "$APR_NAME" \
  --resource-group "$APR_RG" \
  --subscription "$ALERT_SUB" \
  --query "name" -o tsv 2>/dev/null || true)

if [[ -n "$APR_STATE" ]]; then
  ok "Alert Processing Rule in place: $APR_NAME"
  ok "All alerts in sub $ALERT_SUB route to RuriSkry automatically (managed by Terraform)."
else
  warn "Alert Processing Rule not found — Terraform may have lacked Monitoring Contributor on $ALERT_SUB."
  warn "Grant it to the deploying identity and re-run:"
  warn "  terraform apply -chdir=$TF_DIR \\"
  warn "    -target=azurerm_resource_group.ruriskry_monitor \\"
  warn "    -target=azurerm_monitor_alert_processing_rule_action_group.ruriskry"
fi
