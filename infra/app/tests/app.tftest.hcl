# Offline contract tests for the application stack. No AWS credentials needed.
# Requires the Lambda artifact to exist: python scripts/build_lambda.py

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::798299234172:role/mock-runtime"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "798299234172"
    }
  }
  mock_data "aws_region" {
    defaults = {
      name = "ap-south-1"
    }
  }
  mock_data "aws_ssm_parameter" {
    defaults = {
      type  = "SecureString"
      value = "{\"client_id\":\"mock-client-id.apps.googleusercontent.com\",\"client_secret\":\"mock-secret\"}"
    }
  }
  mock_data "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:ap-south-1:798299234172:certificate/00000000-0000-0000-0000-000000000000"
    }
  }
}

variables {
  environment          = "dev"
  aws_account_id       = "798299234172"
  cors_allowed_origins = ["https://app-dev.ambrace.in", "http://localhost:3000"]
  custom_domain_name   = "api-dev.ambrace.in"
  web_app_url          = "https://app-dev.ambrace.in"

  cognito_domain_prefix = "ambrace-dev"
  auth_callback_urls    = ["https://app-dev.ambrace.in/auth/callback", "http://localhost:3000/auth/callback"]
  auth_logout_urls      = ["https://app-dev.ambrace.in/sign-in", "http://localhost:3000/sign-in"]
}

run "dev_sessions" {
  command = plan

  assert {
    condition     = aws_dynamodb_table.sessions.name == "ambrace-dev-sessions"
    error_message = "DEV must use its own table."
  }
  assert {
    condition     = aws_lambda_function.backend.environment[0].variables.DYNAMODB_TABLE_NAME == "ambrace-dev-sessions"
    error_message = "DEV Lambda must receive the DEV table name."
  }
  assert {
    condition     = aws_dynamodb_table.sessions.hash_key == "pk" && aws_dynamodb_table.sessions.range_key == "sk"
    error_message = "Session table must use composite owner/session keys."
  }
  assert {
    condition     = aws_dynamodb_table.sessions.billing_mode == "PROVISIONED" && aws_dynamodb_table.sessions.read_capacity == 5 && aws_dynamodb_table.sessions.write_capacity == 5
    error_message = "Prototype capacity must retain the deliberate 5/5 allocation."
  }
  assert {
    condition     = length(aws_dynamodb_table.sessions.global_secondary_index) == 0 && length(aws_dynamodb_table.sessions.local_secondary_index) == 0
    error_message = "Current access patterns need no indexes."
  }
}

run "prod_sessions" {
  command = plan

  variables {
    environment          = "prod"
    aws_account_id       = "074259854866"
    cors_allowed_origins = ["https://app.ambrace.in"]
    web_app_url          = "https://app.ambrace.in"
    custom_domain_name   = "api.ambrace.in"
  }

  assert {
    condition     = aws_dynamodb_table.sessions.name == "ambrace-prod-sessions"
    error_message = "PROD must use its own table."
  }
  assert {
    condition     = aws_lambda_function.backend.environment[0].variables.DYNAMODB_TABLE_NAME == "ambrace-prod-sessions"
    error_message = "PROD Lambda must receive the PROD table name."
  }
  assert {
    condition     = aws_dynamodb_table.sessions.deletion_protection_enabled
    error_message = "PROD table must have deletion protection."
  }
}

run "bootstrap_contract" {
  command = plan

  assert {
    condition     = aws_iam_role.lambda_runtime.permissions_boundary == "arn:aws:iam::798299234172:policy/ambrace-dev-runtime-boundary"
    error_message = "Runtime roles must carry the bootstrap boundary or the deploy role cannot create them."
  }
  assert {
    condition     = startswith(aws_iam_role.lambda_runtime.name, "ambrace-dev-")
    error_message = "Roles must be named ambrace-<env>-* to be within reach of the deploy role."
  }
  assert {
    condition     = aws_ssm_parameter.google_api_key.name == "/ambrace/dev/google_api_key"
    error_message = "Parameters must live under /ambrace/<env>/."
  }
  assert {
    condition     = aws_cloudwatch_log_group.lambda.name == "/aws/lambda/ambrace-dev-backend"
    error_message = "Log group must match the log scope of the deploy role."
  }
}

