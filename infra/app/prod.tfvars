# Ambrace Prod account. No secrets belong in this file.
environment           = "prod"
aws_account_id        = "074259854866"
log_retention_in_days = 30
custom_domain_name    = "api.ambrace.in"
cors_allowed_origins = [
  "https://app.ambrace.in",
]
web_app_url = "https://app.ambrace.in"

# Sign-in. The prefix is registered in the Google OAuth client "Ambrace Prod".
cognito_domain_prefix = "ambrace"
auth_callback_urls = [
  "https://app.ambrace.in/auth/callback",
]
auth_logout_urls = [
  "https://app.ambrace.in",
]
