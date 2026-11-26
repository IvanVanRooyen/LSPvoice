# Confirmed voucher policy — workflow reference

The product owner confirmed on 23 November: payment due 24 hours after voucher issuance (provisional and may change), one-day entertainment, Route A starts on redemption, once per patient per hospital stay, the backend integration owner owns the family-payment integration.

Use explicit pathway steps for eligibility, complete read-backs, full terms/clear yes, exact issued-deadline confirmation, issue, recipient confirmation, SMS receipt/resend and ticket-backed handoff. Retrieve identity/eligibility, voucher identifiers, payment and activation from authoritative backend responses. Conversation history is not proof of entitlement or payment.

Still open: unavailable email/address, final B terms/authorization and real activation mechanics, unused-voucher release, overdue/late-redemption handling and enforcement. Mock behavior is not a production API contract. The current synthetic code reserves at issue and returns review-required for unpaid overdue cases.

This is an internal workflow reference and is excluded from general knowledge upload. Maintain the decision register and explicit terms steps when policy changes; do not rely on retrieval to enforce mandatory ordering.
