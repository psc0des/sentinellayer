#!/usr/bin/env bash
# =============================================================================
# cleanup.sh — Wipe all Azure resources from a previous (partial) RuriSkry deploy
# =============================================================================
# Run this before a fresh deploy to ensure no soft-deleted resources,
# leftover resource groups, or stale local Terraform state block a retry.
#
# Usage:
#   bash scripts/cleanup.sh           # delete RG + purge soft-deleted; keep tfstate
#   bash scripts/cleanup.sh --all     # also delete tfstate storage (full reset)
#
# After this script completes, do a fresh deploy with:
#   bash scripts/deploy.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$REPO_ROOT/infrastructure/terraform-core"

# ── Flags ─────────────────────────────────────────────────────────────────────
DELETE_TFSTATE=false
for arg in "$@"; do
  case "$arg" in
    --all) DELETE_TFSTATE=true ;;
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
# 1. Prerequisites
# =============================================================================
step "Prerequisites"

command -v az &>/dev/null || die "Azure CLI not found."
az account show &>/dev/null  || die "Not logged in. Run: az login"

[[ -f "$TF_DIR/terraform.tfvars" ]] \
  || die "terraform.tfvars not found at $TF_DIR/terraform.tfvars\n   Nothing to clean up — no deploy was attempted with this config."

