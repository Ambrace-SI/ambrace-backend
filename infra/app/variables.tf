variable "environment" {
  type        = string
  description = "Target deployment environment. One AWS account per environment."

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Environment must be dev or prod."
  }
}

variable "aws_account_id" {
  type        = string
  description = "AWS account this environment lives in. The provider refuses to run against any other account."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "aws_region" {
  type        = string
  description = "AWS region to deploy the application runtime."
  default     = "ap-south-1"
}

# ------------------------------------------------------------------------------
# Viva behaviour
# ------------------------------------------------------------------------------

variable "gemini_live_model" {
  type        = string
  description = "Gemini Live model used to constrain ephemeral credentials."
  default     = "gemini-3.8-live"

  validation {
    condition     = can(regex("^\\S+$", var.gemini_live_model))
    error_message = "Gemini Live model must be non-empty and contain no whitespace."
  }
}

variable "class_insight_provider" {
  type        = string
  description = "Writer of teachers' class insights: gemini, or heuristic (no model, no cost)."
  default     = "gemini"

  validation {
    condition     = contains(["gemini", "heuristic"], var.class_insight_provider)
    error_message = "Class insight provider must be gemini or heuristic."
  }
}

variable "class_insight_model" {
  type        = string
  description = "Text model used for class insights when the provider is a model."
  default     = "gemini-3.5-flash-lite"

  validation {
    condition     = can(regex("^\\S+$", var.class_insight_model))
    error_message = "Class insight model must be non-empty and contain no whitespace."
  }
}

variable "viva_session_duration_minutes" {
  type        = number
  description = "Fixed viva duration, configured per environment."
  default     = 5

  validation {
    condition     = contains([5, 10, 15], var.viva_session_duration_minutes)
    error_message = "Viva duration must be 5, 10, or 15 minutes."
  }
}

# ------------------------------------------------------------------------------
# Lambda
# ------------------------------------------------------------------------------

variable "lambda_memory_size" {
  type        = number
  description = "Memory allocated to the Lambda function in MB."
  default     = 512
}

variable "lambda_timeout" {
  type        = number
  description = "Lambda function execution timeout in seconds."
  default     = 30
}

variable "log_retention_in_days" {
  type        = number
  description = "CloudWatch log retention period in days."
  default     = 14
}

variable "lambda_adapter_layer_arn" {
  type        = string
  description = "ARN of the AWS Lambda Web Adapter layer (arm64). Version from https://github.com/aws/aws-lambda-web-adapter."
  default     = "arn:aws:lambda:ap-south-1:753240598075:layer:LambdaAdapterLayerArm64:30"

  validation {
    condition     = can(regex(":753240598075:layer:LambdaAdapterLayerArm64:[0-9]+$", var.lambda_adapter_layer_arn))
    error_message = "The function is arm64; use the AWS-published LambdaAdapterLayerArm64 layer with a pinned version."
  }
}

variable "lambda_artifact_path" {
  type        = string
  description = "Relative path to the Lambda ZIP artifact from the Terraform root module."
  default     = "../../dist/ambrace-backend.zip"
}

# ------------------------------------------------------------------------------
# API
# ------------------------------------------------------------------------------

variable "custom_domain_name" {
  type        = string
  description = "Public hostname for the API (e.g. api-dev.ambrace.in). Its ACM certificate must already be ISSUED. Empty disables the custom domain."
  default     = ""
}

variable "cors_allowed_origins" {
  type        = list(string)
  description = "Exact browser origins allowed to call the API with credentials. No wildcards."

  validation {
    condition = alltrue([
      for origin in var.cors_allowed_origins :
      can(regex("^https://[a-z0-9.-]+$", origin)) || can(regex("^http://localhost:[0-9]+$", origin))
    ])
    error_message = "Each origin must be https://host with no path or wildcard, or http://localhost:<port>."
  }
}

variable "api_throttling_rate_limit" {
  type        = number
  description = "Steady-state requests per second allowed across the whole API."
  default     = 20
}

variable "api_throttling_burst_limit" {
  type        = number
  description = "Short burst of requests allowed across the whole API."
  default     = 40
}

variable "web_app_url" {
  type        = string
  description = "Address the deployed web app is served from. Handed to the web build, and must be one of cors_allowed_origins."

  validation {
    condition     = can(regex("^https://[a-z0-9.-]+$", var.web_app_url))
    error_message = "web_app_url must be https://host with no path."
  }

  validation {
    condition     = contains(var.cors_allowed_origins, var.web_app_url)
    error_message = "web_app_url must be listed in cors_allowed_origins, or the deployed web app could not call the API."
  }
}

# ------------------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------------------

variable "cognito_domain_prefix" {
  type        = string
  description = "First part of the Cognito sign-in address (<prefix>.auth.<region>.amazoncognito.com). Must be unused by any other AWS customer in the region, and must match the URLs registered in the Google OAuth client."

  validation {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", var.cognito_domain_prefix)) && !can(regex("aws|amazon|cognito", var.cognito_domain_prefix))
    error_message = "Use lowercase letters, numbers and hyphens (not at either end). Cognito also forbids the words aws, amazon and cognito."
  }
}

variable "auth_callback_urls" {
  type        = list(string)
  description = "Exact web app URLs Cognito may send a user back to after sign-in."

  validation {
    condition = length(var.auth_callback_urls) > 0 && alltrue([
      for url in var.auth_callback_urls :
      can(regex("^https://[a-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$", url)) || can(regex("^http://localhost:[0-9]+(/[A-Za-z0-9._~/-]*)?$", url))
    ])
    error_message = "Each callback must be an https:// URL, or http://localhost:<port> for local development. No wildcards or query strings."
  }
}

variable "auth_logout_urls" {
  type        = list(string)
  description = "Exact web app URLs Cognito may send a user to after sign-out. The web app always asks for its sign-in page (redirectSignOut in ambrace-webapp/lib/auth/config.ts), and Cognito refuses any address that is not listed here in full."

  validation {
    condition = length(var.auth_logout_urls) > 0 && alltrue([
      for url in var.auth_logout_urls :
      can(regex("^https://[a-z0-9.-]+/sign-in$", url)) || can(regex("^http://localhost:[0-9]+/sign-in$", url))
    ])
    error_message = "Each sign-out URL must be the web app's sign-in page: https://<host>/sign-in, or http://localhost:<port>/sign-in for local development. The bare site address is refused by Cognito at sign-out."
  }
}
