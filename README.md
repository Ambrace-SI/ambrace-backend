# Ambrace Backend

FastAPI service behind the Ambrace AI viva (oral assessment) product. It starts a
viva session, mints a short-lived, constrained Gemini Live token for the browser,
and stores the session and its result. The browser talks to Gemini directly; this
service never proxies audio.

Ported from the Veenoe prototype. The viva logic (prompt, token constraints,
session lifecycle) is unchanged.

## How it runs

| Piece | Choice |
|---|---|
| Runtime | AWS Lambda, Python 3.13, arm64, zip package |
| HTTP | API Gateway HTTP API → Lambda Web Adapter → uvicorn |
| Data | DynamoDB, one table per environment (`ambrace-<env>-sessions`) |
| Secrets | SSM Parameter Store SecureString under `/ambrace/<env>/` |
| Auth | Amazon Cognito access tokens, verified in the app |
| Region | `ap-south-1` (Mumbai), one AWS account per environment |

## Layout

```
app/
  api/            HTTP routes
  core/auth/      Cognito token verification and FastAPI auth dependencies
  core/           settings and SSM runtime configuration
  services/       viva lifecycle, Gemini Live token and prompt construction, user profile
  db/             DynamoDB repositories (sessions, user profile)
infra/
  bootstrap/      applied once per account by an admin (state, GitHub OIDC, deploy role)
  app/            everything else, applied only by GitHub Actions
scripts/          Lambda packaging
tests/            pytest suite (no AWS access needed)
docs/             Gemini Live contract, QA notes, domains and DNS
```

## Authentication status

Protected routes require a Cognito **access token** as `Authorization: Bearer …`.
The token's signature, expiry, issuer, `token_use` and `client_id` are checked.

Each environment has its own Cognito user pool (`infra/app/cognito.tf`). People
sign in with an email and password or with Google, and the web app sends the
resulting access token to this API.

- **Cost:** the pool uses the Lite feature plan (free up to 10,000 monthly
  active users, Google sign-ins included), Cognito's built-in email sender
  (50 emails a day per AWS account) and no MFA text messages.
- **Google credentials:** the deployment creates one SecureString parameter,
  `/ambrace/<env>/google_oauth`, with a placeholder. An operator replaces its
  value with the JSON `{"client_id":"...","client_secret":"..."}` from the
  Google OAuth client and then **runs the deployment again**, which passes the
  values to Cognito. Until then Google sign-in does not work and the deployment
  log shows a warning; email and password sign-in works regardless. A malformed
  value stops the deployment with a clear error. Rotation is the same two steps.
- **Not linked:** the same email used once with Google and once with a password
  is two separate accounts for now.

When run locally without `COGNITO_USER_POOL_ID` and `COGNITO_APP_CLIENT_ID`, the
API fails closed: `/` and `/health` work, every protected route returns `401`.

## User profile and onboarding

After signing in, the web app asks the student for a few details once and
stores them through this API. The user is always the one in the access token;
a profile cannot be read or written for anyone else.

| Route | Purpose |
|---|---|
| `GET /api/v1/me` | Always `200`. Returns `user_id`, `onboarding_complete` and `profile` (`null` until onboarding is done). |
| `PUT /api/v1/me` | Creates or replaces the profile and returns the same shape. |

Profile fields:

- `name`: required, up to 100 characters.
- `account_type`: `student` (the default), `teacher` or `parent`. Teachers and parents have exactly the same permissions (`is_educator`); the type only changes wording.
  - Switching between teacher and parent is always allowed.
  - Switching between student and teacher or parent returns `409`, with a message saying what to do first, while anything depends on the current side. A student must have left their groups first; a teacher or parent must have deleted theirs.
  - Saving a profile also updates the copies of the name (and type) that groups keep for their lists.
- `class_level`: a whole number from 5 to 12. Required for a student, refused for a teacher or parent. Their classes belong to their groups.
- `board`: optional, one of `CBSE`, `ICSE`, `STATE_BOARD` or `OTHER`.
- `school_name` and `school_address`: optional.

Unknown fields and control characters are rejected; an address may contain
line breaks. Profiles saved before account types existed read as students.
Responses carry `Cache-Control: no-store`.

The profile is one item in the sessions table (`pk = USER#<id>`,
`sk = PROFILE`), so it needs no extra table, index or permission. The
`/api/v1/viva/start` request is unchanged: the web app passes the name and
class from the profile.

## Groups (teachers and parents)

