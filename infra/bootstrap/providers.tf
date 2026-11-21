provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile != "" ? var.aws_profile : null

  # Hard stop if the active credentials belong to a different account than the
  # one this var-file targets (e.g. dev.tfvars applied with the prod profile).
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project     = "Ambrace"
      ManagedBy   = "Terraform"
      Component   = "Bootstrap"
      Environment = var.environment
      Repository  = "ambrace-backend"
    }
  }
}

# CloudFront reads viewer certificates from us-east-1 only. Nothing else is
# created there.
provider "aws" {
  alias   = "us_east_1"
  region  = "us-east-1"
  profile = var.aws_profile != "" ? var.aws_profile : null

  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project     = "Ambrace"
      ManagedBy   = "Terraform"
      Component   = "Bootstrap"
      Environment = var.environment
      Repository  = "ambrace-backend"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}
