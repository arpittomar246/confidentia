# Network.
#
# Variables and outputs live in this file rather than in `variables.tf` and
# `outputs.tf` beside it. The convention exists to keep large modules navigable;
# these modules are forty lines, and three files of six lines each is harder to
# read than one file with headings.

variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "allowed_ingress" { type = list(string) }
variable "tags" { type = map(string) }

resource "azurerm_virtual_network" "main" {
  name                = "vnet-${var.name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  address_space       = ["10.42.0.0/16"]
  tags                = var.tags
}

# Confidential compute. Delegated to nothing: these are plain VMs, and the enclave
# boundary is inside the guest rather than at the network edge.
resource "azurerm_subnet" "compute" {
  name                 = "snet-compute"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.42.1.0/24"]

  # Storage reached over the Azure backbone rather than the public internet. The
  # lake holds encrypted Parquet, so this is defence in depth rather than the
  # control -- but a private path removes an entire class of exfiltration.
  service_endpoints = ["Microsoft.Storage", "Microsoft.KeyVault"]
}

# PostgreSQL Flexible Server with VNet integration requires a delegated subnet of
# its own, and the delegation cannot be changed after creation.
resource "azurerm_subnet" "data" {
  name                 = "snet-data"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.42.2.0/24"]

  delegation {
    name = "postgres"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_private_dns_zone" "postgres" {
  name                = "${var.name}.postgres.database.azure.com"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "link-postgres"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.main.id
  registration_enabled  = false
  tags                  = var.tags
}

resource "azurerm_network_security_group" "compute" {
  name                = "nsg-compute-${var.name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  # No inbound rule for SSH, and none for the API.
  #
  # Operators reach these hosts through Azure Bastion or the Serial Console, not
  # through a port on the internet. The API is fronted by Container Apps, which
  # terminates ingress somewhere this subnet cannot be reached from. A default
  # NSG already denies inbound; the point of saying so here is that the absence
  # is deliberate rather than forgotten.
  dynamic "security_rule" {
    for_each = length(var.allowed_ingress) > 0 ? [1] : []
    content {
      name                       = "allow-operator-https"
      priority                   = 200
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "443"
      source_address_prefixes    = var.allowed_ingress
      destination_address_prefix = "*"
    }
  }
}

resource "azurerm_subnet_network_security_group_association" "compute" {
  subnet_id                 = azurerm_subnet.compute.id
  network_security_group_id = azurerm_network_security_group.compute.id
}

# The data subnet needs one too (Checkov CKV2_AZURE_31, and it is right).
#
# A delegated subnet is not exempt from needing a security group: delegation
# constrains what service may live there, not what may talk to it. This one has no
# rules of its own -- the default deny-inbound-from-internet is the whole point,
# and the only traffic Postgres should see comes from the compute subnet.
resource "azurerm_network_security_group" "data" {
  name                = "nsg-data-${var.name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  security_rule {
    name                       = "allow-postgres-from-compute"
    priority                   = 200
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5432"
    source_address_prefix      = "10.42.1.0/24"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "deny-all-other-inbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "data" {
  subnet_id                 = azurerm_subnet.data.id
  network_security_group_id = azurerm_network_security_group.data.id
}

output "compute_subnet_id" { value = azurerm_subnet.compute.id }
output "data_subnet_id" { value = azurerm_subnet.data.id }
output "postgres_dns_zone_id" { value = azurerm_private_dns_zone.postgres.id }
