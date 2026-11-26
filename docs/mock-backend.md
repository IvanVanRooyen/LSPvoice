# Mock backend contract

`mock-backend/` is a **synthetic test contract, not the real LSPcare API**. It sends no SMS, takes
no payment and activates no screen. State is held in memory and resets on restart. It exists
so the conversation logic can be built and tested before the real backend integration is
available.

## Authentication

Every endpoint except `GET /` (health) requires `Authorization: Bearer <MOCK_BEARER>`. The
runner generates a random token unless `MOCK_BEARER` is set.

## Device lookup

`POST /hospitals/{hospital}/devices/lookup` with `bed_id` and/or `device_id`.

1. **Static inventory** (when `private-config/lspcare-inventory.json` is present): hospital-scoped,
   with aliases (RAH / Royal Adelaide, RNS / RNSH / Royal North Shore). Bed or Device ID is
   enough; if both are given they must agree. It returns location only, never package,
   payment or stay state (`account_state_known: false`).
2. **Synthetic fixtures** for the demo hospitals (General Hospital / GH, Mercy, St Andrews
   Private). Device ID prefixes trigger scenarios: `test-404`, `test-active`, `test-loading`,
   `test-adv-used`, `test-owing`, `test-sms-fail`, `test-invoice-fail`, `test-ticket-fail`,
   `test-no-invoice`.

## Legacy endpoints

`/devices/{id}/advance` (pay-later check: name, valid AU mobile, timezone-aware `pay_by` within
24 h, one per stay), `/payment-links`, `/tickets`, `/invoices/send-sms`. These are simulated
outcomes only.

## Voucher lifecycle (`/mock/vouchers/...`)

Policy: payment due 24 h after **issuance** (provisional), one-day service from Route A
**redemption**, once per patient per stay (see [business rules](business-rules.md)). Each voucher
keeps its policy and original timestamps. Retries and failed events never reset deadlines or
extend service.

| Method / path | Result |
|---|---|
| `POST /mock/stays/resolve` | Resolve a fictional stay reference to patient/stay (fixtures `mock-stay-001`..`004`; General Hospital only) |
| `POST /mock/vouchers/issue` | Validate identity, details/read-back/terms flags, route and recipient; create the benefit (never activates or sends). Same `request_id` + same details returns the same voucher; changed details return 409 |
| `GET /mock/vouchers/{id}` | Current simulated state |
| `POST /mock/vouchers/{id}/sms` | Initial send + one resend; fake gateway acceptance, never delivery confirmation |
| `POST /mock/vouchers/{id}/redeem` | Simulated redemption event |
| `POST /mock/vouchers/{id}/payment` | Simulated payment event (Route B activates on payment) |

After the payment deadline, status returns `policy_review_required` and `service_active: null`.
Late-payment behaviour is deliberately unresolved (DEC-11). No real patient or stay
identification is implied: stay references are simulated backend identifiers, not names or
phone numbers.
