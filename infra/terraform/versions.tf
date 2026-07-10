terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Local state by default so `terraform init` works with zero prerequisites.
  # This state file contains the generated DB password in plaintext (see
  # rds.tf) - before running this for real, switch to a remote backend so
  # state isn't sitting unencrypted on a laptop or committed to git:
  #
  # backend "s3" {
  #   bucket         = "your-tfstate-bucket"
  #   key            = "task-scheduler/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "your-tflock-table"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      ManagedBy   = "terraform"
      Environment = var.environment
    }
  }
}
