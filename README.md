# Cauce Platform

Cauce is a multi-tenant collections platform for Mexican businesses. This repository contains only the current Cauce product: three explicit user experiences, one payments API, its deployment template, and current operating documentation.

## Applications

| Application | Audience | Local command | Route |
| --- | --- | --- | --- |
| `apps/business-workspace` | Business owners and staff | `npm run dev -w @cauce/business-workspace` | `/app/:businessId/*` |
| `apps/implementation-console` | Cauce superadmins | `npm run dev -w @cauce/implementation-console` | `/platform/implementacion` |
| `apps/payment-page` | A business's payer | Rendered by the payments API | `/pay/:token` |

The business workspace and implementation console are separate builds because tenant operations and platform administration are different trust boundaries. The public payment page is server-rendered with the API so an opaque charge link can be validated before any financial information is displayed.

## Services

`services/payments-api` is the Python 3.12 AWS Lambda/API Gateway application. It uses DynamoDB, AWS Secrets Manager, scheduled workers, and Mercado Pago Checkout Pro. Infrastructure is declared in `services/payments-api/template.yaml` using AWS SAM.

## Local verification

```powershell
npm ci
npm run typecheck
npm test
npm run build

python -m pip install -r services/payments-api/requirements-dev.txt -r services/payments-api/src/requirements.txt
python -m pytest services/payments-api/tests/unit -q
python -m pytest services/payments-api/tests/integration/test_dynamodb_payment_flow.py services/payments-api/tests/integration/test_import_jobs.py services/payments-api/tests/integration/test_import_retention.py services/payments-api/tests/integration/test_import_apply.py -q
sam validate --lint --template-file services/payments-api/template.yaml
```

Local frontend development uses clearly labelled fixtures when OIDC and the API URL are absent. Production builds do not enable fixtures implicitly.

## Deployment boundary

Deployment is manual through `.github/workflows/deploy-sandbox.yml`. Pushing this repository does not deploy AWS resources. Before dispatching that workflow, configure the documented GitHub variables, secrets, Cognito callback URLs, and AWS OIDC role; see [deployment.md](docs/deployment.md).

## Scope discipline

This repository intentionally excludes old PostgreSQL code and migrations, recovered applications, archived worktrees, build artifacts, and superseded planning documents. The source manifest is in [migration-manifest.md](docs/migration-manifest.md).

