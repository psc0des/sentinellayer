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

# step "title" prints "══ Step N/TOTAL — title ══" so the user always knows
# how far through the run we are. STEP_NUM auto-increments; TOTAL_STEPS is
# set once at the top of the script after parsing flags.
STEP_NUM=0
# Total steps depends on flags:
#   default      = 6 (Prereq, Main RG, Monitor RG, Foundry, Key Vault, Local state)
#   --all adds 1 = 7 (Prereq, Main RG, Monitor RG, Foundry, Key Vault, tfstate storage, Local state)
TOTAL_STEPS=$([[ "$DELETE_TFSTATE" == true ]] && echo 7 || echo 6)
step() {
  STEP_NUM=$((STEP_NUM + 1))
  echo ""
  echo -e "${BOLD}${BLUE}══ Step ${STEP_NUM}/${TOTAL_STEPS} — $* ══${NC}"
}

trap 'echo -e "\n${RED}${BOLD}✗  Unexpected error on line $LINENO${NC}" >&2
      echo -e "${RED}   Failed command: $BASH_COMMAND${NC}" >&2
      echo -e "${RED}   Re-run with: bash -x scripts/cleanup.sh  (for full trace)${NC}" >&2' ERR

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

# target_subscription_id is optional — empty means same-subscription deployment
TARGET_SUBSCRIPTION_ID=$(grep -E '^target_subscription_id\s*=' "$TF_DIR/terraform.tfvars" 2>/dev/null \
  | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr -d '[:space:]' || true)
TARGET_SUBSCRIPTION_ID=${TARGET_SUBSCRIPTION_ID:-$SUBSCRIPTION_ID}

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
MONITOR_RG="ruriskry-monitor-rg-${SUFFIX}"
TFSTATE_RG="ruriskry-tfstate-rg"
TFSTATE_SA="ruriskrytf${SUFFIX}"

echo ""
echo -e "${BOLD}Operating on:${NC}"
echo "  Subscription        : $SUBSCRIPTION_ID"
if [[ "$TARGET_SUBSCRIPTION_ID" != "$SUBSCRIPTION_ID" ]]; then
  echo "  Target subscription : $TARGET_SUBSCRIPTION_ID  (monitor RG lives here)"
fi
echo ""
echo -e "${BOLD}Resources to delete / purge:${NC}"
echo "  Resource group      : $RESOURCE_GROUP  (subscription: $SUBSCRIPTION_ID)"
echo "  Monitor RG          : $MONITOR_RG  (subscription: $TARGET_SUBSCRIPTION_ID)"
echo "  Foundry account     : $FOUNDRY_ACCOUNT_NAME  (purge soft-delete if present)"
echo "  Key Vault           : $KV_NAME  (purge soft-delete if present)"
if [[ "$DELETE_TFSTATE" == true ]]; then
  echo "  tfstate RG       : $TFSTATE_RG"
  echo "  tfstate SA       : $TFSTATE_SA"
else
  echo ""
  echo -e "${BOLD}Kept (not deleted):${NC}"
  echo "  tfstate storage  : $TFSTATE_SA  (use --all to also wipe this)"
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

  # Capture initial resource count so the heartbeat can show real progress
  # (e.g. "47 of 56 resources remaining") instead of just elapsed time. The
  # baseline is taken AFTER az group delete returns — by then Azure has
  # marked some resources for deletion but most are still listable.
  INITIAL_COUNT=$(az resource list --resource-group "$RESOURCE_GROUP" \
    --subscription "$SUBSCRIPTION_ID" --query "length(@)" -o tsv 2>/dev/null || echo "?")

  # Poll every 15s with an in-place heartbeat showing elapsed time AND the
  # number of resources still in the RG. \r and \033[K (clear-to-EOL) keep
  # the line stable instead of spamming the terminal. If output is piped
  # (CI, tee), the carriage returns still produce readable logs.
  START_TIME=$(date +%s)
  for i in $(seq 1 40); do
    if ! az group show --name "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
      ELAPSED=$(( $(date +%s) - START_TIME ))
      printf "\r\033[K"
      ok "Resource group deleted (took ${ELAPSED}s)."
      break
    fi
    ELAPSED=$(( $(date +%s) - START_TIME ))
    REMAINING=$(az resource list --resource-group "$RESOURCE_GROUP" \
      --subscription "$SUBSCRIPTION_ID" --query "length(@)" -o tsv 2>/dev/null || echo "?")
    printf "\r\033[K${BLUE}▶  Still deleting... %ds elapsed — %s of %s resources remaining${NC}" \
      "$ELAPSED" "$REMAINING" "$INITIAL_COUNT"
    if [[ "$i" -eq 40 ]]; then
      printf "\n"
      warn "Resource group still deleting after 10 minutes. Continuing anyway."
      warn "Check status: az group show --name $RESOURCE_GROUP --subscription $SUBSCRIPTION_ID"
    fi
    sleep 15
  done
