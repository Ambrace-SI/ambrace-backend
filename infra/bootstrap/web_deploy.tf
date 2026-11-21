# ==============================================================================
# Web app: GitHub deploy role and certificate
# ==============================================================================
#
# The web app (Ambrace-SI/ambrace-webapp) is a static site: files in a private
# S3 bucket, served by CloudFront. Its pipeline gets its own role, separate from
# the backend's, so neither repository can touch what the other deploys.
#
# The role lives here, not in the web repository, for the same reason the
# backend's does: a pipeline must never be able to write its own permissions.

locals {
  # Exactly one subject may assume the role: a job in the web repository that
  # declares this GitHub Environment. No wildcards.
  web_deploy_subject = "${var.web_github_subject_prefix}:environment:${var.github_environment_name}"

  # Named ambrace-github-web-deploy-<env>, outside the ambrace-<env>-* range.
  web_deploy_role_name = "ambrace-github-web-deploy-${var.environment}"

  # Bucket names are global, so the account ID keeps this one unique.
  web_bucket_name = "${local.name_prefix}-web-${local.account_id}"

  # State layout: web/terraform.tfstate, next to backend/ and bootstrap/.
  web_state_prefix = "web"
  web_state_key    = "${local.web_state_prefix}/terraform.tfstate"

  # /ambrace/<env>/web/*          settings the web build reads (written by the
  #                               backend stack: API address, sign-in IDs)
  # /ambrace/<env>/web/hosting/*  where to publish (written by the web stack:
  #                               bucket name, distribution ID)
  web_parameter_path = "${local.ssm_parameter_path}/web"
}

data "aws_iam_policy_document" "web_deploy_trust" {
  statement {
    sid     = "GitHubActionsEnvironmentOnly"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = [var.github_oidc_audience]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.web_deploy_subject]
    }
  }
}

resource "aws_iam_role" "web_deploy" {
  name                 = local.web_deploy_role_name
  description          = "Assumed by GitHub Actions (environment ${var.github_environment_name}) to deploy the Ambrace web app to ${var.environment}."
  assume_role_policy   = data.aws_iam_policy_document.web_deploy_trust.json
  max_session_duration = 3600

  tags = {
    Name = local.web_deploy_role_name
  }
}

# ------------------------------------------------------------------------------
# Terraform state (web/ only)
# ------------------------------------------------------------------------------

