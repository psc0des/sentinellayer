#!/usr/bin/env bash
# =============================================================================
# deploy.sh — First-time deploy for RuriSkry
# =============================================================================
# Provisions all Azure infrastructure and deploys both the FastAPI backend
# and React dashboard in the correct order.
#
# Usage (from repo root):
#   bash scripts/deploy.sh           # full first-time deploy
#   bash scripts/deploy.sh --stage2  # skip ACR/Docker, jump straight to full apply
#                                     # use this if Stage 1 succeeded but Stage 2 failed
#
# NOTE: This script is for FIRST-TIME deploys only.
# For subsequent code or infra changes, use the "Redeploy Workflows" section
# in infrastructure/terraform-core/deploy.md — those are faster and skip the
# 90-second role propagation wait and Docker rebuild.
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
for arg in "$@"; do
  case "$arg" in
    --stage2) STAGE2_ONLY=true ;;
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

docker info &>/dev/null \
  || die "Docker daemon not running. Start Docker Desktop and try again."

[[ -f "$TF_DIR/terraform.tfvars" ]] \
  || die "terraform.tfvars not found.\n   Run: cp $TF_DIR/terraform.tfvars.example $TF_DIR/terraform.tfvars\n   Then fill in subscription_id and suffix."

ok "All prerequisites satisfied"

# =============================================================================
# 1. Terraform init
# =============================================================================
step "Initialising Terraform"

cd "$TF_DIR"
terraform init -upgrade
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
  warn "Assuming ACR and Static Web App already exist with URL patched into tfvars."
  ACR_NAME=$(terraform output -raw acr_name)
  ACR_SERVER=$(terraform output -raw acr_login_server)
  DASHBOARD_URL=$(terraform output -raw dashboard_url)
else
  step "Stage 1 — Provisioning ACR, Managed Identity, role assignment, and Static Web App"
  # Why SWA is in Stage 1 (not Stage 2 with everything else):
  #   Azure SWA generates a random subdomain on creation. The Container App
  #   needs the exact SWA URL as its DASHBOARD_URL env var (for CORS and for
  #   Teams notification links). By creating the SWA here, before docker push
  #   and before the Container App exists, we can patch the real URL into
  #   terraform.tfvars so Stage 2 creates the Container App with the correct
  #   value already set — no re-apply needed, no stale CORS window.

  terraform apply -auto-approve \
    -target=azurerm_resource_group.ruriskry \
    -target=azurerm_container_registry.ruriskry \
    -target=azurerm_user_assigned_identity.acr_pull \
    -target=azurerm_role_assignment.acr_pull \
    -target=time_sleep.acr_role_propagation \
    -target=azurerm_static_web_app.dashboard

  ACR_NAME=$(terraform output -raw acr_name)
  ACR_SERVER=$(terraform output -raw acr_login_server)
  DASHBOARD_URL=$(terraform output -raw dashboard_url)
  ok "ACR ready: $ACR_SERVER (role assignment propagated)"

  # Patch the real dashboard URL into terraform.tfvars immediately.
  # Stage 2 will create the Container App with DASHBOARD_URL already correct —
  # no wire-back step, no re-apply after dashboard deploy.
  log "Patching dashboard_url into terraform.tfvars..."
  "$PYTHON" - <<PYEOF
import re, pathlib
tfvars = pathlib.Path("terraform.tfvars")
content = tfvars.read_text()
url = "$DASHBOARD_URL".rstrip("/")
updated = re.sub(
    r'^dashboard_url\s*=.*',
    f'dashboard_url = "{url}"',
    content,
    flags=re.MULTILINE,
)
tfvars.write_text(updated)
print(f"  dashboard_url = {url}")
PYEOF
  ok "terraform.tfvars patched: dashboard_url = $DASHBOARD_URL"

  # =============================================================================
  # 3. Docker build + push
  # =============================================================================
  # Check if image already exists — skip rebuild if re-running after a partial failure
  IMAGE_EXISTS=false
  if az acr repository show-tags \
       --name "$ACR_NAME" \
       --repository ruriskry-backend \
       --query "[?@=='latest']" \
       --output tsv 2>/dev/null | grep -q "latest"; then
    IMAGE_EXISTS=true
  fi

  if [[ "$IMAGE_EXISTS" == true ]]; then
    warn "Image ruriskry-backend:latest already exists in ACR — skipping Docker build."
    warn "To force a rebuild, delete the tag first:"
    warn "  az acr repository delete --name $ACR_NAME --image ruriskry-backend:latest --yes"
  else
    step "Building and pushing Docker image"

    cd "$REPO_ROOT"

    log "Logging in to ACR..."
    az acr login --name "$ACR_NAME"

    log "Building backend image..."
    docker build -t "$ACR_SERVER/ruriskry-backend:latest" .

    log "Pushing to ACR..."
    docker push "$ACR_SERVER/ruriskry-backend:latest"
    ok "Image pushed: $ACR_SERVER/ruriskry-backend:latest"
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
ok "Backend URL: $BACKEND_URL"

# =============================================================================
# 5. Dashboard build + deploy
# =============================================================================
step "Building React dashboard"

log "Writing dashboard/.env.production..."
echo "VITE_API_URL=$BACKEND_URL" > "$DASHBOARD_DIR/.env.production"

cd "$DASHBOARD_DIR"
log "Installing dependencies..."
npm install --silent

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
# 7. Health check
# =============================================================================
step "Verifying deployment"

log "Checking backend health..."
HEALTH=$("$PYTHON" -c "
import urllib.request, json, sys
try:
    with urllib.request.urlopen('$BACKEND_URL/health', timeout=15) as r:
        print(json.load(r).get('status', '?'))
except Exception as e:
    print('unreachable — may need 30s cold start')
" 2>/dev/null)
ok "Backend health: $HEALTH"

# =============================================================================
# 8. Summary
# =============================================================================
KV_NAME=$(terraform output -raw keyvault_name 2>/dev/null || echo "ruriskry-kv-<suffix>")

echo ""
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✅ RuriSkry deployed successfully!${NC}"
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Dashboard  →  ${BOLD}$DASHBOARD_URL${NC}"
echo -e "  Backend    →  ${BOLD}$BACKEND_URL${NC}"
echo ""
echo "  Remaining manual steps:"
echo "  1. Seed AI Search index:"
echo "       cd $REPO_ROOT && python scripts/seed_data.py"
echo ""
echo "  2. Store GitHub PAT in Key Vault (for Execution Gateway):"
echo "       az keyvault secret set --vault-name $KV_NAME --name github-pat --value 'github_pat_xxx'"
echo "       # Then set use_github_pat = true in terraform.tfvars and re-apply"
echo ""
echo "  3. Generate local .env for development:"
echo "       cd $REPO_ROOT && bash scripts/setup_env.sh"
echo ""
echo "  For subsequent code or infra changes, see:"
echo "  infrastructure/terraform-core/deploy.md § Redeploy Workflows"
echo ""