run "runtime_platform" {
  command = plan

  assert {
    condition     = aws_lambda_function.backend.runtime == "python3.13" && aws_lambda_function.backend.architectures[0] == "arm64"
    error_message = "The artifact is built for Python 3.13 on arm64."
  }
  assert {
    condition     = strcontains(aws_lambda_function.backend.layers[0], "LambdaAdapterLayerArm64")
    error_message = "The Web Adapter layer must match the function architecture."
  }
}

run "browser_origins_are_exact" {
  command = plan

  assert {
    condition     = aws_lambda_function.backend.environment[0].variables.CORS_ORIGINS == "https://app-dev.ambrace.in,http://localhost:3000"
    error_message = "The app must receive exactly the configured origins."
  }
}

run "the_web_build_reads_its_settings_from_ssm" {
  command = plan

  assert {
    condition     = output.web_settings_path == "/ambrace/dev/web"
    error_message = "Web settings path is a contract with the web deploy role's read permission."
  }

  assert {
    condition = toset(keys(aws_ssm_parameter.web_setting)) == toset([
      "app_url", "api_base_url", "cognito_user_pool_id", "cognito_client_id", "cognito_domain",
    ])
    error_message = "The web build needs exactly these settings."
  }

  assert {
    condition     = aws_ssm_parameter.web_setting["app_url"].value == "https://app-dev.ambrace.in"
    error_message = "The web app must be told its own address."
  }

  assert {
    condition     = aws_ssm_parameter.web_setting["api_base_url"].value == "https://api-dev.ambrace.in"
    error_message = "With a custom domain, the web app calls the API on it."
  }

  assert {
    condition     = alltrue([for parameter in aws_ssm_parameter.web_setting : parameter.type == "String" && parameter.tier == "Standard"])
    error_message = "Web settings are public values: plain String, free Standard tier."
  }
}

run "analytics_settings_exist_but_are_filled_in_by_hand" {
  command = plan

  assert {
    condition     = aws_ssm_parameter.web_analytics_setting["posthog_key"].name == "/ambrace/dev/web/posthog_key"
    error_message = "The web pipeline reads the key from this exact name."
  }

  assert {
    condition     = aws_ssm_parameter.web_analytics_setting["posthog_host"].name == "/ambrace/dev/web/posthog_host"
    error_message = "The web pipeline reads the host from this exact name."
  }

  assert {
    condition     = !startswith(aws_ssm_parameter.web_analytics_setting["posthog_key"].value, "phc_")
    error_message = "A new environment starts with analytics off: the web app only accepts a value starting with phc_ as a key."
  }

  assert {
    condition     = startswith(aws_ssm_parameter.web_analytics_setting["posthog_host"].value, "https://")
    error_message = "Events are only ever sent over HTTPS."
  }

  assert {
    condition     = alltrue([for parameter in aws_ssm_parameter.web_analytics_setting : parameter.type == "String" && parameter.tier == "Standard"])
    error_message = "A PostHog project key is public: plain String, free Standard tier."
  }
}

run "rejects_a_web_app_the_api_would_refuse" {
  command = plan

  variables {
    web_app_url = "https://somewhere-else.ambrace.in"
  }

  expect_failures = [var.web_app_url]
}

run "rejects_wildcard_origin" {
  command = plan

  variables {
    cors_allowed_origins = ["https://*.vercel.app"]
  }

  expect_failures = [var.cors_allowed_origins]
}

