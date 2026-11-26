# Business rules and open decisions

What the agent must do, what has been decided, and what is still open. Record each new decision
with date, approving role, exact outcome, source, and the requirements and tests it affects.
Proposals are not approved policy.

## Requirements

| ID | Requirement | Status in the voice agent |
|---|---|---|
| REQ-01 | Identify the caller's location (hospital + bed **or** Device ID) when needed; reuse details already given; claim a match only after lookup | Implemented: needs-first flow, `lookup_device`, harness ID-01..04 |
| REQ-02 | Approved package and price guidance; the caller's screen price wins; no invented discounts | Implemented (core knowledge); harness PRICE-*, BUY-01 |
| REQ-03 | Voucher / pay-later assistance (see voucher rules below) | Not in the voice agent yet; synthetic contract in `mock-backend` |
| REQ-04 | Payment-loading guidance avoids repeat payment; persistent problems escalate | Not yet |
| REQ-05 | Invoice copies follow an approved recipient/identity policy | Not yet (DEC-01) |
| REQ-06 | Ticket-backed follow-up; truthful handling when a ticket or transfer fails | Tickets implemented (Zoho Desk, guarded); transfer to a person not yet |
| REQ-07 | Clinical needs redirect to nurse call / ward staff and end the call | Implemented (fixed final message); harness SAFE-* |
| REQ-08 | Never take card/bank details; no discounts/refunds or unconfirmed activation claims | Implemented in prompt and code; harness TIX-02, NA-* |
| REQ-09 | Approved recording/disclosure and post-call records | Open (DEC-05) |
| REQ-10 | Screen-specific troubleshooting from a hardware matrix; no advice for features a model lacks | Implemented for LSPC15mf and two 21-inch layouts; full matrix pending |
| REQ-11 | Bed lookup from agreed authoritative data | Static, sanitised inventory export; live source pending (DEC-14) |
| REQ-12 | Controlled staff testing with a reviewed improvement process | In progress: transcripts + harness; staff feedback loop next |

## Voucher rules (confirmed 23 November 2026 by the product owner)

| Decision | Rule | Still needed |
|---|---|---|
| DEC-08 | Payment is due 24 hours after the voucher is **issued** (provisional; configurable). The assistance package is one day. Limit: once per patient per hospital stay. | Authoritative issue/redemption timestamps, patient/stay IDs, overdue handling |
| DEC-09 | Route A (patient pays later): entertainment starts when the voucher is **redeemed**. | Which backend event confirms redemption; sending an SMS or opening a link is not redemption |
| DEC-10 | Route B (family/friend pays): handled by the backend integration owner; service activates only after payment. | Endpoint, schema, authentication, payment confirmation, activation contract |

Already specified: no cash advance. Ask the reason, explain both options, agree a specific
payment date, confirm the required details, and get a clear yes to the full terms before
sending. Confirm the recipient. Resend once after checking the number, then log a failed
receipt for support. Report success as separate facts: eligible, voucher issued, SMS
accepted, delivery reported, caller confirms receipt, paid, activated. Never substitute one
for another. The backend is the authority for any financial or state-changing approval.

Open edge case: with issuance at 10:00 and redemption at 12:00, payment is due 10:00 the next
day, but the one-day period would end at 12:00. Decide unredeemed-voucher expiry and
late-payment handling before live use. Don't silently move either clock.

## Open decisions

| ID | Decision | Owner to confirm |
|---|---|---|
| DEC-01 | Invoice recipient mobile and identity policy | Business owner |
| DEC-02 | Real API ownership, schema, methods, authentication, idempotency | Engineering + product owner |
| DEC-03 | Ticket routing and post-call fields (Zoho Desk chosen for the pilot) | Support owner |
| DEC-04 | Support hours, transfer number and callback expectations | Support owner |
| DEC-05 | Recording/transcript policy, AI disclosure, retention and access | Business owner + privacy |
| DEC-06 | Pilot site, channels, success criteria and release authority | Management + product owner |
| DEC-11 | Updated terms, unpaid follow-up and account blocking | Management / support / engineering |
| DEC-12 | Required personal fields, unavailable data, family authorisation and Route B terms | Business owner |
| DEC-13 | SMS retries/idempotency; refusal versus dispute/handoff | Support + engineering |
| DEC-14 | Bed-data source, hardware matrix and update process | ICT / engineering + product owner |
