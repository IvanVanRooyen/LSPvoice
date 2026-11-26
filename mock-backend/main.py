"""
LSPcare API mock — just enough to let the Bland pathway run end-to-end.

Not production code. Fixtures are keyed by device_id prefix; in-memory advance
state simulates one stay per hospital/device until the process restarts.

Device ID prefixes that trigger specific scenarios:
    test-ok       happy path (default)
    test-404      device not found
    test-active   device has an active package already
    test-loading  payment currently loading (<5 min)
    test-adv-used earlier pay-later already used this stay
    test-owing    earlier pay-later unpaid

Run:
    pip install -r requirements.txt
    MOCK_BEARER=<random-token> uvicorn main:app --host 127.0.0.1 --port 8099
"""

import os
import random
import re
from threading import Lock
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from static_inventory import StaticInventory, InventoryError
from private_files import private_path

BEARER = os.environ.get("MOCK_BEARER")
BRISBANE = timezone(timedelta(hours=10))
HOSPITALS = {"general hospital", "gh", "mercy hospital", "mercy", "st andrews private"}
# One simulated stay per hospital/device for the life of this process.
ADVANCES: dict[tuple[str, str], dict[str, Any]] = {}
STATE_LOCK = Lock()
INVENTORY = StaticInventory(os.environ.get('LSPCARE_INVENTORY_PATH')
                            or private_path('lspcare-inventory.json'))

app = FastAPI(title="LSPcare mock API", version="0.1.0")


def _auth(authorization: str | None) -> None:
    if not BEARER:
        raise HTTPException(status_code=503, detail="MOCK_BEARER is not configured")
    expected = f"Bearer {BEARER}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="bad bearer token")


def _brisbane_now() -> datetime:
    # AEST, UTC+10, no daylight saving
    return datetime.now(BRISBANE)


def _hospital(value: str | None) -> str:
    # The ASGI server decodes URL paths once. Bland's browser webhook can send
    # an already-escaped variable through another encoding pass (%20 -> %2520),
    # leaving a literal %20 in this parameter. Decode that remaining layer.
    name = " ".join(unquote(value or "").split()).casefold()
    return {"gh": "general hospital", "mercy": "mercy hospital"}.get(name, name)


