# Development deployment.
#
# `admin_ssh_public_key` and `admin_object_ids` have no defaults and are not set
# here: one is a per-operator value and the other decides who administers the HSM
# that holds every wrapping key. Supply them on the command line, or in a
# `*.auto.tfvars` file that is gitignored.
#
#   terraform apply -var-file=environments/dev.tfvars \
#     -var="admin_ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
#     -var='admin_object_ids=["<your-entra-object-id>"]'

# **This file deploys the Managed HSM.** Purge protection is required for it and
# cannot be disabled after creation, so `terraform destroy` soft-deletes rather
# than removes it, and it bills through the retention period. Use
# `environments/measure.tfvars` for a deployment you can tear down the same day —
# it gives the real SEV-SNP measurement this project needs and leaves out the one
# resource that is a commitment. See docs/deploy.md.

project     = "confidentia"
environment = "dev"

# The full C2 architecture: key release the hardware enforces, not a policy the
# application honours. Read the resource comment in modules/key_management first.
deploy_managed_hsm = true

# North Europe offers DCasv5 confidential VMs and an attestation provider, and is
# the closest such region to where this project is being written. Availability
# varies by subscription and quota -- see README.md for how to check before
# planning.
location = "northeurope"

tenant_names = ["healthcare", "financial"]

# 4 vCPU, 16 GB. The pipeline's Spark driver is configured for 2 GB and the
# development machine runs `local[2]`, so this is the same shape with room for the
# enclave's own overhead rather than a scale-up.
confidential_vm_size = "Standard_DC4as_v5"

postgres_sku     = "GP_Standard_D2ds_v4"
postgres_version = "16"

# Empty: nothing reaches the confidential VM from the internet. Operators use
# Azure Bastion. Adding a CIDR here opens 443 on the compute NSG and should be a
# reviewed change, not a convenience.
allowed_ingress_cidrs = []

tags = {
  owner       = "M25DE2005"
  cost_centre = "iitj-mtp"
}
