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
#   2. Create remote state storage (one-time, if not done):
#        See infrastructure/terraform-core/deploy.md § "Create remote state storage"
#
#   3. Tools required: terraform, az (Azure CLI), docker, node, npm
#      Run `az login` before executing this script.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$REPO_ROOT/infrastructure/terraform-core"
DASHBOARD_DIR="$REPO_ROOT/dashboard"

# ── Flags ─────────────────────────────────────────────────────────────────────
STAGE2_ONLY=false
UPGRADE_PROVIDERS=false
for arg in "$@"; do
  case "$arg" in
    --stage2)            STAGE2_ONLY=true ;;
    --upgrade-providers) UPGRADE_PROVIDERS=true ;;
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

ok "All prerequisites satisfied"

# =============================================================================
# 1. Terraform init
# =============================================================================
step "Initialising Terraform"

cd "$TF_DIR"
if [[ "$UPGRADE_PROVIDERS" == true ]]; then
  terraform init -upgrade
else
  terraform init
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
# 4b. GitHub PAT — store in Key Vault if use_github_pat = true
# =============================================================================
# The Container App needs the github-pat KV secret to exist before it can start.
# We prompt here so the user doesn't have to do it manually after deployment.
USE_GITHUB_PAT=$(grep -E '^use_github_pat\s*=\s*true' terraform.tfvars 2>/dev/null | wc -l)
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
  npm ci --silent
else
  npm install --silent
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
" 2>/dev/null)
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
echo "       See docs/slack-setup.md"
echo ""
echo "  (GitHub PAT was handled during deploy. To rotate it later:"
echo "   az keyvault secret set --vault-name $KV_NAME --name github-pat --value 'github_pat_xxx')"
echo ""
echo "  For subsequent code or infra changes, see:"
echo "  infrastructure/terraform-core/deploy.md § Redeploy Workflows"
echo ""