A teacher or parent (an "educator") creates groups. Students join a group with its six-character code
or link. Joining is the student's consent to share their results with the
group's owner, so an educator can never add a student directly.

Permissions come from relationships, not a global role: the owner of a group
manages it. This leaves room for schools, with organisation admins, later.

| Route | Who | Purpose |
|---|---|---|
| `POST /api/v1/groups` | educator | Create a group (`name`, optional `class_level`). |
| `GET /api/v1/groups` | educator | My groups, with member count and join code. |
| `GET /api/v1/groups/{id}` | owner | The group and its roster. |
| `PATCH /api/v1/groups/{id}` | owner | Rename. |
| `DELETE /api/v1/groups/{id}` | owner | Delete the group, its code and every membership. |
| `POST /api/v1/groups/{id}/join-code` | owner | Replace the code. The old code stops working at once. |
| `DELETE /api/v1/groups/{id}/members/{student}` | owner | Remove a student. |
| `GET /api/v1/groups/invite/{code}` | signed in | Preview a group before joining, with `already_member`. 20 per minute. |
| `POST /api/v1/groups/join` | student | Join with `{"code": ...}`. Repeating it changes nothing and returns `already_member: true`. 5 per minute. |
| `GET /api/v1/groups/joined` | student | Groups I belong to. No roster and no code. |
| `DELETE /api/v1/groups/joined/{id}` | student | Leave. |

A group that is missing and a group someone else owns both return `404`, so
ids cannot be probed. Limits are 200 students per group and 50 groups per
educator.

Storage stays in the sessions table with no index:

- `GROUP#<id>/META` holds the group itself.
- `GROUP#<id>/MEMBER#<student>` is one row per student in the group.
- `USER#<owner>/OWNS#<id>` lets an educator list their groups.
- `USER#<student>/MEMBEROF#<id>` lets a student list the groups they joined.
- `JOINCODE#<code>/JOINCODE` finds a group from its code.

Both sides of a relationship are written in one transaction. Transactions are
authorised through the item actions the Lambda already has, plus
`dynamodb:BatchGetItem` and `dynamodb:ConditionCheckItem`.

## Set vivas (assignments)

A group's owner sets a viva for one chapter (with optional topics) and an
optional due day. Students in the group see it and start it. The viva is built
on the server from the assignment, so a student cannot change its scope.

| Route | Who | Purpose |
|---|---|---|
| `POST /api/v1/groups/{id}/assignments` | owner | Set a viva: `curriculum_selection` (one chapter) and optional `due_on`. The class must match the group's class, if it has one. |
| `GET /api/v1/groups/{id}/assignments` | owner | The group's vivas, newest first, with `completed_count` (current members who finished) and `average_score` (every result, including students who have since left). |
| `DELETE /api/v1/groups/{id}/assignments/{aid}` | owner | Remove a viva and every result recorded for it. |
| `GET /api/v1/groups/assigned` | student | Vivas set in all my groups: not done first, soonest due first, with my score once done. |
| `POST /api/v1/groups/{id}/assignments/{aid}/start` | student in the group | Start it. Returns the same answer as `/viva/start`, so the browser's voice engine is unchanged. 5 per minute. |

When a viva started from an assignment concludes, the **first** finished
attempt is recorded for the owner: the report only, never the transcript.
Later attempts are the student's own practice. Nothing is recorded once the
viva has been removed or the student has left the group. The browser sends a
conclusion once, so a failure to record is logged
(`event=assignment_result_record_failed`) and never fails the student's own
report.

Storage stays in the sessions table:

- `GROUP#<id>/ASSIGN#<aid>` holds the set viva.
- `GROUP#<id>/RESULT#<aid>#<student>` holds the student's report, for the owner.
- `USER#<student>/DONE#<id>#<aid>` records the completion on the student's side.
- The session itself gains an optional `assignment` reference.

## Results and the class summary

Only the group's owner reads these; anyone else gets 404. They hold the report
only: there is no transcript to return.

| Route | Purpose |
|---|---|
| `GET /api/v1/groups/{id}/assignments/{aid}/results` | Every student against one viva (`done` with the score, or `not_started`), and the average. A student who left after finishing stays, with `in_group: false`. |
| `GET /api/v1/groups/{id}/assignments/{aid}/results/{student}` | That student's report: score, summary, strengths, areas to work on, next steps. 404 until they finish. |
| `GET /api/v1/groups/{id}/summary` | The class at a glance: completion (`completed` of `expected`), average, score bands (0-4, 5-7, 8-10), each viva's average, and each student's average, latest score and overdue count. |

