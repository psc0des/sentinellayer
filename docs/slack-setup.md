# Slack Notifications Setup

RuriSkry sends real-time Slack alerts when the governance engine blocks or escalates an action, and when Azure Monitor alerts fire or resolve. This guide walks you through creating a Slack webhook and wiring it into the project.

---

## Prerequisites

- A Slack workspace where you have permission to install apps
- Terraform CLI installed (for production deploy)
- Access to `infrastructure/terraform-core/terraform.tfvars`

---

## Step 1: Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch**
3. Name it (e.g. `RuriSkry Alerts`)
4. Select your workspace
5. Click **Create App**

## Step 2: Enable Incoming Webhooks

1. In the app settings sidebar, click **Incoming Webhooks**
2. Toggle the switch to **On**
3. Click **Add New Webhook to Workspace** (bottom of page)
4. Choose the channel for notifications (e.g. `#ruriskry-alerts` — create it first if needed)
5. Click **Allow**
6. Copy the webhook URL. It looks like:
   ```
   https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX
   ```

> **Security note:** Anyone with this URL can post to your channel. Never commit it to source control. The production deploy stores it in Azure Key Vault.

## Step 3: Configure the Project

### Production (Terraform)

Add to `infrastructure/terraform-core/terraform.tfvars`:

```hcl
slack_webhook_url           = "https://hooks.slack.com/services/T.../B.../XXX..."
slack_notifications_enabled = true
```

Then apply:

```bash
cd infrastructure/terraform-core
terraform apply
```

Terraform stores the URL in Key Vault and injects it into the Container App as a secret reference — not a plain environment variable.

### Local Development

Add to your `.env` file at the repo root:

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../XXX...
SLACK_NOTIFICATIONS_ENABLED=true
```

Start the backend normally (`uvicorn src.api.dashboard_api:app`). Notifications will fire on the next DENIED/ESCALATED verdict or alert trigger.

## Step 4: Verify

Trigger a test notification from the dashboard:

1. Open the dashboard → **Scans** page
2. Run any agent scan (Deploy, Monitoring, or Cost)
3. If a verdict comes back as **DENIED** or **ESCALATED**, a Slack message should appear in your channel within a few seconds

Or use the API directly:

```bash
curl -X POST http://localhost:8000/api/test-notification
```

---

## What Gets Notified

| Event | Trigger | Sidebar Color |
|---|---|---|
| Verdict (DENIED / ESCALATED) | Governance pipeline blocks or escalates an action | Red / Amber |
| Alert fired | Azure Monitor alert triggers a background investigation | Red |
| Alert resolved | Alert investigation completes with findings | Green |

APPROVED verdicts do not send notifications — you only get pinged when something needs human attention.

---

## Disabling Notifications

To pause notifications without removing the webhook URL:

- **Production:** Set `slack_notifications_enabled = false` in `terraform.tfvars` and run `terraform apply`
- **Local:** Set `SLACK_NOTIFICATIONS_ENABLED=false` in `.env`

To fully remove: clear `slack_webhook_url` in tfvars (or `SLACK_WEBHOOK_URL` in `.env`) and re-apply.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No messages appearing | Check that `SLACK_NOTIFICATIONS_ENABLED=true` and `SLACK_WEBHOOK_URL` is set. Look for `Slack notification skipped` in backend logs. |
| `404` from Slack | The webhook URL is invalid or the app was deleted. Regenerate it in Slack app settings. |
| `403` / `invalid_token` | The webhook was revoked. Go to your Slack app → Incoming Webhooks → add a new one. |
| Messages appear but channel is wrong | Each webhook is tied to one channel. Add a new webhook for a different channel. |
| Works locally but not in production | Verify the Key Vault secret `slack-webhook-url` exists: `az keyvault secret show --vault-name <vault> --name slack-webhook-url`. Check Container App logs for errors. |
