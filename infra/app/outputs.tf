output "environment" {
  description = "Target deployment environment."
  value       = var.environment
}

output "aws_region" {
  description = "AWS region of the deployed application stack."
  value       = local.aws_region
}

output "lambda_function_name" {
  description = "Name of the backend Lambda function."
  value       = aws_lambda_function.backend.function_name
}

output "lambda_function_arn" {
  description = "ARN of the backend Lambda function."
  value       = aws_lambda_function.backend.arn
}

output "lambda_log_group_name" {
  description = "Name of the CloudWatch log group for Lambda execution logs."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_id" {
  description = "ID of the API Gateway HTTP API."
  value       = aws_apigatewayv2_api.http_api.id
}

output "api_endpoint" {
  description = "Base URL endpoint of the API Gateway HTTP API."
  value       = aws_apigatewayv2_api.http_api.api_endpoint
}

output "health_url" {
  description = "Public URL for the smoke test health check endpoint (GET /)."
  value       = "${aws_apigatewayv2_api.http_api.api_endpoint}/"
}

output "db_health_url" {
  description = "Public URL for the database health check endpoint (GET /health)."
  value       = "${aws_apigatewayv2_api.http_api.api_endpoint}/health"
}

output "custom_domain_name" {
  description = "Custom domain name assigned to the API Gateway."
  value       = local.custom_domain_enabled ? aws_apigatewayv2_domain_name.api[0].domain_name : null
}

output "custom_domain_target" {
  description = "Target API Gateway domain name to point DNS CNAME records to."
  value       = local.custom_domain_enabled ? aws_apigatewayv2_domain_name.api[0].domain_name_configuration[0].target_domain_name : null
}

output "custom_domain_url" {
  description = "Public base URL for the custom domain."
  value       = local.custom_domain_enabled ? "https://${aws_apigatewayv2_domain_name.api[0].domain_name}" : null
}

output "cors_allowed_origins" {
  description = "Browser origins allowed to call the API."
  value       = var.cors_allowed_origins
}

output "google_api_key_parameter" {
  description = "SSM parameter an operator must fill with the real Gemini API key."
  value       = aws_ssm_parameter.google_api_key.name
}

output "google_oauth_parameter" {
  description = "SSM parameter an operator fills with the Google client id and secret (JSON)."
  value       = aws_ssm_parameter.google_oauth.name
}

output "google_sign_in_configured" {
  description = "False while the Google credentials are still the placeholder."
  value       = nonsensitive(local.google_oauth.client_id != local.google_oauth_placeholder_client_id)
}

output "cognito_user_pool_id" {
  description = "Cognito user pool that holds the user accounts."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_app_client_id" {
  description = "Cognito app client the web app signs in with. Not a secret."
  value       = aws_cognito_user_pool_client.web.id
}

output "cognito_issuer" {
  description = "Issuer of the tokens this API accepts."
  value       = "https://cognito-idp.${local.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
}

output "cognito_domain_url" {
  description = "Base URL of the Cognito sign-in endpoints."
  value       = "https://${local.cognito_domain_host}"
}

output "google_authorized_redirect_uri" {
  description = "Must be listed under Authorised redirect URIs in the Google OAuth client."
  value       = "https://${local.cognito_domain_host}/oauth2/idpresponse"
}

output "web_settings_path" {
  description = "SSM path the web app's build reads its settings from."
  value       = local.web_settings_prefix
}
