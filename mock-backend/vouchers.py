"""Synthetic voucher lifecycle. Not LSP's production contract or a payment gateway."""

import os
from dataclasses import dataclass, asdict
from datetime import timedelta, timezone
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _spoken_brisbane(moment):
    # Portable equivalent of "%A %-d %B %Y at %-I:%M %p" (%-d is glibc-only).
    local = moment.astimezone(timezone(timedelta(hours=10)))
    return f"{local:%A} {local.day} {local:%B %Y} at {local.hour % 12 or 12}:{local:%M %p} Brisbane time"


@dataclass(frozen=True)
class VoucherPolicy:
    payment_clock: Literal["issuance", "redemption"] = "issuance"
    payment_hours: int = 24
    service_hours: int = 24
    version: str = "mock-voucher-v1-provisional"

    def __post_init__(self):
        if self.payment_clock not in {"issuance", "redemption"}:
            raise ValueError("Unsupported payment clock")
        if self.payment_hours != 24 or self.service_hours != 24:
            raise ValueError("Only the agreed 24-hour/one-day mock policy is supported")


# Authoritative within this fake backend only. Caller names/phones are not identity.
# Move p1/s1 from A101 to A102; new patient p2/s2 can occupy the same A101 fixture.
STAYS = {
    "mock-stay-001": {"patient": "mock-patient-001", "beds": {"A101", "A102"}, "device": "mock-screen-001"},
    "mock-stay-002": {"patient": "mock-patient-002", "beds": {"A101"}, "device": "mock-screen-002"},
    "mock-stay-003": {"patient": "mock-patient-001", "beds": {"A103"}, "device": "mock-screen-003"},
    "mock-stay-004": {"patient": "mock-patient-004", "beds": {"A104"}, "device": "mock-screen-004"},
}


class IssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    hospital: str
    stay_reference: str
    bed_id: str
    route: Literal["A", "B"]
    eligibility_reason: Literal["no_wallet", "family_agreed"]
    reason: str = Field(min_length=1, max_length=1000)
    contact_name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=1000)
    email: str = Field(min_length=1, max_length=254)
    ward: str = Field(min_length=1, max_length=100)
    mobile: str = ""
    payer_name: str = ""
    payer_mobile: str = ""
    details_confirmed: bool
    terms_agreed: bool
    request_id: str = Field(min_length=1, max_length=200)



    @field_validator("request_id")
    @classmethod
    def resolved_request_id(cls, value):
        if "{{" in value or "}}" in value:
            raise ValueError("request_id must be resolved, not a template")
        return value


