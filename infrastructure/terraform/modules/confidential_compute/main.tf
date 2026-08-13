# Confidential compute — where TEE_BACKEND stops being `sim`.
#
# A DCasv5 VM runs with AMD SEV-SNP: guest memory is encrypted with a key the
# hypervisor does not hold, and the firmware will produce a signed attestation
# report describing the launch measurement. That report is what MAA verifies and
# what the HSM's release policy is written against.
#
# This is the single resource that would turn every MODELLED number in the project
# into a measurement. `TeeSettings.is_measured` returns False under `sim`; running
# the pipeline here is what makes it True.

variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "subnet_id" { type = string }
variable "vm_size" { type = string }
variable "attestation_uri" { type = string }
variable "managed_hsm_uri" { type = string }
variable "tags" { type = map(string) }

variable "admin_ssh_public_key" {
  description = "OpenSSH public key for the operator account. No default, on purpose."
  type        = string

  validation {
    condition     = can(regex("^(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp[0-9]+) ", var.admin_ssh_public_key))
    error_message = "admin_ssh_public_key must be an OpenSSH public key. A private key pasted here would be committed to state."
  }
}

resource "azurerm_network_interface" "pipeline" {
  name                = "nic-pipeline-${var.name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"
    # No public IP. Reached through Bastion; the pipeline needs no inbound path.
  }
}

resource "azurerm_linux_virtual_machine" "pipeline" {
  name                = "vm-pipeline-${var.name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  size                = var.vm_size
  admin_username      = "confidentia"

  network_interface_ids = [azurerm_network_interface.pipeline.id]

  # The three settings that make this a confidential VM rather than an ordinary
  # one. Omit any of them and the deployment succeeds, the pipeline runs, and
  # there is no enclave -- which is exactly the failure this project exists to
  # make visible, so they are asserted rather than assumed.
  #
  #   vtpm_enabled          the virtual TPM that holds the attestation identity
  #   secure_boot_enabled   required for a measured launch to mean anything
  #   security_encryption_type on the OS disk = the memory encryption itself
  vtpm_enabled        = true
  secure_boot_enabled = true

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"

    # "VMGuestStateOnly" encrypts the guest state blob with a platform-managed
    # key. "DiskWithVMGuestState" also encrypts the OS disk with a customer-managed
    # key -- stronger, and it requires a Disk Encryption Set wired to the HSM,
    # which cannot be created until the HSM has been activated with its security
    # domain. Named here because the weaker option is a real limitation, not an
    # oversight.
    security_encryption_type = "VMGuestStateOnly"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-confidential-vm-jammy"
    sku       = "22_04-lts-cvm"
    version   = "latest"
  }

  # Password authentication disabled; the key is supplied out of band. A
  # confidential VM whose console is reachable with a password is a confidential
  # VM with a plaintext side door.
  disable_password_authentication = true

  admin_ssh_key {
    username = "confidentia"
    # Supplied as a variable, not read with `file()`.
    #
    # `file("~/.ssh/id_rsa.pub")` fails `terraform validate` outright -- the
    # function does not expand `~` and requires the path to exist at validation
    # time, so the configuration would only validate on a machine that happened to
    # have that key. Passing it in also means the key is a deliberate input rather
    # than whatever the operator's default key happens to be.
    public_key = var.admin_ssh_public_key
  }

  identity {
    type = "SystemAssigned"
  }

  # The pipeline reads these to talk to MAA and the HSM. They are endpoints, not
  # credentials: possession of them grants nothing, because the release policy is
  # what decides.
  tags = merge(var.tags, {
    tee_backend     = "snp-cvm"
    attestation_uri = var.attestation_uri
    managed_hsm_uri = var.managed_hsm_uri
  })
}

# The attestation extension. Without it the guest can produce a hardware report but
# has nothing that turns it into an MAA token, and the HSM speaks only MAA tokens.
resource "azurerm_virtual_machine_extension" "attestation" {
  name                       = "GuestAttestation"
  virtual_machine_id         = azurerm_linux_virtual_machine.pipeline.id
  publisher                  = "Microsoft.Azure.Security.LinuxAttestation"
  type                       = "GuestAttestation"
  type_handler_version       = "1.0"
  auto_upgrade_minor_version = true
  tags                       = var.tags

  settings = jsonencode({
    AttestationConfig = {
      MaaSettings = {
        maaEndpoint   = var.attestation_uri
        maaTenantName = "GuestAttestation"
      }
    }
  })
}

output "vm_id" { value = azurerm_linux_virtual_machine.pipeline.id }
output "principal_id" { value = azurerm_linux_virtual_machine.pipeline.identity[0].principal_id }
output "private_ip" { value = azurerm_network_interface.pipeline.private_ip_address }