The summary is plain arithmetic (`app/services/class_summary.py`) over the
group's current members, so every number can be checked. A student
`needs_attention` when their average is below 5 or a viva is past its due day
and not done. A viva due before someone joined is never overdue for them.

## Local development

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.13 and the exact
dependency versions recorded in `uv.lock`; there is no separate install step.

```powershell
Copy-Item .env.example .env
```

Fill in `.env`, then:

```powershell
uv run uvicorn app.main:app --reload --port 8080
```

Dependencies are declared in `pyproject.toml`. After changing them, run
`uv lock` and commit `uv.lock`.

`.env.example` points at DynamoDB Local on port 8000. Leave
`AMBRACE_SSM_PARAMETER_PREFIX` unset locally so settings come from `.env`.

## Tests and checks

```powershell
uv run python scripts/build_lambda.py
```

```powershell
uv run pytest
```

The packaging tests inspect the built zip, so build first.

Code quality (the same commands CI runs; versions are locked in `uv.lock`):

```powershell
uv run ruff format .
```

```powershell
uv run ruff check --fix .
```

```powershell
uv run mypy
```

Terraform checks need no AWS credentials:

```powershell
terraform fmt -check -recursive infra
```

```powershell
terraform -chdir=infra/app init -backend=false
```

```powershell
terraform -chdir=infra/app test
```

The same steps run in GitHub Actions on every pull request (`checks.yml`), which
also lints Terraform with TFLint and the workflow files with actionlint. Two
scans are advisory and do not block a merge yet: Trivy (Terraform
misconfigurations) and zizmor (workflow security).

## Deploying

Nothing is deployed from a laptop. GitHub Actions assumes an AWS role through
OIDC; there are no stored AWS keys.

| Workflow | Trigger | Effect |
|---|---|---|
| `checks.yml` | every pull request, pushes to `master` | tests, package, Terraform checks |
| `deploy-dev.yml` | manual (PLAN / APPLY), from `master` or an `Ambrace-*` branch | deploys to the dev account |
| `deploy-prod.yml` | manual (PLAN / APPLY), from the `release` branch only | deploys to the prod account after a reviewer approves |

Nothing deploys on a push or a merge. `_deploy.yml` holds the steps both
pipelines share, so dev and prod are deployed the same way.

**Branches:** work lands on `master` through pull requests and is deployed to
dev by hand. To release, merge `master` into `release` and run
`deploy-prod.yml` from `release`. Production runs the code *and the workflow
files* as they exist on `release`, so a pipeline change on `master` cannot
reach production until it is promoted.

Each deploy runs the tests against the exact artifact, plans, refuses any plan
that deletes or replaces a resource, applies, then smoke-tests the live API
(health, database, 401 on anonymous and forged callers, CORS) and confirms a
second plan is empty.

The one-time account setup is in [infra/bootstrap/README.md](infra/bootstrap/README.md).

### After the first deploy of an environment

1. **Set the real Gemini API key.** Terraform creates
   `/ambrace/<env>/google_api_key` with a placeholder. Replace the value in the
   AWS console (Systems Manager → Parameter Store), keeping type SecureString.
   Lambda reads it at cold start, so redeploy or wait for a new instance.
2. **Add the API DNS record** shown in the deploy log
   ([docs/domains-and-dns.md](docs/domains-and-dns.md)). Until then the API is
   reachable on its default `execute-api` URL.

## Configuration

| Variable | Set by | Purpose |
|---|---|---|
| `DYNAMODB_TABLE_NAME` | Terraform | sessions table |
| `AMBRACE_SSM_PARAMETER_PREFIX` | Terraform | where to read secrets; unset locally |
| `GOOGLE_API_KEY` | SSM (`google_api_key`) | Gemini API key |
| `GEMINI_LIVE_MODEL` | Terraform | Gemini Live model name |
| `VIVA_SESSION_DURATION_MINUTES` | Terraform | 5, 10 or 15 |
| `CORS_ORIGINS` | Terraform | exact browser origins, comma-separated, no wildcards |
| `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` | Terraform (from the user pool it creates) | set together, or both empty (fail closed) |

## Privacy

Candidate audio never reaches this service. Transcripts, prompts and profile
details (name, school, address) are never logged; logs carry event names and
safe metadata only.