class StayLookup(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    hospital: str
    bed_id: str = ""
    device_id: str = ""


class SMSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mobile: str
    mobile_confirmed: bool
    deadline_terms_confirmed: bool = False
    confirmed_pay_by: str = ""
    request_id: str = Field(min_length=1, max_length=200)
    # Fake gateway switch; not delivery evidence and never forwarded externally.
    simulate_failure: bool = False



    @field_validator("request_id")
    @classmethod
    def resolved_request_id(cls, value):
        if "{{" in value or "}}" in value:
            raise ValueError("request_id must be resolved, not a template")
        return value


class MockEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True


class VoucherStore:
    def __init__(self, clock, hospital, mobile, policy=None):
        self.clock, self.hospital, self.mobile = clock, hospital, mobile
        self.policy = policy or VoucherPolicy()
        self.lock = Lock()
        self.records, self.claims, self.requests = {}, {}, {}

    def resolve(self, body):
        matches = []
        bed_id = body.bed_id.upper()
        if self.hospital(body.hospital) == "general hospital" and (body.bed_id or body.device_id):
            for reference, stay in STAYS.items():
                if bed_id and bed_id not in stay["beds"]:
                    continue
                aliases = {stay["device"]}
                if body.bed_id:
                    aliases.add("bed-" + bed_id.casefold())
                if body.device_id and body.device_id not in aliases:
                    continue
                matches.append((reference, stay))
        if len(matches) != 1:
            return {"stay_resolved": False, "stay_reference": None, "reason": "stay_unknown_or_ambiguous", "simulation": True}
        reference, stay = matches[0]
        return {"stay_resolved": True, "stay_reference": reference,
                "bed_id": bed_id or sorted(stay["beds"])[0], "device_id": stay["device"], "simulation": True}

    def view(self, record):
        now = self.clock()
        due, end = record["due"], record["end"]
        overdue = due is not None and now > due and record["paid"] is None
        return {
            "voucher_id": record["id"], "route": record["route"],
            "issued_at": record["issued"].isoformat(),
            "pay_by": due.isoformat() if due else None,
            "pay_by_spoken": _spoken_brisbane(due) if due else None,
            "redeemed_at": record["redeemed"].isoformat() if record["redeemed"] else None,
            "paid_at": record["paid"].isoformat() if record["paid"] else None,
            "activated_at": record["activated"].isoformat() if record["activated"] else None,
            "ends_at": end.isoformat() if end else None,
            "service_active": None if overdue else bool(end and now < end),
            "deadline_passed": overdue,
            "policy_review_required": overdue,
            "sms_attempts": len(record["sms"]), "sms_accepted": any(x["sms_accepted"] for x in record["sms"].values()),
            "policy": record["policy"], "simulation": True,
        }

    def issue(self, body):
        payload = body.model_dump()
        hospital = self.hospital(body.hospital)
        # No arbitrary caller-supplied patient identity; stay must be in the fake registry.
        stay = STAYS.get(body.stay_reference)
        if hospital != "general hospital" or not stay or body.bed_id not in stay["beds"]:
            return {"approved": False, "reason": "stay_not_verified", "simulation": True}
        if not body.details_confirmed or not body.terms_agreed:
            return {"approved": False, "reason": "confirmation_required", "simulation": True}
        if "@" not in body.email or "." not in body.email.rsplit("@", 1)[-1]:
            return {"approved": False, "reason": "invalid_email", "simulation": True}
        recipient = self.mobile(body.mobile if body.route == "A" else body.payer_mobile)
        if not recipient or (body.route == "B" and not body.payer_name):
            return {"approved": False, "reason": "missing_recipient", "simulation": True}
        # Route B may omit the patient's mobile; any supplied mobile must be valid.
        if body.mobile and not self.mobile(body.mobile):
            return {"approved": False, "reason": "invalid_mobile", "simulation": True}
        payload["hospital"] = hospital
        with self.lock:
            prior = self.requests.get(body.request_id)
            if prior:
                if prior["payload"] != payload:
                    raise HTTPException(409, "request_id reused for different details")
                return {"approved": True, **self.view(self.records[prior["id"]])}
            identity = (hospital, stay["patient"], body.stay_reference)
            if identity in self.claims:
                return {"approved": False, "reason": "patient_stay_already_used", "simulation": True}
            now = self.clock()
            policy = asdict(self.policy)
            voucher_id = "mock-" + str(uuid4())
            record = {"id": voucher_id, "route": body.route, "issued": now,
                      "due": now + timedelta(hours=policy["payment_hours"]) if policy["payment_clock"] == "issuance" else None,
                      "redeemed": None, "paid": None, "activated": None, "end": None,
                      "policy": policy, "sms": {}, "recipient": recipient, "details": payload}
            self.records[voucher_id] = record
            # Conservative mock reservation at issue; expiry/release rules await business/API decision.
            self.claims[identity] = voucher_id
            self.requests[body.request_id] = {"payload": payload, "id": voucher_id}
            return {"approved": True, **self.view(record)}

    def get(self, voucher_id):
        if voucher_id not in self.records:
            raise HTTPException(404, "unknown mock voucher")
        return self.records[voucher_id]

    def sms(self, voucher_id, body):
        with self.lock:
            record = self.get(voucher_id)
            if not body.deadline_terms_confirmed or body.confirmed_pay_by != (record["due"].isoformat() if record["due"] else None):
                raise HTTPException(422, "full terms and the issued payment deadline must be confirmed")
            if self.view(record)["policy_review_required"]:
                raise HTTPException(409, "overdue policy requires support review")
            mobile = self.mobile(body.mobile)
            if not body.mobile_confirmed or not mobile:
                raise HTTPException(422, "confirmed valid recipient required")
            if not record["sms"] and mobile != record["recipient"]:
                raise HTTPException(422, "initial SMS must use the confirmed issue recipient")
            signature = (mobile, body.simulate_failure, body.confirmed_pay_by)
            if body.request_id in record["sms"]:
                prior = record["sms"][body.request_id]
                if prior["signature"] != signature:
                    raise HTTPException(409, "SMS request_id reused for different details")
                return {k: v for k, v in prior.items() if k != "signature"}
            if len(record["sms"]) >= 2:
                return {"sms_accepted": False, "reason": "resend_limit", "needs_helpdesk": True, "simulation": True}
            failed = body.simulate_failure or os.environ.get("MOCK_VOUCHER_SMS_FAIL") == "1"
            result = {"sms_accepted": not failed, "delivery_confirmed": False,
                      "mobile_last3": mobile[-3:], "sent_at": self.clock().isoformat() if not failed else None, "needs_helpdesk": failed and bool(record["sms"]),
                      "voucher_id": voucher_id, "simulation": True}
            # A corrected, explicitly confirmed number can be used on the one allowed resend.
            record["recipient"] = mobile
            record["sms"][body.request_id] = {**result, "signature": signature}
            return result

    def event(self, voucher_id, event, success):
        with self.lock:
            record = self.get(voucher_id)
            if not success:
                return {"event_accepted": False, **self.view(record)}
            if record[event] is not None:
                return {"event_accepted": True, **self.view(record)}
            # Do not invent expiry/disconnection policy for an unpaid overdue voucher.
            if self.view(record)["policy_review_required"]:
                return {"event_accepted": False, "reason": "overdue_policy_unresolved", **self.view(record)}
            now = self.clock()
            record[event] = now
            policy = record["policy"]
            if event == "redeemed" and policy["payment_clock"] == "redemption" and record["due"] is None:
                record["due"] = now + timedelta(hours=policy["payment_hours"])
            activate = (record["route"] == "A" and event == "redeemed") or (record["route"] == "B" and event == "paid")
            if activate and record["activated"] is None:
                record["activated"] = now
                record["end"] = now + timedelta(hours=policy["service_hours"])
            return {"event_accepted": True, **self.view(record)}


def install(app, auth, clock, hospital, mobile):
    store = VoucherStore(clock, hospital, mobile)
    app.state.vouchers = store

    @app.post("/mock/stays/resolve")
    def resolve(body: StayLookup, authorization: str | None = Header(default=None)):
        auth(authorization)
        return store.resolve(body)

    @app.post("/mock/vouchers/issue")
    def issue(body: IssueRequest, authorization: str | None = Header(default=None)):
        auth(authorization)
        return store.issue(body)

    @app.get("/mock/vouchers/{voucher_id}")
    def status(voucher_id: str, authorization: str | None = Header(default=None)):
        auth(authorization)
        with store.lock:
            return store.view(store.get(voucher_id))

    @app.post("/mock/vouchers/{voucher_id}/sms")
    def sms(voucher_id: str, body: SMSRequest, authorization: str | None = Header(default=None)):
        auth(authorization)
        return store.sms(voucher_id, body)

    @app.post("/mock/vouchers/{voucher_id}/redeem")
    def redeem(voucher_id: str, body: MockEvent, authorization: str | None = Header(default=None)):
        auth(authorization)
        return store.event(voucher_id, "redeemed", body.success)

    @app.post("/mock/vouchers/{voucher_id}/payment")
    def payment(voucher_id: str, body: MockEvent, authorization: str | None = Header(default=None)):
        auth(authorization)
        return store.event(voucher_id, "paid", body.success)
