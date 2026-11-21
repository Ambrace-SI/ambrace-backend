# Bootstrap: Terraform state, GitHub OIDC and deploy role

This stack is the **only** infrastructure applied by hand. It creates the things a
pipeline needs before it can run at all. Everything else (`infra/app`) is applied
by GitHub Actions.

It is applied **once per AWS account**, by an administrator, from a laptop.

| Environment | AWS account    | Var file       | Backend config     | AWS CLI profile |
|-------------|----------------|----------------|--------------------|-----------------|
| dev         | `798299234172` | `dev.tfvars`   | `dev.backend.hcl`  | `ambrace-dev`   |
| prod        | `074259854866` | `prod.tfvars`  | `prod.backend.hcl` | `ambrace-prod`  |

Region: `ap-south-1` (Mumbai).

## What it creates

| Resource | Name | Purpose |
|---|---|---|
| S3 bucket | `ambrace-terraform-state-<account_id>` | Terraform state. Versioned, encrypted (SSE-S3), TLS-only, private, S3-native locking. |
| IAM OIDC provider | `token.actions.githubusercontent.com` | Lets AWS verify tokens issued by GitHub Actions. |
| IAM role | `ambrace-github-deploy-<env>` | Assumed by the pipeline. No access keys exist. |
| IAM policy | `ambrace-<env>-runtime-boundary` | Upper limit on what application (Lambda) roles can ever do. |
| ACM certificate | `api-dev.ambrace.in` / `api.ambrace.in` | TLS for the API custom domain. Issued after a DNS record is added by hand. |
| IAM role | `ambrace-github-web-deploy-<env>` | Assumed by the web app's pipeline (`ambrace-webapp`). Separate from the backend's role. |
| ACM certificate (us-east-1) | `app-dev.ambrace.in` / `app.ambrace.in` | TLS for the web app on CloudFront. Issued after a DNS record is added by hand. |

Nothing here has a standing monthly charge beyond a few KB of S3 storage.

### How the GitHub trust works

GitHub signs a short-lived token for every workflow job. The deploy role trusts
exactly one token subject:

```
repo:Ambrace-SI@357749772/ambrace-backend@1462386177:environment:<development|production>
```

- The numbers are GitHub's permanent organisation and repository IDs (the
  "immutable subject" format used by repositories created after 15 Jul 2026), so
  renaming or re-creating a repository of the same name cannot inherit access.
- `environment:` means only a job that declares that **GitHub Environment** can
  deploy. A branch push, a pull request or a fork gets a different subject and is
  refused. Production approval rules live on the GitHub Environment.
- There are no wildcards.

If the repository is ever transferred or its subject format changes, read the
live prefix and update `github_subject_prefix`:

```powershell
gh api repos/Ambrace-SI/ambrace-backend/actions/oidc/customization/sub --jq .sub_claim_prefix
```

### What the deploy role can and cannot do

See `deploy_permissions.tf`. In short, it can manage Lambda functions, the HTTP
API, DynamoDB tables, log groups, SSM parameters, the Cognito user pool
configuration and Lambda execution roles, **only** for resources named
`ambrace-<env>-*` in its own account.

It cannot: read or change this bootstrap stack's state, modify itself (its name
is deliberately outside the `ambrace-<env>-*` range), create IAM roles without
the runtime boundary, manage Cognito users, or touch anything in the other
account. Granting application code a new AWS capability means editing
`runtime_boundary.tf` and re-applying this stack.

## Prerequisites (once per laptop)

- Terraform `>= 1.11.4`
- AWS CLI v2
- GitHub CLI (`gh`), signed in with admin rights on `Ambrace-SI/ambrace-backend`
- `AdministratorAccess` on the target account through IAM Identity Center

Create the two CLI profiles. Run this once per profile and answer the prompts:

```powershell
aws configure sso --profile ambrace-dev
```