data "aws_iam_policy_document" "web_deploy_state" {
  statement {
    sid       = "ListWebStatePrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        local.web_state_prefix,
        "${local.web_state_prefix}/*",
      ]
    }
  }

  statement {
    sid    = "ReadWriteWebState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.state.arn}/${local.web_state_prefix}/*"]
  }

  # Releasing a lock deletes the lock file; the state object is never deleted.
  statement {
    sid       = "ReleaseWebStateLock"
    effect    = "Allow"
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.web_state_prefix}/*.tflock"]
  }
}

resource "aws_iam_role_policy" "web_deploy_state" {
  name   = "terraform-state"
  role   = aws_iam_role.web_deploy.id
  policy = data.aws_iam_policy_document.web_deploy_state.json
}

# ------------------------------------------------------------------------------
# The site: one bucket, CloudFront, and its settings
# ------------------------------------------------------------------------------

data "aws_iam_policy_document" "web_deploy_site" {
  # The one bucket that holds the site. Deliberately no ACL actions: the bucket
  # is private and reached only through CloudFront.
  statement {
    sid    = "SiteBucket"
    effect = "Allow"
    actions = [
      "s3:CreateBucket",
      "s3:DeleteBucket",
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:GetBucketPolicy",
      "s3:PutBucketPolicy",
      "s3:DeleteBucketPolicy",
      "s3:GetBucketPublicAccessBlock",
      "s3:PutBucketPublicAccessBlock",
      "s3:GetBucketOwnershipControls",
      "s3:PutBucketOwnershipControls",
      "s3:GetEncryptionConfiguration",
      "s3:PutEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:GetBucketVersioning",
      "s3:GetBucketTagging",
      "s3:PutBucketTagging",
      # Read by the Terraform AWS provider on every refresh of a bucket.
      "s3:GetBucketAcl",
      "s3:GetBucketCORS",
      "s3:GetBucketWebsite",
      "s3:GetBucketLogging",
      "s3:GetBucketRequestPayment",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetAccelerateConfiguration",
      "s3:GetReplicationConfiguration",
    ]
    resources = ["arn:aws:s3:::${local.web_bucket_name}"]
  }

  statement {
    sid    = "SiteFiles"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["arn:aws:s3:::${local.web_bucket_name}/*"]
  }

  # CloudFront generates the IDs of distributions, access controls and
  # policies, so they cannot be scoped by name. The boundary is the account,
  # which holds a single environment.
  statement {
    sid    = "CloudFront"
    effect = "Allow"
    actions = [
      "cloudfront:CreateDistribution",
      "cloudfront:GetDistribution",
      "cloudfront:GetDistributionConfig",
      "cloudfront:UpdateDistribution",
      "cloudfront:DeleteDistribution",
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
      "cloudfront:ListTagsForResource",
      "cloudfront:CreateInvalidation",
      "cloudfront:GetInvalidation",
      "cloudfront:ListInvalidations",
      "cloudfront:CreateOriginAccessControl",
      "cloudfront:GetOriginAccessControl",
      "cloudfront:GetOriginAccessControlConfig",
      "cloudfront:UpdateOriginAccessControl",
      "cloudfront:DeleteOriginAccessControl",
      "cloudfront:CreateFunction",
      "cloudfront:DescribeFunction",
      "cloudfront:GetFunction",
      "cloudfront:UpdateFunction",
      "cloudfront:PublishFunction",
      "cloudfront:TestFunction",
      "cloudfront:DeleteFunction",
      "cloudfront:CreateResponseHeadersPolicy",
      "cloudfront:GetResponseHeadersPolicy",
      "cloudfront:GetResponseHeadersPolicyConfig",
      "cloudfront:UpdateResponseHeadersPolicy",
      "cloudfront:DeleteResponseHeadersPolicy",
      # The AWS-managed "CachingOptimized" and "CachingDisabled" policies.
      "cloudfront:GetCachePolicy",
      "cloudfront:GetCachePolicyConfig",
    ]
    resources = [
      "arn:aws:cloudfront::${local.account_id}:distribution/*",
      "arn:aws:cloudfront::${local.account_id}:origin-access-control/*",
      "arn:aws:cloudfront::${local.account_id}:function/${local.name_prefix}-*",
      "arn:aws:cloudfront::${local.account_id}:response-headers-policy/*",
      "arn:aws:cloudfront::${local.account_id}:cache-policy/*",
      # AWS-managed policies live in the "aws" account.
      "arn:aws:cloudfront::aws:cache-policy/*",
    ]
  }

  # List actions do not support resource-level scoping.
  statement {
    sid    = "CloudFrontDiscovery"
    effect = "Allow"
    actions = [
      "cloudfront:ListDistributions",
      "cloudfront:ListOriginAccessControls",
      "cloudfront:ListFunctions",
      "cloudfront:ListResponseHeadersPolicies",
      "cloudfront:ListCachePolicies",
    ]
    resources = ["*"]
  }

  # Read-only: the certificate is requested below and only looked up by the
  # web stack.
  statement {
    sid    = "CertificateLookup"
    effect = "Allow"
    actions = [
      "acm:ListCertificates",
      "acm:DescribeCertificate",
      "acm:GetCertificate",
      "acm:ListTagsForCertificate",
    ]
    resources = ["*"]
  }

  # The web build reads its settings; it cannot change them.
  statement {
    sid    = "ReadBuildSettings"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:aws:ssm:${local.aws_region}:${local.account_id}:parameter/${local.web_parameter_path}",
      "arn:aws:ssm:${local.aws_region}:${local.account_id}:parameter/${local.web_parameter_path}/*",
    ]
  }

  statement {
    sid    = "WriteHostingParameters"
    effect = "Allow"
    actions = [
      "ssm:PutParameter",
      "ssm:DeleteParameter",
      "ssm:AddTagsToResource",
      "ssm:RemoveTagsFromResource",
      "ssm:ListTagsForResource",
    ]
    resources = [
      "arn:aws:ssm:${local.aws_region}:${local.account_id}:parameter/${local.web_parameter_path}/hosting/*",
    ]
  }

  statement {
    sid       = "ParameterDiscovery"
    effect    = "Allow"
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "web_deploy_site" {
  name   = "site"
  role   = aws_iam_role.web_deploy.id
  policy = data.aws_iam_policy_document.web_deploy_site.json
}

# ------------------------------------------------------------------------------
# TLS certificate for the app domain
# ------------------------------------------------------------------------------
#
# CloudFront only reads viewer certificates from us-east-1 (N. Virginia). This
# is the one Ambrace resource outside Mumbai, and it is free.
#
# Same pattern as the API certificate: requested here by an admin, validated by
# a DNS record added by hand, and only looked up by the web stack once ISSUED.

resource "aws_acm_certificate" "app" {
  count    = var.app_domain_name != "" ? 1 : 0
  provider = aws.us_east_1

  domain_name       = var.app_domain_name
  validation_method = "DNS"

  tags = {
    Name = var.app_domain_name
  }

  lifecycle {
    create_before_destroy = true
  }
}
