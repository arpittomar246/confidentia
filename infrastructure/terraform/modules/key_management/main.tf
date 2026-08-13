# Key management — the real version of C2.
#
# This module is the point of the whole configuration. Everything else is ordinary
# cloud plumbing; this is where the platform's simulated attestation-gated key
# release becomes a thing the hardware enforces.
#
# The mapping, component for component:
#
#   confidentia/crypto/keys.py       ->  Managed HSM holds the master key
#   confidentia/crypto/attestation.py -> Microsoft Azure Attestation (MAA)
#   confidentia/crypto/broker.py     ->  the HSM's secure key release policy
#   crypto/java/LocalKmsClient.java  ->  a KMS client backed by Managed HSM
#
# The difference that matters: under `sim` the process measures itself and could
# lie, which is why every audit row carries `hardware_rooted = false`. Here the
# measurement is produced by the AMD SEV-SNP firmware, signed by AMD's key, and
# verified by MAA before the HSM will release anything. The refusal stops being a
# policy the application chooses to honour and becomes one it cannot bypass.

variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tenant_names" { type = list(string) }
variable "admin_object_ids" { type = list(string) }
variable "tags" { type = map(string) }

data "azurerm_client_config" "current" {}

# Microsoft Azure Attestation. The remote party that decides whether an SEV-SNP
# attestation report is genuine and describes an acceptable guest.
resource "azurerm_attestation_provider" "main" {
  name                = replace("att${var.name}", "-", "")
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

# Managed HSM, not Key Vault.
#
# Secure key release is a Managed HSM and Premium Key Vault feature, and the
# exportable-key-with-release-policy shape this design rests on needs the HSM. It
# is also the expensive choice -- Managed HSM bills per hour whether or not a key
# is used -- and that cost is the honest price of a key that a compromised host
# cannot read.
resource "azurerm_key_vault_managed_hardware_security_module" "main" {
  name                = substr(replace("hsm${var.name}", "-", ""), 0, 24)
  resource_group_name = var.resource_group_name
  location            = var.location
  sku_name            = "Standard_B1"
  tenant_id           = data.azurerm_client_config.current.tenant_id

  # The keys in here wrap every DEK in the platform. Losing them means every
  # encrypted table is unreadable forever -- there is no escrow, by design -- so
  # purge protection is on and cannot be turned off after creation.
  purge_protection_enabled   = true
  soft_delete_retention_days = 90

  # Deliberately not defaulted to the current principal. An HSM whose
  # administrators are whoever happened to run `terraform apply` is an HSM with an
  # accidental administrator.
  admin_object_ids = var.admin_object_ids

  # Public access disabled: the HSM is reachable only from the VNet. A key release
  # policy makes a key safe to hand to an attested workload; it does not make the
  # management plane safe to expose.
  public_network_access_enabled = false

  network_acls {
    bypass         = "AzureServices"
    default_action = "Deny"
  }

  tags = var.tags
}

# The identity the confidential workload runs as. It is granted the ability to
# *ask* for a key; the release policy decides whether asking succeeds.
resource "azurerm_user_assigned_identity" "workload" {
  name                = "id-workload-${var.name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

# One key per tenant, mirroring the platform's own hierarchy: tenant isolation is
# cryptographic rather than a filter, so two tenants never share a wrapping key.
#
# NOT created here. A Managed HSM must be *activated* with a security domain
# before any key operation succeeds, and activation requires downloading the
# security domain with a quorum of RSA key pairs held by humans. That is a
# deliberate manual step -- it is the moment the organisation takes custody of the
# root of trust -- and automating it in Terraform would defeat the reason it
# exists. `terraform apply` therefore stops here, and the keys are created
# afterwards with the policy in ../../policies/skr-policy.json:
#
#   az keyvault key create --hsm-name <hsm> --name confidentia-healthcare-kek \
#     --ops wrapKey unwrapKey --kty RSA-HSM --size 3072 --exportable true \
#     --policy @infrastructure/terraform/policies/skr-policy.json
#
# `--exportable` is what makes the key eligible for secure release at all, and it
# is only safe *because* the release policy is attached at creation and cannot be
# loosened afterwards without recreating the key.
locals {
  tenant_key_names = [for t in var.tenant_names : "${var.name}-${t}-kek"]
}

output "attestation_uri" {
  value       = azurerm_attestation_provider.main.attestation_uri
  description = "The authority a key release policy binds to."
}

output "managed_hsm_uri" {
  value = azurerm_key_vault_managed_hardware_security_module.main.hsm_uri
}

output "workload_identity_id" {
  value = azurerm_user_assigned_identity.workload.id
}

output "workload_principal_id" {
  value = azurerm_user_assigned_identity.workload.principal_id
}

output "planned_tenant_keys" {
  value       = local.tenant_key_names
  description = "Keys to create after the HSM is activated. See the comment above."
}
