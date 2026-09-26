# Application map

The repository has three sites. Their names describe the audience, not an implementation accident.

## Business workspace

`apps/business-workspace` is the authenticated client portal used by a business owner or staff member. It manages customers and charges, creates payment links, tracks balances and payment evidence, requests refunds, and resolves human-review items. Tenant authorization is enforced by the API.

## Implementation console

`apps/implementation-console` is an authenticated Cauce operations tool. Only the server-confirmed `super_admin` platform role can use it. It validates, previews, applies, and purges onboarding imports. It is not the client portal and is built separately so platform-only code cannot accidentally ship as part of the tenant workspace.

## Public payment page

`apps/payment-page` documents the payer-facing product boundary. Its runtime renderer lives in `services/payments-api/src/payment_page` and is returned by `/pay/:token`. The page reveals one obligation only after server-side token validation and sends card entry to Mercado Pago.

