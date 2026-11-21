variable "environment" {
  type        = string
  description = "Short environment name. One AWS account per environment."

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\"."
  }
}

variable "aws_account_id" {
  type        = string
  description = "The AWS account this environment lives in. The provider refuses to run against any other account."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "aws_region" {
  type        = string
  description = "Region for all regional bootstrap resources (state bucket, API certificate)."
  default     = "ap-south-1"
}

variable "aws_profile" {
  type        = string
  description = "AWS CLI profile used for the manual bootstrap apply. Leave empty to use the ambient credentials."
  default     = ""
}

# ------------------------------------------------------------------------------
# GitHub Actions OIDC
# ------------------------------------------------------------------------------

variable "github_subject_prefix" {
  type        = string
  description = <<-EOT
    Prefix of the OIDC `sub` claim GitHub issues for this repository. Repositories
    created after 15 Jul 2026 use the immutable form that embeds the numeric owner
    and repository IDs: `repo:OWNER@OWNER_ID/REPO@REPO_ID`. Read the live value with:
      gh api repos/Ambrace-SI/ambrace-backend/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  default     = "repo:Ambrace-SI@357749772/ambrace-backend@1462386177"

  validation {
    condition     = can(regex("^repo:[^:*?]+/[^:*?]+$", var.github_subject_prefix))
    error_message = "github_subject_prefix must look like repo:OWNER/REPO or repo:OWNER@ID/REPO@ID, with no wildcards."
  }
}

variable "github_environment_name" {
  type        = string
  description = "GitHub Environment whose jobs may assume the deploy role (case-sensitive, must match the workflow's `environment:`)."

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.github_environment_name))
    error_message = "github_environment_name may contain only letters, digits, hyphens and underscores."
  }
}

variable "github_oidc_audience" {
  type        = string
  description = "Audience GitHub puts in the token. aws-actions/configure-aws-credentials uses sts.amazonaws.com."
  default     = "sts.amazonaws.com"
}

# ------------------------------------------------------------------------------
# API custom domain
# ------------------------------------------------------------------------------

variable "api_domain_name" {
  type        = string
  description = "Public hostname of the backend API in this environment (e.g. api-dev.ambrace.in). Empty skips the certificate."
  default     = ""
}

# ------------------------------------------------------------------------------
# Web app (Ambrace-SI/ambrace-webapp)
# ------------------------------------------------------------------------------

variable "web_github_subject_prefix" {
  type        = string
  description = <<-EOT
    Prefix of the OIDC `sub` claim GitHub issues for the web repository. Read the
    live value with:
      gh api repos/Ambrace-SI/ambrace-webapp/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  default     = "repo:Ambrace-SI@357749772/ambrace-webapp@1462385816"

  validation {
    condition     = can(regex("^repo:[^:*?]+/[^:*?]+$", var.web_github_subject_prefix))
    error_message = "web_github_subject_prefix must look like repo:OWNER/REPO or repo:OWNER@ID/REPO@ID, with no wildcards."
  }
}

variable "app_domain_name" {
  type        = string
  description = "Public hostname of the web app in this environment (e.g. app-dev.ambrace.in). Empty skips the certificate."
  default     = ""
}
