# Ambrace Dev account. No secrets belong in this file.
environment           = "dev"
aws_account_id        = "798299234172"
log_retention_in_days = 14
custom_domain_name    = "api-dev.ambrace.in"
cors_allowed_origins = [
  "https://app-dev.ambrace.in",
  "http://localhost:3000",
]
web_app_url = "https://app-dev.ambrace.in"

# Sign-in. The prefix is registered in the Google OAuth client "Ambrace Dev".
cognito_domain_prefix = "ambrace-dev"
auth_callback_urls = [
  "https://app-dev.ambrace.in/auth/callback",
  "http://localhost:3000/auth/callback",
]
auth_logout_urls = [
  "https://app-dev.ambrace.in",
  "http://localhost:3000",
]
