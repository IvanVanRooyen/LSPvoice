# mock-backend

A synthetic stand-in for the LSPcare backend API, plus two modules the voice agent shares.
It sends no SMS, takes no payment and activates no screen. See
[docs/mock-backend.md](../docs/mock-backend.md) for the contract.

| File | Role |
|---|---|
| `main.py` | FastAPI mock: device lookup (static inventory, then synthetic fixtures), pay-later check, payment links, tickets, invoices. All endpoints except `GET /` need the `MOCK_BEARER` token |
| `vouchers.py` | Synthetic voucher lifecycle (issue, SMS, redeem, pay) with the provisional policy in [business rules](../docs/business-rules.md) |
| `static_inventory.py` | Validated, hospital-scoped inventory lookup (also used by the voice agent) |
| `private_files.py` | Private folder location and owner-only permissions (POSIX modes / Windows ACLs) |
| `run.ps1`, `run.sh` | Run locally on port 8099, optionally behind a quick tunnel |

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m unittest test_static_inventory test_vouchers
powershell -ExecutionPolicy Bypass -File run.ps1 -NoTunnel
```

The voice agent does not call this mock yet. It is the reference for the future backend
integration (account status, vouchers, payment links), which will replace it.