| Prompt | Answer |
|---|---|
| SSO session name | `ambrace` |
| SSO start URL | `https://ssoins-7223d1889d057fde.portal.us-east-1.app.aws` |
| SSO region | `us-east-1` |
| Account | Ambrace Dev (`798299234172`) |
| Role | `AdministratorAccess` |
| Default client region | `ap-south-1` |
| Output format | `json` |

Repeat with `--profile ambrace-prod` and the Ambrace Prod account (`074259854866`).

## First apply in an account

The state bucket is created by this stack, so the first apply keeps state on the
laptop and moves it into the bucket straight afterwards. The commands below are
for **dev**; for prod replace `dev` with `prod` everywhere.

Run from `infra/bootstrap`.

**1. Sign in and confirm you are in the right account**

```powershell
aws sso login --profile ambrace-dev
```

```powershell
aws sts get-caller-identity --profile ambrace-dev --query Account --output text
```

This must print `798299234172` (dev) or `074259854866` (prod). Terraform also
refuses to run against any other account.

**2. Use local state for this one apply**

First check the folder holds no state from another account or an interrupted run:

```powershell
Get-ChildItem terraform.tfstate* -ErrorAction SilentlyContinue
```

- Nothing listed: carry on.
- A `terraform.tfstate` exists from an **interrupted first apply in this same
  account**: keep it. It is the only record of what was already created;
  deleting it makes the next apply fail with "already exists" errors.
- It belongs to the **other** account: step 4 was never finished there. Finish it
  for that account first; never reuse or delete that file.

Then clear only the cached backend settings:

```powershell
Remove-Item -Recurse -Force .terraform -ErrorAction SilentlyContinue
```

```powershell
Copy-Item backend_override.tf.example backend_override.tf
```

```powershell
terraform init
```

**3. Review the plan, then apply exactly that plan**

```powershell
terraform plan -var-file="dev.tfvars" -out="dev.tfplan"
```

Expect only additions (14 resources) and no changes or deletions.

```powershell
terraform apply "dev.tfplan"
```

**4. Move the state into the new bucket**

```powershell
Remove-Item backend_override.tf
```

```powershell
terraform init -migrate-state -backend-config="dev.backend.hcl"
```

Answer `yes` when asked to copy the state. Then confirm nothing drifted and
remove the local copies:

```powershell
terraform plan -var-file="dev.tfvars"
```

This must report "No changes".

```powershell
Remove-Item terraform.tfstate, terraform.tfstate.backup, dev.tfplan -ErrorAction SilentlyContinue
```

**5. Note the outputs**

```powershell
terraform output
```

You need `deploy_role_arn` (next section) and
`api_certificate_validation_records` (see `docs/domains-and-dns.md`).

## Connect GitHub (once per environment)

Create the GitHub Environment and give it the two values the workflows read.
These are plain variables, not secrets: a role ARN is not sensitive, and the role
can only be assumed by this repository's environment.

```powershell
gh api --method PUT repos/Ambrace-SI/ambrace-backend/environments/development
```

```powershell
gh variable set AWS_DEPLOY_ROLE_ARN --repo Ambrace-SI/ambrace-backend --env development --body "arn:aws:iam::798299234172:role/ambrace-github-deploy-dev"
```

```powershell
gh variable set AWS_REGION --repo Ambrace-SI/ambrace-backend --env development --body "ap-south-1"
```

For prod use environment `production` and
`arn:aws:iam::074259854866:role/ambrace-github-deploy-prod`.

Then, in GitHub → Settings → Environments:

- `production`: add **Required reviewers** (at least one person) and restrict
  **Deployment branches** to `release`. Production is only ever deployed from
  the `release` branch.
- `development`: restrict **Deployment branches** to `master` and `Ambrace-*`.
  Without this, a workflow on any branch that declares
  `environment: development` receives the dev deploy role.

The environment names are case-sensitive and must match `github_environment_name`
in the var file and `environment:` in the workflow.

## The web app's pipeline

The web app is a static site: files in a private S3 bucket, served by
CloudFront. Its repository (`Ambrace-SI/ambrace-webapp`) gets its own role,
defined in `web_deploy.tf`, through the same GitHub OIDC provider.

