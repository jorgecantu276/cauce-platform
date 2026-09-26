# Sandbox tenant onboarding

Copy `tenant.example.json`, replace the example UUIDs and identity-provider subject IDs, then validate it:

```powershell
python scripts/onboard_tenant.py onboarding/tenant.json
```

Apply only after the tenant's Secrets Manager references exist:

```powershell
python scripts/onboard_tenant.py onboarding/tenant.json --apply --table-name "payments-pilot"
```

The manifest holds identifiers, visual tokens, and secret references—never access tokens or webhook secrets. Each membership must have a distinct subject ID. Its branding values are applied to both the staff workspace and the public payment page. A first apply creates the business, memberships, sandbox connection, and a table-wide connection ID ownership record in one DynamoDB transaction. The connection starts unverified; a provider-verification operation must validate the test seller before checkout is enabled.

Apply is create-only. Repeating it for an existing tenant or reusing a connection ID owned by another tenant exits with an error and changes nothing, even if the manifest has changed. This prevents a retry from restoring revoked memberships, adding duplicate memberships, or resetting a verified connection. A failed transaction leaves no new tenant items; correct the cause and retry. A manifest can contain at most 97 memberships because DynamoDB limits the transaction to 100 items.

After provisioning, verify the sandbox seller using AWS credentials that may read the two scoped Secrets Manager references:

```powershell
python scripts/verify_merchant_connection.py --connection-id "22222222-2222-4222-8222-222222222222" --table-name "payments-pilot"
```