def _mobile(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not re.fullmatch(r"\+?[\d\s()-]+", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if not raw.startswith("+") and re.fullmatch(r"04\d{8}", digits):
        return "+61" + digits[1:]
    if raw.startswith("+") and re.fullmatch(r"614\d{8}", digits):
        return "+" + digits
    return None


def _deadline(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value or "")
        if parsed.utcoffset() is None:
            return None
        return parsed
    except ValueError:
        return None


def _helpdesk_open() -> bool:
    now = _brisbane_now()
    # Mon-Fri 08:30-17:00 Brisbane
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 8 * 60 + 30 <= minutes < 17 * 60


def _scenario(device_id: str) -> str:
    if not device_id:
        return "ok"
    for prefix in ("test-404", "test-active", "test-loading",
                   "test-adv-used", "test-owing", "test-ok"):
        if device_id.startswith(prefix):
            return prefix.removeprefix("test-")
    return "ok"


# -- 1. Find device -----------------------------------------------------------

class FindDeviceReq(BaseModel):
    hospital: str | None = None
    device_id: str | None = None
    bed_id: str | None = None
    call_id: str | None = None


@app.post("/hospitals/{hospital}/devices/lookup")
def find_device(hospital: str, body: FindDeviceReq,
                authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(authorization)
    try:
        static = INVENTORY.lookup(_hospital(hospital), body.bed_id or '', body.device_id or '',
                                  _hospital(body.hospital) if body.hospital else None)
    except InventoryError:
        raise HTTPException(status_code=503, detail='Static inventory unavailable; use support') from None
    if static is not None:
        return {**static, 'helpdesk_open': _helpdesk_open()}
    device_id = (body.device_id or "").strip()
    bed_id = (body.bed_id or "").strip()
    hospital_key = _hospital(hospital)
    scenario = _scenario(device_id or bed_id)

    if (scenario == "404" or hospital_key not in HOSPITALS
            or not (device_id or bed_id)
            or (body.hospital and _hospital(body.hospital) != hospital_key)):
        return {
            "device_found": False, "device_id": None, "package_active": False,
            "ends_at": None, "payment_pending_minutes": None,
            "advance_used": False, "balance_owing": False,
            "helpdesk_open": _helpdesk_open(),
        }

    base = {
        "device_found": True,
        "lookup_source": "synthetic_fixture",
        "account_state_known": True,
        "device_id": device_id or f"bed-{bed_id.casefold()}",
        "package_active": False,
        "ends_at": None,
        "payment_pending_minutes": None,
        "advance_used": False,
        "balance_owing": False,
        "helpdesk_open": _helpdesk_open(),
    }
    if scenario == "active":
        base["package_active"] = True
        ends = _brisbane_now() + timedelta(hours=18)
        # Portable equivalent of "%a %-d %b, %-I:%M %p" (%-d is glibc-only).
        base["ends_at"] = f"{ends:%a} {ends.day} {ends:%b}, {ends.hour % 12 or 12}:{ends:%M %p}"
    elif scenario == "loading":
        base["payment_pending_minutes"] = 2
    elif scenario == "adv-used":
        base["advance_used"] = True
    elif scenario == "owing":
        base["balance_owing"] = True
    with STATE_LOCK:
        previous = ADVANCES.get((hospital_key, base["device_id"]))
        if previous:
            base["advance_used"] = True
            base["package_active"] = previous["expires"] > _brisbane_now()
            base["ends_at"] = previous["response"]["ends_at"]
    return base


# -- 2. Pay-later check -------------------------------------------------------

class AdvanceReq(BaseModel):
    hospital: str | None = None
    device_id: str | None = None
    bed_id: str | None = None
    contact_name: str | None = None
    mobile: str | None = None
    email: str | None = None
    pay_by: str | None = None
    call_id: str | None = None


@app.post("/devices/{device_id}/advance")
def advance(device_id: str, body: AdvanceReq,
            authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(authorization)
    scenario = _scenario(device_id)

    def decline(reason: str) -> dict[str, Any]:
        return {"approved": False, "reason": reason, "ends_at": None}

    if _hospital(body.hospital) not in HOSPITALS:
        return decline("device_not_found")
    if body.device_id and body.device_id != device_id:
        return decline("device_not_found")
    if not (body.contact_name or "").strip() or not _mobile(body.mobile):
        return decline("missing_contact")
    deadline = _deadline(body.pay_by)
    now = _brisbane_now()
    if deadline is None or not now < deadline <= now + timedelta(hours=24):
        return decline("invalid_pay_by")

    if scenario == "404":
        return {"approved": False, "reason": "device_not_found", "ends_at": None}
    if scenario == "active":
        return {"approved": False, "reason": "package_active", "ends_at": None}
    if scenario == "loading":
        return {"approved": False, "reason": "payment_loading", "ends_at": None}
    if scenario == "adv-used":
        return {"approved": False, "reason": "advance_used", "ends_at": None}
    if scenario == "owing":
        return {"approved": False, "reason": "unpaid_advance", "ends_at": None}

    key = (_hospital(body.hospital), device_id)
    with STATE_LOCK:
        previous = ADVANCES.get(key)
        if previous:
            # Retry of exactly the same call/request reuses the original result.
            if body.call_id and previous["request"] == body.model_dump():
                return previous["response"]
            return decline("advance_used")
        expires = now + timedelta(hours=24)
        response = {"approved": True, "reason": None, "ends_at": expires.isoformat()}
        ADVANCES[key] = {"request": body.model_dump(), "response": response, "expires": expires}
        return response


# -- 3. Payment link by SMS ---------------------------------------------------

class PaymentLinkReq(BaseModel):
    mobile: str
    hospital: str | None = None
    device_id: str | None = None
    pay_by: str | None = None
    advance_granted: str | bool | None = None
    call_id: str | None = None


@app.post("/payment-links")
def payment_link(body: PaymentLinkReq,
                 authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(authorization)
    mobile = _mobile(body.mobile)
    if mobile is None or (body.device_id or "").startswith("test-sms-fail"):
        return {"sms_sent": False, "mobile_last4": None}
    last4 = mobile[-4:]
    return {"sms_sent": True, "mobile_last4": last4}


# -- 4. Log ticket ------------------------------------------------------------

class TicketReq(BaseModel):
    hospital: str | None = None
    device_id: str | None = None
    bed_id: str | None = None
    contact_name: str | None = None
    mobile: str | None = None
    callback_time: str | None = None
    reason: str | None = None
    intent: str | None = None
    call_id: str | None = None
    port_label: str | None = None
    screen_state: str | None = None
    screen_description: str | None = None
    priority: str | None = None


@app.post("/tickets")
def ticket(body: TicketReq,
           authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(authorization)
    if (body.device_id or "").startswith("test-ticket-fail"):
        return {"ticket_logged": False, "ticket_id": None}
    return {"ticket_logged": True, "ticket_id": f"LSP-{random.randint(10000, 99999)}"}


# -- 5. Send invoice by SMS ---------------------------------------------------

class InvoiceReq(BaseModel):
    hospital: str | None = None
    device_id: str | None = None
    invoice_period: str | None = None
    invoice_package: str | None = None
    mobile: str
    call_id: str | None = None


@app.post("/invoices/send-sms")
def invoice(body: InvoiceReq,
            authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(authorization)
    mobile = _mobile(body.mobile)
    if mobile is None or (body.device_id or "").startswith("test-invoice-fail"):
        return {"invoice_sent": False, "invoice_count": 0, "mobile_last4": None}
    last4 = mobile[-4:]
    # Pretend invoice_count = 0 for a specific test device so node 12d can be exercised
    if body.device_id and body.device_id.startswith("test-no-invoice"):
        return {"invoice_sent": False, "invoice_count": 0, "mobile_last4": last4}
    return {"invoice_sent": True, "invoice_count": 1, "mobile_last4": last4}


@app.get("/")
def root() -> dict[str, Any]:
    try:
        inventory = INVENTORY.summary()
    except InventoryError:
        inventory = {'active': False, 'error': 'unavailable'}
    return {"status": "ok", "service": "mock-backend", "voucher_contract": "conversation-v1",
            "helpdesk_open": str(_helpdesk_open()), 'inventory': inventory}


# New explicit synthetic contract; legacy endpoints retained for the existing Agent.
from vouchers import install as install_vouchers
install_vouchers(app, _auth, lambda: _brisbane_now(), _hospital, _mobile)