else
  ok "Resource group '$RESOURCE_GROUP' does not exist — nothing to delete."
fi

# =============================================================================
# 3. Delete monitor resource group in target subscription
# =============================================================================
# Terraform creates ruriskry-monitor-rg-<suffix> in the TARGET subscription
# (azurerm.target provider) to host the Alert Processing Rule.
# cleanup.sh must delete it separately — it is never inside the main RG.
step "Deleting monitor resource group in target subscription"

if az group show --name "$MONITOR_RG" --subscription "$TARGET_SUBSCRIPTION_ID" &>/dev/null; then
  log "Deleting resource group: $MONITOR_RG (subscription: $TARGET_SUBSCRIPTION_ID)..."
  az group delete \
    --name "$MONITOR_RG" \
    --subscription "$TARGET_SUBSCRIPTION_ID" \
    --yes \
    --no-wait

  # Monitor RG is small (APR + action group); typically <30s. Same heartbeat
  # pattern as the main RG — shows resources remaining for real progress.
  INITIAL_COUNT=$(az resource list --resource-group "$MONITOR_RG" \
    --subscription "$TARGET_SUBSCRIPTION_ID" --query "length(@)" -o tsv 2>/dev/null || echo "?")
  START_TIME=$(date +%s)
  for i in $(seq 1 20); do
    if ! az group show --name "$MONITOR_RG" --subscription "$TARGET_SUBSCRIPTION_ID" &>/dev/null; then
      ELAPSED=$(( $(date +%s) - START_TIME ))
      printf "\r\033[K"
      ok "Monitor resource group deleted (took ${ELAPSED}s)."
      break
    fi
    ELAPSED=$(( $(date +%s) - START_TIME ))
    REMAINING=$(az resource list --resource-group "$MONITOR_RG" \
      --subscription "$TARGET_SUBSCRIPTION_ID" --query "length(@)" -o tsv 2>/dev/null || echo "?")
    printf "\r\033[K${BLUE}▶  Still deleting... %ds elapsed — %s of %s resources remaining${NC}" \
      "$ELAPSED" "$REMAINING" "$INITIAL_COUNT"
    if [[ "$i" -eq 20 ]]; then
      printf "\n"
      warn "Monitor resource group still deleting after 5 minutes. Continuing anyway."
    fi
    sleep 15
  done
else
  ok "Monitor resource group '$MONITOR_RG' does not exist in $TARGET_SUBSCRIPTION_ID — nothing to delete."
fi

# =============================================================================
# 5. Purge soft-deleted Cognitive Services / Foundry account
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
# 6. Delete tfstate storage (only with --all)
# =============================================================================
if [[ "$DELETE_TFSTATE" == true ]]; then
  step "Deleting Terraform remote state"

  if az group show --name "$TFSTATE_RG" --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
    log "Deleting tfstate resource group: $TFSTATE_RG"
    az group delete \
      --name "$TFSTATE_RG" \
      --subscription "$SUBSCRIPTION_ID" \
      --yes \
      --no-wait

    # tfstate RG holds one storage account; usually <30s.
    INITIAL_COUNT=$(az resource list --resource-group "$TFSTATE_RG" \
      --subscription "$SUBSCRIPTION_ID" --query "length(@)" -o tsv 2>/dev/null || echo "?")
    START_TIME=$(date +%s)
    for i in $(seq 1 20); do
      if ! az group show --name "$TFSTATE_RG" --subscription "$SUBSCRIPTION_ID" &>/dev/null; then
        ELAPSED=$(( $(date +%s) - START_TIME ))
        printf "\r\033[K"
        ok "tfstate resource group deleted (took ${ELAPSED}s)."
        break
      fi
      ELAPSED=$(( $(date +%s) - START_TIME ))
      REMAINING=$(az resource list --resource-group "$TFSTATE_RG" \
        --subscription "$SUBSCRIPTION_ID" --query "length(@)" -o tsv 2>/dev/null || echo "?")
      printf "\r\033[K${BLUE}▶  Still deleting... %ds elapsed — %s of %s resources remaining${NC}" \
        "$ELAPSED" "$REMAINING" "$INITIAL_COUNT"
      if [[ "$i" -eq 20 ]]; then
        printf "\n"
        warn "tfstate resource group still deleting after 5 minutes. Continuing anyway."
      fi
      sleep 15
    done
  else
    ok "tfstate resource group '$TFSTATE_RG' does not exist."
  fi
fi

# =============================================================================
# 7. Clean up local Terraform state
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
