# Provider versions, pinned.
#
# `azurerm` 4.x renamed and removed enough arguments from 3.x that a floating
# constraint would make this configuration valid today and invalid on the next
# `init`. The lock file records exact hashes; this records the intent.
terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.14"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # No backend block.
  #
  # State for this configuration has never existed, because it has never been
  # applied -- there is no Azure subscription behind this project. Declaring an
  # `azurerm` backend would name a storage account that does not exist and make
  # `terraform init` fail for everyone, which would hide the validation this
  # configuration *can* pass.
  #
  # A real deployment adds:
  #
  #   backend "azurerm" {
  #     resource_group_name  = "rg-confidentia-tfstate"
  #     storage_account_name = "stconfidentiatfstate"
  #     container_name       = "tfstate"
  #     key                  = "confidentia.tfstate"
  #   }
  #
  # State holds connection strings and generated passwords in cleartext, so that
  # storage account needs the same protection as the data plane it describes.
}

provider "azurerm" {
  features {
    key_vault {
      # Soft delete exists so a deleted key can be recovered. Purging on destroy
      # would make `terraform destroy` irreversibly lose every DEK-wrapping key --
      # and in this platform, losing the key hierarchy means every encrypted table
      # is unreadable forever, with no escrow by design.
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }

    resource_group {
      # Refuse to delete a resource group that still contains resources Terraform
      # does not know about.
      prevent_deletion_if_contains_resources = true
    }
  }
}