run "sign_in_is_free_tier_and_protected" {
  command = plan

  assert {
    condition     = aws_cognito_user_pool.main.user_pool_tier == "LITE"
    error_message = "The user pool must stay on the cheapest feature plan."
  }

  assert {
    condition     = aws_cognito_user_pool.main.deletion_protection == "ACTIVE"
    error_message = "Deleting the user pool deletes every account; protection must stay on."
  }

  assert {
    condition     = aws_cognito_user_pool.main.mfa_configuration == "OFF"
    error_message = "MFA by text message costs money and is not part of the MVP."
  }

  assert {
    condition     = aws_cognito_user_pool.main.email_configuration[0].email_sending_account == "COGNITO_DEFAULT"
    error_message = "Email must use the built-in Cognito sender, not SES."
  }

  assert {
    condition     = toset(aws_cognito_user_pool.main.username_attributes) == toset(["email"])
    error_message = "People sign in with their email address."
  }
}

run "web_client_is_public_pkce_client" {
  command = plan

  assert {
    condition     = aws_cognito_user_pool_client.web.generate_secret == false
    error_message = "A browser app cannot keep a client secret."
  }

  assert {
    condition     = toset(aws_cognito_user_pool_client.web.allowed_oauth_flows) == toset(["code"])
    error_message = "Only the authorization-code flow is allowed (no implicit flow)."
  }

  assert {
    condition     = toset(aws_cognito_user_pool_client.web.supported_identity_providers) == toset(["COGNITO", "Google"])
    error_message = "Sign-in must offer email/password and Google."
  }

  assert {
    condition     = !contains(aws_cognito_user_pool_client.web.explicit_auth_flows, "ALLOW_USER_PASSWORD_AUTH")
    error_message = "Plain password sign-in must stay disabled; the app uses SRP."
  }

  assert {
    condition     = toset(aws_cognito_user_pool_client.web.callback_urls) == toset(["https://app-dev.ambrace.in/auth/callback", "http://localhost:3000/auth/callback"])
    error_message = "Cognito must only return users to the configured URLs."
  }
}

# The value itself is only read during apply on a first deployment, so its
# format check (secrets.tf) cannot be exercised by these plan-only tests.
run "google_sign_in_is_wired_to_the_ssm_parameter" {
  command = plan

  assert {
    condition     = aws_ssm_parameter.google_oauth.name == "/ambrace/dev/google_oauth" && aws_ssm_parameter.google_oauth.type == "SecureString"
    error_message = "Google credentials live in one encrypted SSM parameter per environment."
  }

  assert {
    condition     = data.aws_ssm_parameter.google_oauth.name == aws_ssm_parameter.google_oauth.name
    error_message = "Terraform must read the same parameter it creates."
  }

  assert {
    condition     = aws_cognito_identity_provider.google.provider_type == "Google"
    error_message = "The identity provider must be Google."
  }

  assert {
    condition     = aws_cognito_user_pool_domain.main.domain == "ambrace-dev"
    error_message = "The sign-in address must match the one registered with Google."
  }
}

run "rejects_reserved_word_in_domain_prefix" {
  command = plan

  variables {
    cognito_domain_prefix = "ambrace-cognito"
  }

  expect_failures = [var.cognito_domain_prefix]
}

run "rejects_wildcard_callback" {
  command = plan

  variables {
    auth_callback_urls = ["https://*.ambrace.in/auth/callback"]
  }

  expect_failures = [var.auth_callback_urls]
}

# The web app asks Cognito to return to /sign-in after sign-out. Cognito
# compares the whole address, so the bare site address breaks sign-out for
# every Google account ("Required parameters missing").
run "rejects_a_sign_out_address_the_web_app_never_asks_for" {
  command = plan

  variables {
    auth_logout_urls = ["https://app-dev.ambrace.in"]
  }

  expect_failures = [var.auth_logout_urls]
}

run "sign_out_returns_to_the_sign_in_page" {
  command = plan

  assert {
    condition     = aws_cognito_user_pool_client.web.logout_urls == toset(["https://app-dev.ambrace.in/sign-in", "http://localhost:3000/sign-in"])
    error_message = "The web client must allow sign-out to return to the sign-in page."
  }
}
