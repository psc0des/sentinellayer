# =============================================================================
# SentinelLayer — Mini Production Environment — Input Variables
# =============================================================================
# Copy terraform.tfvars.example → terraform.tfvars and fill in your values.
# terraform.tfvars is in .gitignore — never commit it.
# =============================================================================

variable "subscription_id" {
  description = "Azure Subscription ID. Run: az account show --query id -o tsv"
  type        = string
}

variable "location" {
  description = "Azure region for all prod resources. Must match or be compatible with the main sentinel-layer-rg region."
  type        = string
  default     = "eastus"
}

variable "suffix" {
  description = <<-EOT
    Short unique suffix for globally-unique resource names (storage account, App Service).
    Rules: lowercase letters and digits only, max 8 characters.
    Example: "abc1234" makes storage account "sentinelprodabc1234"
    and App Service "payment-api-prod-abc1234".
    Tip: use your initials + 4 random digits.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{1,8}$", var.suffix))
    error_message = "suffix must be 1-8 lowercase letters and digits only (no dashes)."
  }
}

variable "vm_admin_username" {
  description = "Local administrator username for both VMs. Cannot be 'admin', 'administrator', or 'root'."
  type        = string
  default     = "sentineladmin"
}

variable "vm_admin_password" {
  description = <<-EOT
    Local administrator password for both VMs.
    Must be 12+ characters with uppercase, lowercase, digit, and special character.
    Stored only in terraform.tfvars (gitignored) — never committed.
  EOT
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.vm_admin_password) >= 12
    error_message = "vm_admin_password must be at least 12 characters."
  }
}

variable "alert_email" {
  description = "Email address for Azure Monitor alert notifications (CPU + heartbeat alerts)."
  type        = string
  default     = "ops-team@example.com"
}

variable "allowed_source_cidr_override" {
  description = "Optional CIDR override for NSG HTTP/HTTPS allow rules (example: 203.0.113.10/32). If empty, Terraform auto-detects your current public IP."
  type        = string
  default     = ""

  validation {
    condition = (
      var.allowed_source_cidr_override == "" ||
      can(cidrhost(var.allowed_source_cidr_override, 0))
    )
    error_message = "allowed_source_cidr_override must be empty or a valid CIDR (for example 203.0.113.10/32)."
  }
}
