# Infrastructure Deployment Runbook

This file is the practical guide for deploying and testing SentinelLayer infrastructure from GitHub.

## Scope

This runbook covers:

1. Terraform deployment of Azure resources
2. Runtime `.env` generation in secure mode
3. Basic post-deploy validation
4. Update/redeploy workflow
5. Optional teardown for POC

Current design choice:

- Foundry account and model deployment are Terraform-managed.
- Foundry project is intentionally not Terraform-managed (`create_foundry_project=false`).

## What Gets Deployed

- Resource Group
- Azure AI Foundry account (`azurerm_ai_services`)
- Foundry model deployment (`azurerm_cognitive_deployment`, default: `gpt-41`)
- Azure AI Search
- Azure Cosmos DB (SQL API)
- Azure Key Vault
- Key Vault secrets for Foundry/Search/Cosmos keys
- Log Analytics workspace

## Prerequisites

- Azure subscription with required quotas
- Azure CLI installed and logged in (`az login`)
- Terraform 1.5+
- Python 3.11+

## 1) Clone And Prepare

From repo root:

```bash
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `infrastructure/terraform/terraform.tfvars`:

- `subscription_id`
- `suffix` (must be globally unique)
- keep `create_foundry_project=false`
- set `create_foundry_deployment=true` for Terraform-managed deployment

## 2) Deploy Infrastructure

```bash
cd infrastructure/terraform
terraform init
terraform validate
terraform plan -input=false
terraform apply -input=false
```

## 3) Generate Runtime .env

Back at repo root:

```bash
bash scripts/setup_env.sh
```

What this does:

- Writes endpoints and Key Vault secret names to `.env`
- Prompts for `AZURE_SUBSCRIPTION_ID` and `AZURE_TENANT_ID`
- Keeps plaintext keys out of `.env` by default

Optional local fallback:

```bash
bash scripts/setup_env.sh --include-keys
```

Non-interactive mode (CI):

```bash
bash scripts/setup_env.sh --no-prompt
```

## 4) Post-Deploy Validation

### 4.1 Terraform state

```bash
cd infrastructure/terraform
terraform state list
terraform output
```

### 4.2 Key Vault access check

From repo root:

```bash
set -a
source .env
set +a

KV_NAME=$(echo "$AZURE_KEYVAULT_URL" | sed -E 's#https://([^.]+)\.vault\.azure\.net/?#\1#')

az keyvault secret show --vault-name "$KV_NAME" --name "$AZURE_OPENAI_API_KEY_SECRET_NAME" --query id -o tsv
az keyvault secret show --vault-name "$KV_NAME" --name "$AZURE_SEARCH_API_KEY_SECRET_NAME" --query id -o tsv
az keyvault secret show --vault-name "$KV_NAME" --name "$COSMOS_KEY_SECRET_NAME" --query id -o tsv
```

### 4.3 Seed Search index

Brief note:
- `scripts/seed_data.py` creates/updates the Azure AI Search index schema `incident-history`.
- It uploads the 7 demo incident documents from `data/seed_incidents.json`.
- This is needed so `HistoricalPatternAgent` can run live similarity/history checks in Azure mode; without this seed, live search returns no useful historical incidents.

```bash
python scripts/seed_data.py
```

Expected:

- Index `incident-history` created/updated
- `Uploaded 7/7 incidents`

### 4.4 Test infrastructure clients

```bash
python -m pytest tests/test_infrastructure_clients.py -v
```

## 5) Redeploy After Changes

```bash
cd infrastructure/terraform
terraform fmt
terraform validate
terraform plan -input=false
terraform apply -input=false
cd ../..
bash scripts/setup_env.sh
```

Re-run:

```bash
python scripts/seed_data.py
python -m pytest tests/test_infrastructure_clients.py -v
```

## 6) Optional Teardown (POC)

```bash
cd infrastructure/terraform
terraform destroy -input=false
```

Use teardown carefully; this deletes all Terraform-managed cloud resources in this stack.
