# The cheap deployment: everything the dissertation needs measured, nothing it
# cannot delete the same day.
#
# Identical to dev.tfvars except for `deploy_managed_hsm`. That one flag is the
# difference between an afternoon's experiment and a Managed HSM that purge
# protection keeps — and bills for — through its retention period.
#
# What this gives you: a real SEV-SNP measurement, a real MAA token, and the
# enclave overhead that section 5.5 currently reports as MODELLED ×1.206.
#
# What it does not: enforceable key release. The workload attests, the four refusal
# paths run and audit, and they remain a policy the application honours rather than
# one the hardware imposes. Report it that way.
#
#   terraform apply -var-file=environments/measure.tfvars \
#     -var="admin_ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
#     -var='admin_object_ids=["<your-entra-object-id>"]'
#
# See docs/deploy.md before running it.

project     = "confidentia"
environment = "dev"

# Must offer both DCasv5 confidential VMs and an attestation provider. Check with
# `az vm list-skus` before planning; quota is often zero by default.
location = "northeurope"

tenant_names = ["healthcare", "financial"]

# The resource this file exists to leave out.
deploy_managed_hsm = false

confidential_vm_size = "Standard_DC4as_v5"

postgres_sku     = "GP_Standard_D2ds_v4"
postgres_version = "16"

# Nothing reaches the confidential VM from the internet. Operators use Azure
# Bastion.
allowed_ingress_cidrs = []

tags = {
  owner       = "M25DE2005"
  cost_centre = "iitj-mtp"
  purpose     = "one-off measurement; destroy same day"
}
