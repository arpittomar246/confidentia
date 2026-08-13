# Data plane: the metadata store and the lake.

variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "subnet_id" { type = string }
variable "private_dns_zone_id" { type = string }
variable "postgres_sku" { type = string }
variable "postgres_version" { type = string }
variable "tags" { type = map(string) }

resource "random_password" "postgres" {
  length      = 32
  special     = true
  min_upper   = 2
  min_lower   = 2
  min_numeric = 2
  min_special = 2
  # `$` and backtick survive Terraform fine and then break a shell that
  # interpolates the password into a connection string.
  override_special = "!#%*()-_=+[]{}<>:?"
}

# The platform metadata store: runs, quality scores, the promotion gate, the
# lineage chain, the key release audit.
#
# Note what is *not* here: no tenant data. Gold publishes disclosure-controlled
# aggregates into a `gold` schema, and those are already k-anonymised. The row-level
# data never leaves the lake, which is why this server can be an ordinary managed
# PostgreSQL rather than something running inside an enclave.
resource "azurerm_postgresql_flexible_server" "main" {
  name                = "psql-${var.name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  version             = var.postgres_version

  administrator_login    = "confidentia"
  administrator_password = random_password.postgres.result

  sku_name   = var.postgres_sku
  storage_mb = 32768

  # VNet-integrated. `public_network_access_enabled` is not set alongside
  # `delegated_subnet_id` -- the provider rejects the combination, because a
  # server with a delegated subnet has no public endpoint by construction.
  delegated_subnet_id = var.subnet_id
  private_dns_zone_id = var.private_dns_zone_id

  backup_retention_days        = 14
  geo_redundant_backup_enabled = false

  # Zone-redundant HA needs a second zone and roughly doubles the bill. Off for a
  # dissertation deployment, and named so the omission is a decision.
  high_availability {
    mode = "SameZone"
  }

  tags = var.tags

  lifecycle {
    # Azure picks a zone at create time and reports it as a diff on every
    # subsequent plan.
    ignore_changes = [zone]
  }
}

# wal_level = logical, required by the CDC path.
#
# The ingestion layer consumes the write-ahead log directly rather than polling an
# `updated_at` column, so this is not tuning -- without it, `ingest cdc` cannot
# create a replication slot and Stage 3's exactly-once guarantee has nothing to
# stand on.
resource "azurerm_postgresql_flexible_server_configuration" "wal_level" {
  name      = "wal_level"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "logical"
}

resource "azurerm_postgresql_flexible_server_configuration" "max_replication_slots" {
  name      = "max_replication_slots"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "8"
}

resource "azurerm_postgresql_flexible_server_database" "platform" {
  name      = "confidentia"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_database" "oltp" {
  name      = "ehr_oltp"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# The lake. ADLS Gen2 -- `is_hns_enabled` gives real directories, which Delta's
# transaction log depends on for atomic rename.
resource "azurerm_storage_account" "lake" {
  name                = substr(replace("st${var.name}lake", "-", ""), 0, 24)
  resource_group_name = var.resource_group_name
  location            = var.location

  account_tier             = "Standard"
  account_replication_type = "ZRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true

  https_traffic_only_enabled    = true
  min_tls_version               = "TLS1_2"
  public_network_access_enabled = false

  # Both found by Checkov (CKV_AZURE_190, CKV2_AZURE_47, CKV_AZURE_244) and both
  # correct. An ADLS container is private by default, but "default" is not a
  # control -- and SFTP local users are a second credential system beside Entra,
  # which is exactly the kind of side door this platform should not have.
  allow_nested_items_to_be_public = false
  local_user_enabled              = false

  # Azure encrypts blobs at rest regardless. That is orthogonal to what this
  # platform does: Parquet Modular Encryption encrypts *columns*, with per-tenant
  # per-sensitivity keys the storage service never sees. Storage-side encryption
  # protects against a stolen disk; PME protects against the storage service
  # itself, which is the threat model this project is about.
  shared_access_key_enabled = false

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 14
    }
  }

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    virtual_network_subnet_ids = [var.subnet_id]
  }

  tags = var.tags
}

resource "azurerm_storage_container" "lake" {
  name                  = "lake"
  storage_account_id    = azurerm_storage_account.lake.id
  container_access_type = "private"
}

# Separate from the lake container, and this is the C5 finding made structural.
#
# The placement planner found that the landing zone holds *plaintext*: files are
# written as unencrypted Parquet before Bronze applies encryption, and one landing
# patient file contained 19 SSN-shaped strings. A separate container with a
# lifecycle rule that deletes after a day bounds the exposure. It does not remove
# it -- the honest fix is to encrypt landing or delete each batch once Bronze has
# durably committed, and neither is implemented (ADR 0017).
resource "azurerm_storage_container" "landing" {
  name                  = "landing"
  storage_account_id    = azurerm_storage_account.lake.id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "landing_expiry" {
  storage_account_id = azurerm_storage_account.lake.id

  rule {
    name    = "expire-landing-plaintext"
    enabled = true
    filters {
      prefix_match = ["landing/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        # Not `delete_after_creation_greater_than_days`, which is what the 3.x
        # provider called it and what `terraform validate` rejected here.
        delete_after_days_since_creation_greater_than = 1
      }
    }
  }
}

output "postgres_fqdn" { value = azurerm_postgresql_flexible_server.main.fqdn }
output "storage_account_name" { value = azurerm_storage_account.lake.name }

output "postgres_password" {
  value     = random_password.postgres.result
  sensitive = true
}
