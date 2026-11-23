# ==============================================================================
# SSM Parameter Store: what the web app's build needs to know
# ==============================================================================
#
# The web app (Ambrace-SI/ambrace-webapp) is built into plain files, and these
# values are baked into them at build time. None is a secret: every one ends up
# in JavaScript that any browser downloads. They are kept here, not in GitHub,
# so there is one place per environment and nothing is copied by hand; the web
# pipeline reads this path, and can only read it.
#
# Plain String, Standard tier: no charge.

locals {
  web_settings_prefix = "${local.ssm_parameter_prefix}/web"

  web_settings = {
    app_url = {
      value       = var.web_app_url
      description = "Address the web app is served from. Sign-in returns here."
    }
    api_base_url = {
      value       = local.custom_domain_enabled ? "https://${var.custom_domain_name}" : aws_apigatewayv2_api.http_api.api_endpoint
      description = "Base address of the backend API."
    }
    cognito_user_pool_id = {
      value       = aws_cognito_user_pool.main.id
      description = "Cognito user pool the web app signs in to."
    }
    cognito_client_id = {
      value       = aws_cognito_user_pool_client.web.id
      description = "Cognito app client of the web app. A browser client has no secret."
    }
    cognito_domain = {
      value       = local.cognito_domain_host
      description = "Host of the Cognito sign-in pages, without https://."
    }
  }
}

resource "aws_ssm_parameter" "web_setting" {
  for_each = local.web_settings

  name        = "${local.web_settings_prefix}/${each.key}"
  description = "Ambrace ${var.environment} web build: ${each.value.description}"
  type        = "String"
  tier        = "Standard"
  value       = each.value.value
}

# ------------------------------------------------------------------------------
# Product analytics (PostHog)
# ------------------------------------------------------------------------------
#
# Terraform creates these so every environment has them, but it does not own
# their values: the project key is pasted in by a person, once, and must
# survive every later apply. Until then the key is a placeholder. The web app
# only accepts a value that starts with "phc_" as a key, so any placeholder
# leaves analytics off.
#
# A PostHog project key is public: it ships in the JavaScript.

locals {
  web_setting_not_set = "unset"

  web_analytics_settings = {
    posthog_key = {
      value       = local.web_setting_not_set
      description = "PostHog project key (phc_...). Anything else leaves analytics off."
    }
    posthog_host = {
      value       = "https://us.i.posthog.com"
      description = "Address PostHog events are sent to. Must also be in the web app's connect_sources."
    }
  }
}

resource "aws_ssm_parameter" "web_analytics_setting" {
  for_each = local.web_analytics_settings

  name        = "${local.web_settings_prefix}/${each.key}"
  description = "Ambrace ${var.environment} web build: ${each.value.description}"
  type        = "String"
  tier        = "Standard"
  value       = each.value.value

  lifecycle {
    ignore_changes = [value]
  }
}