# ── Read values from terraform.tfvars ────────────────────────────────────────
SUFFIX=$(grep -E '^suffix\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
[[ -n "$SUFFIX" && "$SUFFIX" != "replace-me" ]] \
  || die "suffix is not set in terraform.tfvars — cannot derive resource names."

SUBSCRIPTION_ID=$(grep -E '^subscription_id\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
[[ -n "$SUBSCRIPTION_ID" ]] || die "subscription_id is not set in terraform.tfvars."

az account set --subscription "$SUBSCRIPTION_ID" \
  || die "Could not select subscription $SUBSCRIPTION_ID. Run: az login"
ok "Active subscription: $SUBSCRIPTION_ID"

ENV_VAL=$(grep -E '^env\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
ENV_VAL=${ENV_VAL:-core}

# Derive resource names the same way Terraform + deploy.sh do
RESOURCE_GROUP=$(grep -E '^resource_group_name\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
RESOURCE_GROUP=${RESOURCE_GROUP:-"ruriskry-${ENV_VAL}-engine-rg"}

FOUNDRY_ACCOUNT_NAME=$(grep -E '^foundry_account_name\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
FOUNDRY_ACCOUNT_NAME=${FOUNDRY_ACCOUNT_NAME:-"ruriskry-${ENV_VAL}-foundry-${SUFFIX}"}

FOUNDRY_LOCATION=$(grep -E '^foundry_location\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]')
FOUNDRY_LOCATION=${FOUNDRY_LOCATION:-eastus2}

KV_NAME="ruriskry-${ENV_VAL}-kv-${SUFFIX}"
TFSTATE_RG="ruriskry-tfstate-rg"
TFSTATE_SA="ruriskrytf${SUFFIX}"

echo ""
echo -e "${BOLD}Resources targeted:${NC}"
echo "  Subscription     : $SUBSCRIPTION_ID"
echo "  Resource group   : $RESOURCE_GROUP"
echo "  Foundry account  : $FOUNDRY_ACCOUNT_NAME  (purge soft-delete)"
echo "  Key Vault        : $KV_NAME  (purge soft-delete)"
if [[ "$DELETE_TFSTATE" == true ]]; then
  echo "  tfstate RG       : $TFSTATE_RG  (will be deleted)"
  echo "  tfstate SA       : $TFSTATE_SA  (will be deleted)"
else
  echo "  tfstate RG/SA    : kept (use --all to also delete)"
fi
echo ""
echo -e "${YELLOW}${BOLD}This will permanently delete the resources listed above.${NC}"
echo -e "Press ${BOLD}Enter${NC} to continue or ${BOLD}Ctrl+C${NC} to abort."
read -r _

# =============================================================================
# 2. Delete main resource group (async — Azure handles all child resources)
# =============================================================================
step "Deleting main resource group"

if az group show --name "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
  log "Deleting resource group: $RESOURCE_GROUP (this takes 2–5 minutes)..."
  az group delete \
    --name "$RESOURCE_GROUP" \
    --subscription "$SUBSCRIPTION_ID" \
    --yes \
    --no-wait
  ok "Deletion started — Azure is removing all child resources in the background."
  log "Waiting for deletion to complete..."
  # Poll every 15s until the RG is gone
  for i in $(seq 1 40); do
    if ! az group show --name "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
      ok "Resource group deleted."
      break
    fi
    if [[ "$i" -eq 40 ]]; then
      warn "Resource group still deleting after 10 minutes. Continuing anyway."
      warn "Check status: az group show --name $RESOURCE_GROUP --subscription $SUBSCRIPTION_ID"
    fi
    sleep 15
  done
else
  ok "Resource group '$RESOURCE_GROUP' does not exist — nothing to delete."
fi

# =============================================================================
# 3. Purge soft-deleted Cognitive Services / Foundry account
# =============================================================================
step "Purging soft-deleted Foundry account"

DELETED_FOUNDRY=$(az cognitiveservices account list-deleted \
  --subscription "$SUBSCRIPTION_ID" \
  --query "[?name=='${FOUNDRY_ACCOUNT_NAME}'].name" \
  -o tsv 2>/dev/null || true)

if [[ -n "$DELETED_FOUNDRY" ]]; then
  log "Purging: $FOUNDRY_ACCOUNT_NAME"
  az cognitiveservices account purge \
    --location "$FOUNDRY_LOCATION" \
    --resource-group "$RESOURCE_GROUP" \
    --name "$FOUNDRY_ACCOUNT_NAME" \
    --subscription "$SUBSCRIPTION_ID" 2>/dev/null || true
  ok "Foundry account purged."
else
  ok "No soft-deleted Foundry account found."
fi

# =============================================================================
# 4. Purge soft-deleted Key Vault
# =============================================================================
step "Purging soft-deleted Key Vault"

DELETED_KV=$(az keyvault list-deleted \
  --subscription "$SUBSCRIPTION_ID" \
  --query "[?name=='${KV_NAME}'].name" \
  -o tsv 2>/dev/null || true)

if [[ -n "$DELETED_KV" ]]; then
  log "Purging: $KV_NAME"
  az keyvault purge \
    --name "$KV_NAME" \
    --location "$FOUNDRY_LOCATION" \
    --subscription "$SUBSCRIPTION_ID" 2>/dev/null || true
  ok "Key Vault purged."
else
  ok "No soft-deleted Key Vault found."
fi

# =============================================================================
# 5. Delete tfstate storage (only with --all)
# =============================================================================
if [[ "$DELETE_TFSTATE" == true ]]; then
  step "Deleting Terraform remote state"

  if az group show --name "$TFSTATE_RG" --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
    log "Deleting tfstate resource group: $TFSTATE_RG"
    az group delete \
      --name "$TFSTATE_RG" \
      --subscription "$SUBSCRIPTION_ID" \
      --yes
    ok "tfstate resource group deleted."
  else
    ok "tfstate resource group '$TFSTATE_RG' does not exist."
  fi
fi

# =============================================================================
# 6. Clean up local Terraform state
# =============================================================================
step "Cleaning local Terraform state"

if [[ -f "$TF_DIR/backend.hcl" ]]; then
  rm -f "$TF_DIR/backend.hcl"
  ok "Removed backend.hcl"
else
  ok "No backend.hcl found."
fi

if [[ -d "$TF_DIR/.terraform" ]]; then
  rm -rf "$TF_DIR/.terraform"
  ok "Removed .terraform directory"
else
  ok "No .terraform directory found."
fi

if [[ -f "$TF_DIR/tfplan" ]]; then
  rm -f "$TF_DIR/tfplan"
  ok "Removed tfplan"
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║  Cleanup complete — ready for a fresh deploy             ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
if [[ "$DELETE_TFSTATE" == true ]]; then
  echo "  All Azure resources and local state have been wiped."
else
  echo "  Azure resources deleted. tfstate storage was kept."
  echo "  (deploy.sh will reuse it — no need to recreate.)"
fi
echo ""
echo "  Next step:"
echo "    bash scripts/deploy.sh"
echo ""
