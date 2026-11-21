output "aws_account_id" {
  description = "Account this bootstrap was applied to."
  value       = local.account_id
}

output "aws_region" {
  description = "Region of the regional bootstrap resources."
  value       = local.aws_region
}

output "state_bucket_name" {
  description = "S3 bucket holding Terraform state for this account."
  value       = aws_s3_bucket.state.id
}

output "app_state_key" {
  description = "Object key infra/app must use for its state in this account."
  value       = local.app_state_key
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC identity provider."
  value       = aws_iam_openid_connect_provider.github.arn
}

output "github_deploy_subject" {
  description = "The only OIDC subject allowed to assume the deploy role."
  value       = local.github_deploy_subject
}

output "deploy_role_arn" {
  description = "Set this as the AWS_DEPLOY_ROLE_ARN variable on the matching GitHub Environment."
  value       = aws_iam_role.deploy.arn
}

output "runtime_boundary_arn" {
  description = "Permissions boundary infra/app must attach to every IAM role it creates."
  value       = aws_iam_policy.runtime_boundary.arn
}

output "api_certificate_arn" {
  description = "ACM certificate for the API custom domain (null when api_domain_name is empty)."
  value       = one(aws_acm_certificate.api[*].arn)
}

output "api_certificate_validation_records" {
  description = "CNAME record(s) to create at the DNS provider so ACM can issue the API certificate."
  value = flatten([
    for cert in aws_acm_certificate.api : [
      for option in cert.domain_validation_options : {
        domain = option.domain_name
        type   = option.resource_record_type
        name   = option.resource_record_name
        value  = option.resource_record_value
      }
    ]
  ])
}

# ------------------------------------------------------------------------------
# Web app
# ------------------------------------------------------------------------------

output "web_deploy_subject" {
  description = "The only OIDC subject allowed to assume the web deploy role."
  value       = local.web_deploy_subject
}

output "web_deploy_role_arn" {
  description = "Set this as the AWS_DEPLOY_ROLE_ARN variable on the matching GitHub Environment of ambrace-webapp."
  value       = aws_iam_role.web_deploy.arn
}

output "web_bucket_name" {
  description = "The only bucket name the web stack is allowed to create for the site."
  value       = local.web_bucket_name
}

output "web_state_key" {
  description = "Object key ambrace-webapp/infra must use for its state in this account."
  value       = local.web_state_key
}

output "web_parameter_path" {
  description = "SSM path the web build reads its settings from."
  value       = "/${local.web_parameter_path}"
}

output "app_certificate_arn" {
  description = "ACM certificate (us-east-1) for the app domain (null when app_domain_name is empty)."
  value       = one(aws_acm_certificate.app[*].arn)
}

output "app_certificate_validation_records" {
  description = "CNAME record(s) to create at the DNS provider so ACM can issue the app certificate."
  value = flatten([
    for cert in aws_acm_certificate.app : [
      for option in cert.domain_validation_options : {
        domain = option.domain_name
        type   = option.resource_record_type
        name   = option.resource_record_name
        value  = option.resource_record_value
      }
    ]
  ])
}
