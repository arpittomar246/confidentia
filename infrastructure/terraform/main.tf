# CONFIDENTIA on Azure.
#
# **Never applied.** There is no Azure subscription behind this project. What has
# been checked is `terraform init`, `validate`, `fmt` and a Checkov policy scan --
# so the configuration is syntactically valid against the real azurerm provider
# schema and passes static security policy. Whether it *deploys* is unverified, and
# `terraform plan` has never run because plan requires credentials.
#
# That distinction is the same one the platform makes everywhere else: a modelled
# result is labelled, and this is labelled here rather than in a footnote.
#
# The architecture exists to answer one question the rest of the project cannot:
# **what would replace each simulated component if the enclave were real?**
# See docs/architecture/azure.md for the component-by-component mapping.

locals {
  name = "${var.project}-${var.environment}"

  tags = merge(
    {
      project     = var.project
      environment = var.environment
      managed_by  = "terraform"
      # The platform labels every modelled number MODELLED. Infrastructure that has
      # never been applied gets the same treatment, in a tag that survives into the
      # portal rather than living only in a comment.
      provenance = "never-applied"
    },
    var.tags,
  )
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

module "network" {
  source = "./modules/network"

  name                = local.name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  allowed_ingress     = var.allowed_ingress_cidrs
  tags                = local.tags
}

# Key management first: the confidential compute module needs the attestation
# provider's URI to bind its key release policy to, and the data module needs the
# HSM to hold the storage encryption key.
module "key_management" {
  source = "./modules/key_management"

  name                = local.name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tenant_names        = var.tenant_names
  admin_object_ids    = var.admin_object_ids
  deploy_managed_hsm  = var.deploy_managed_hsm
  tags                = local.tags
}

module "data" {
  source = "./modules/data"

  name                = local.name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  subnet_id           = module.network.data_subnet_id
  private_dns_zone_id = module.network.postgres_dns_zone_id
  postgres_sku        = var.postgres_sku
  postgres_version    = var.postgres_version
  tags                = local.tags
}

module "confidential_compute" {
  source = "./modules/confidential_compute"

  name                 = local.name
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  subnet_id            = module.network.compute_subnet_id
  vm_size              = var.confidential_vm_size
  attestation_uri      = module.key_management.attestation_uri
  managed_hsm_uri      = module.key_management.managed_hsm_uri
  admin_ssh_public_key = var.admin_ssh_public_key
  tags                 = local.tags
}
