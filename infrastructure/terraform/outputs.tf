output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "attestation_uri" {
  description = "Paste into policies/skr-policy.json as the `authority`."
  value       = module.key_management.attestation_uri
}

output "managed_hsm_uri" {
  description = "Empty when deploy_managed_hsm is false."
  value       = module.key_management.managed_hsm_uri
}

# Stated as an output rather than left to be inferred from an empty URI. A
# deployment that attests but cannot enforce key release is a legitimate and much
# cheaper configuration, and the one thing worse than running it is running it
# without knowing which of the two you have.
output "key_release_enforced" {
  description = "True only when the Managed HSM exists. False means attestation is real and key release is not."
  value       = module.key_management.managed_hsm_deployed
}

output "planned_tenant_keys" {
  description = "Create these once the HSM has been activated with its security domain."
  value       = module.key_management.planned_tenant_keys
}

output "postgres_fqdn" {
  value = module.data.postgres_fqdn
}

output "storage_account_name" {
  value = module.data.storage_account_name
}

output "pipeline_private_ip" {
  value = module.confidential_compute.private_ip
}

# The environment the platform would need, assembled from what this configuration
# creates. Printed rather than written to a file: it names an FQDN and a hostname,
# and `terraform output` is already the place people look.
output "platform_environment" {
  description = "Settings the pipeline needs. The passphrase and password are not here."
  value = {
    CONFIDENTIA_ENV         = var.environment
    TEE_BACKEND             = "snp-cvm"
    TEE_REQUIRE_ATTESTATION = "true"
    POSTGRES_HOST           = module.data.postgres_fqdn
    POSTGRES_USER           = "confidentia"
    STORAGE_BACKEND         = "adls"
    STORAGE_ACCOUNT         = module.data.storage_account_name
    CRYPTO_ATTESTATION_URI  = module.key_management.attestation_uri
    CRYPTO_HSM_URI          = module.key_management.managed_hsm_uri
  }
}

output "postgres_password" {
  value     = module.data.postgres_password
  sensitive = true
}
