# Payment review retry

An owner's **Retry** action records an idempotent request and queues provider-event work. The worker fetches the current payment from the provider and reassesses it against the stored payment identity, checkout context, and charge balance. When the payment is approved and safely allocatable, one DynamoDB transaction clears the review, allocates the payment, updates the balance, records an audit item, and queues one `payment_approved` notification. Replaying the work does not queue another approval.

A payment review stays open when the provider still reports a mismatch, the snapshot is stale, the charge cannot cover the payment, or an adjustment or prior allocation makes automatic allocation unsafe. A confirmed provider identity or ownership conflict moves the retry's provider event to terminal review, removes it from automatic claiming, and leaves the original payment review open without a financial write. Temporary provider fetch and transaction failures stay retryable.

Moto tests cover these paths and transaction rollback. Real DynamoDB concurrency has not been qualified. This change is not deployed.
