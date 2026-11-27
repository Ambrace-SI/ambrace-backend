locals {
  name_prefix   = "ambrace-${var.environment}"
  function_name = "${local.name_prefix}-backend"
  account_id    = data.aws_caller_identity.current.account_id
  aws_region    = data.aws_region.current.name

  # Contract with infra/bootstrap: every IAM role created here must carry this
  # boundary, or the deploy role is not allowed to create it.
  runtime_boundary_arn = "arn:aws:iam::${local.account_id}:policy/${local.name_prefix}-runtime-boundary"

  ssm_parameter_prefix = "/ambrace/${var.environment}"

  custom_domain_enabled = var.custom_domain_name != ""
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ==============================================================================
# CloudWatch Logs (explicitly managed so retention is always set)
# ==============================================================================

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_in_days
}

# ==============================================================================
# Lambda runtime role (least privilege, capped by the runtime boundary)
# ==============================================================================

resource "aws_iam_role" "lambda_runtime" {
  name                 = "${local.name_prefix}-lambda-runtime"
  description          = "Execution role for ${local.function_name}"
  assume_role_policy   = data.aws_iam_policy_document.lambda_trust.json
  permissions_boundary = local.runtime_boundary_arn
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    sid     = "LambdaServiceAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "lambda_logging" {
  name   = "${local.name_prefix}-lambda-logging"
  role   = aws_iam_role.lambda_runtime.id
  policy = data.aws_iam_policy_document.lambda_logging.json
}

data "aws_iam_policy_document" "lambda_logging" {
  statement {
    sid    = "LambdaLogEvents"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = [
      "${aws_cloudwatch_log_group.lambda.arn}:*"
    ]
  }
}

resource "aws_iam_role_policy" "lambda_ssm_read" {
  name   = "${local.name_prefix}-lambda-ssm-read"
  role   = aws_iam_role.lambda_runtime.id
  policy = data.aws_iam_policy_document.lambda_ssm_read.json
}

data "aws_iam_policy_document" "lambda_ssm_read" {
  statement {
    sid     = "SSMGetParametersExact"
    effect  = "Allow"
    actions = ["ssm:GetParameters"]
    resources = [
      "arn:aws:ssm:${local.aws_region}:${local.account_id}:parameter${local.ssm_parameter_prefix}/google_api_key",
    ]
  }

  # SecureString values are encrypted with the AWS-managed aws/ssm key. Decrypt
  # is allowed only when the request arrives through Parameter Store.
  statement {
    sid       = "DecryptParametersViaSsm"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${local.aws_region}:${local.account_id}:key/*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.aws_region}.amazonaws.com"]
    }
  }
}

# ==============================================================================
# Lambda function (FastAPI behind the AWS Lambda Web Adapter)
# ==============================================================================

resource "aws_lambda_function" "backend" {
  function_name = local.function_name
  description   = "Ambrace FastAPI backend running on AWS Lambda with Lambda Web Adapter"
  role          = aws_iam_role.lambda_runtime.arn

  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "run.sh"

  memory_size = var.lambda_memory_size
  timeout     = var.lambda_timeout

  filename         = "${path.module}/${var.lambda_artifact_path}"
  source_code_hash = filebase64sha256("${path.module}/${var.lambda_artifact_path}")

  layers = [var.lambda_adapter_layer_arn]

  environment {
    variables = {
      # Lambda Web Adapter contract
      AWS_LAMBDA_EXEC_WRAPPER                = "/opt/bootstrap"
      PORT                                   = "8080"
      AWS_LWA_READINESS_CHECK_PATH           = "/"
      AWS_LWA_READINESS_CHECK_HEALTHY_STATUS = "200-399"
      AWS_LWA_INVOKE_MODE                    = "buffered"

      # Application configuration. Secrets are read from SSM at cold start;
      # everything here is a non-secret identifier or setting.
      AMBRACE_SSM_PARAMETER_PREFIX  = local.ssm_parameter_prefix
      DYNAMODB_TABLE_NAME           = aws_dynamodb_table.sessions.name
      GEMINI_LIVE_MODEL             = var.gemini_live_model
      VIVA_SESSION_DURATION_MINUTES = tostring(var.viva_session_duration_minutes)
      CLASS_INSIGHT_PROVIDER        = var.class_insight_provider
      CLASS_INSIGHT_MODEL           = var.class_insight_model
      CORS_ORIGINS                  = join(",", var.cors_allowed_origins)
      COGNITO_USER_POOL_ID          = aws_cognito_user_pool.main.id
      COGNITO_APP_CLIENT_ID         = aws_cognito_user_pool_client.web.id
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda_logging,
    aws_iam_role_policy.lambda_ssm_read,
    aws_iam_role_policy.lambda_sessions,
    # The app reads this parameter at import time and fails closed without it.
    aws_ssm_parameter.google_api_key,
  ]
}

# ==============================================================================
# API Gateway HTTP API and proxy integration
# ==============================================================================

resource "aws_apigatewayv2_api" "http_api" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"
  description   = "HTTP API Gateway proxy for the Ambrace FastAPI backend"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.http_api.id
  integration_type       = "AWS_PROXY"
  integration_method     = "POST"
  integration_uri        = aws_lambda_function.backend.invoke_arn
  payload_format_version = "2.0"
}

# Every path and method (including CORS preflights) is forwarded to FastAPI,
# which owns routing, authentication and CORS.
resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http_api.id
  name        = "$default"
  auto_deploy = true

  # A ceiling on request rate for the whole API, so a burst cannot run up
  # Lambda and Gemini-token cost. Well above expected MVP traffic.
  default_route_settings {
    throttling_burst_limit = var.api_throttling_burst_limit
    throttling_rate_limit  = var.api_throttling_rate_limit
  }
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*"
}

# ==============================================================================
# API custom domain
# ==============================================================================
# The certificate is requested by infra/bootstrap and validated through a DNS
# record added by hand. This stack only looks it up, and only once it is ISSUED.

data "aws_acm_certificate" "api" {
  count       = local.custom_domain_enabled ? 1 : 0
  domain      = var.custom_domain_name
  statuses    = ["ISSUED"]
  most_recent = true
}

resource "aws_apigatewayv2_domain_name" "api" {
  count       = local.custom_domain_enabled ? 1 : 0
  domain_name = var.custom_domain_name

  domain_name_configuration {
    certificate_arn = data.aws_acm_certificate.api[0].arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "api" {
  count       = local.custom_domain_enabled ? 1 : 0
  api_id      = aws_apigatewayv2_api.http_api.id
  domain_name = aws_apigatewayv2_domain_name.api[0].id
  stage       = aws_apigatewayv2_stage.default.id
}
