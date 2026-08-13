variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "confidentia"

  validation {
    # Storage account names are 3-24 characters, lowercase alphanumeric only, and
    # globally unique. The prefix propagates into one, so it is constrained here
    # rather than failing 40 resources into an apply.
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.project))
    error_message = "project must be 3-12 lowercase alphanumeric characters, starting with a letter."
  }
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "stage", "prod"], var.environment)
    error_message = "environment must be dev, stage or prod."
  }
}

variable "location" {
  description = "Azure region. Must offer confidential VMs and Azure Attestation."
  type        = string
  default     = "northeurope"

  validation {
    # Not every region offers DCasv5/ECasv5 or an attestation provider, and the
    # failure when one does not is a quota error deep in an apply rather than a
    # clear message. This list is the intersection at the time of writing and is
    # checked rather than assumed -- see README.md for how to re-check it.
    condition = contains([
      "eastus", "eastus2", "westus2", "westeurope", "northeurope",
      "southeastasia", "centralindia", "uksouth",
    ], var.location)
    error_message = "location must be a region offering confidential compute and attestation."
  }
}

variable "tenant_names" {
  description = "Data tenants. Each gets its own key and its own release policy."
  type        = list(string)
  default     = ["healthcare", "financial"]
}

variable "confidential_vm_size" {
  description = "Confidential VM SKU. DCa = AMD SEV-SNP, matching TEE_BACKEND=snp-cvm."
  type        = string
  default     = "Standard_DC4as_v5"

  validation {
    # DC*as_v5 and EC*as_v5 are the AMD SEV-SNP families. A general-purpose size
    # would deploy successfully and provide no enclave at all -- the failure this
    # whole project is about would become invisible.
    condition     = can(regex("^Standard_[DE]C[0-9]+a[sd]*_v5$", var.confidential_vm_size))
    error_message = "confidential_vm_size must be a DCas_v5 or ECas_v5 SKU (AMD SEV-SNP)."
  }
}

variable "postgres_sku" {
  description = "PostgreSQL Flexible Server SKU."
  type        = string
  default     = "GP_Standard_D2ds_v4"
}

variable "postgres_version" {
  description = "PostgreSQL major version. Must match the development host."
  type        = string
  default     = "16"
}

variable "admin_object_ids" {
  description = <<-EOT
    Entra object IDs permitted to administer the Managed HSM.

    Empty by default and deliberately not defaulted to the current principal: an
    HSM whose administrators are whoever ran `terraform apply` is an HSM with an
    accidental administrator.
  EOT
  type        = list(string)
  default     = []
}

variable "deploy_managed_hsm" {
  description = <<-EOT
    Create the Managed HSM that holds every tenant's wrapping key.

    **This is the one resource here that cannot be undone in an afternoon.** Purge
    protection is required for it and cannot be disabled after creation, so
    `terraform destroy` moves the HSM to a soft-deleted state that persists — and
    continues to be billed — for the retention period. It is also the most
    expensive resource in the configuration by a wide margin, billed per hour
    whether or not a key is ever released.

    False gives an environment that answers the question the dissertation actually
    needs answered: a real SEV-SNP measurement, a real MAA token, and the enclave
    overhead that is currently MODELLED. None of that needs the HSM. What is lost
    is enforcement — the workload attests, but key release stays a policy the
    application honours rather than one the hardware imposes.

    Set true when the money and the retention window are both acceptable, and
    read the resource comment in modules/key_management first.
  EOT
  type        = bool
  default     = false
}

variable "admin_ssh_public_key" {
  description = <<-EOT
    OpenSSH public key for the operator account on the confidential VM.

    No default. Reading it with `file("~/.ssh/id_rsa.pub")` was the first attempt
    and it fails `terraform validate`, because the function does not expand `~`
    and requires the file to exist at validation time -- so the configuration only
    validated on a machine that happened to have that key.
  EOT
  type        = string
}

variable "allowed_ingress_cidrs" {
  description = "CIDRs permitted to reach the API. Empty means Azure services only."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
