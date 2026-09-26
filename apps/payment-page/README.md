# Public payment page

Audience: the customer or payer who receives an opaque charge link.

This application is intentionally server-rendered. `services/payments-api/src/payment_page/render.py` owns the HTML/CSS/interaction layer, while `payments/transport.py` validates the link and returns the correct security headers. Keeping it in the same Lambda artifact prevents a static frontend from receiving charge data before the server validates the token.

Live route: `/pay/:token`.

The page can create an idempotent checkout attempt and redirect to Mercado Pago Checkout Pro. It never captures card details and never treats a browser return as proof of payment.
