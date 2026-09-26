# Sandbox deployment

Repository creation and CI do not deploy anything. AWS changes occur only after a maintainer manually dispatches `Deploy sandbox` in GitHub Actions.

Required repository configuration:

- variable `AWS_REGION`;
- secret `AWS_DEPLOY_ROLE_ARN` for GitHub OIDC;
- secret `PROVIDER_SECRETS_ARN_PATTERN`;
- secret `APPLICATION_SECRET_ARN`;
- workflow inputs for the exact authenticated-app origin, Cognito issuer/client ID, verified SES sender, and SNS alarm topic.

The workflow validates and builds before assuming AWS credentials. It then performs a non-interactive SAM deployment and prints CloudFormation outputs. The business workspace and implementation console remain separate static builds and require hosting plus matching Cognito callback/logout URLs.

Do not dispatch the workflow until AWS account ownership, expected monthly sandbox spend, retention policy, and teardown responsibility are confirmed. Do not connect production Mercado Pago credentials or accept real money as part of sandbox qualification.