The role can: keep Terraform state under `web/`; manage the one bucket named
`ambrace-<env>-web-<account_id>`; manage CloudFront distributions, access
controls, functions named `ambrace-<env>-*` and response headers policies; look
up certificates; read the build settings under `/ambrace/<env>/web/` (written by
`infra/app`); and write `/ambrace/<env>/web/hosting/*`.

It cannot touch the backend's state, Lambda, DynamoDB, Cognito, IAM or any other
SSM parameter, and the backend's role cannot touch the site.

The app certificate is in `us-east-1` because CloudFront reads viewer
certificates only from there. It is the one Ambrace resource outside Mumbai and
it is free. Add its record at the DNS provider the same way as the API's; the
record is printed by `app_certificate_validation_records`.

Connect the web repository once per environment:

```powershell
gh api --method PUT repos/Ambrace-SI/ambrace-webapp/environments/development
```

```powershell
gh variable set AWS_DEPLOY_ROLE_ARN --repo Ambrace-SI/ambrace-webapp --env development --body "arn:aws:iam::798299234172:role/ambrace-github-web-deploy-dev"
```

```powershell
gh variable set AWS_REGION --repo Ambrace-SI/ambrace-webapp --env development --body "ap-south-1"
```

```powershell
gh variable set AWS_ACCOUNT_ID --repo Ambrace-SI/ambrace-webapp --env development --body "798299234172"
```

For prod use environment `production`, account `074259854866` and
`arn:aws:iam::074259854866:role/ambrace-github-web-deploy-prod`. Apply the same
branch rules as above: `production` only from `release` with a required
reviewer, `development` only from `master` and `Ambrace-*`.

## Using temporary keys instead of a CLI profile

If you paste the access portal's temporary keys into the shell
(`$Env:AWS_ACCESS_KEY_ID`, `$Env:AWS_SECRET_ACCESS_KEY`, `$Env:AWS_SESSION_TOKEN`)
rather than using the `ambrace-<env>` profile, blank the profile in both places
that name it, otherwise Terraform looks for a profile that does not exist:

- plan: add `-var "aws_profile="`
- init with the S3 backend: add `-backend-config="profile="` after
  `-backend-config="<env>.backend.hcl"`

The account check still applies, so keys for the wrong account are refused.
This is how the dev account was first bootstrapped on 20 Nov 2026.

## Changing this stack later

```powershell
aws sso login --profile ambrace-dev
```

```powershell
terraform init -reconfigure -backend-config="dev.backend.hcl"
```

```powershell
terraform plan -var-file="dev.tfvars" -out="dev.tfplan"
```

```powershell
terraform apply "dev.tfplan"
```

Always run `init -reconfigure` with the matching backend file when switching
between dev and prod, so a dev plan can never be written to prod state. Apply to
dev first, then prod.

## If the first apply fails

- **`EntityAlreadyExists` for the OIDC provider:** the account already has a
  provider for `token.actions.githubusercontent.com` (AWS allows one per URL).
  Import it instead of creating a second one:
  `terraform import -var-file="dev.tfvars" aws_iam_openid_connect_provider.github arn:aws:iam::<account_id>:oidc-provider/token.actions.githubusercontent.com`
- **`AccessDenied` mentioning a region:** the organisation region policy only
  allows `ap-south-1` plus the two US regions used by global services.
- **Anything else part-way through:** fix the cause and run plan/apply again with
  the same local `terraform.tfstate`. Do not delete it.

## Checks without AWS access

```powershell
terraform fmt -check -recursive
```

```powershell
terraform test
```

`terraform test` uses a mock provider (`tests/bootstrap.tftest.hcl`); it needs no
credentials and creates nothing. It needs a local-backend init first:
`Copy-Item backend_override.tf.example backend_override.tf; terraform init`, and
delete `backend_override.tf` afterwards.

## Removing it

The state bucket has `prevent_destroy`. Tearing an account down is deliberate:
destroy `infra/app` first, then remove the `prevent_destroy` line, empty the
bucket, and run `terraform destroy` with local state.
