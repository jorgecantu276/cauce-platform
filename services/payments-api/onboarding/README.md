# Sandbox tenant onboarding

Copy `tenant.example.json`, replace the example UUIDs and identity-provider subject IDs, then validate it:

```powershell
python scripts/onboard_tenant.py onboarding/tenant.json
```

Apply only after the tenant's Secrets Manager references exist:

```powershell
python scripts/onboard_tenant.py onboarding/tenant.json --apply --table-name "payments-pilot"
```

The manifest holds identifiers, visual tokens, and secret references—never access tokens or webhook secrets. Its branding values are applied to both the staff workspace and the public payment page. It provisions a sandbox connection as unverified on purpose. A provider-verification operation must validate the test seller before checkout is enabled.

After provisioning, verify the sandbox seller using AWS credentials that may read the two scoped Secrets Manager references:

```powershell
python scripts/verify_merchant_connection.py --connection-id "22222222-2222-4222-8222-222222222222" --table-name "payments-pilot"
```
