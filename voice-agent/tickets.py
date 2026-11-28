"""create_ticket: the pilot's one write action, guarded in code rather than by the prompt alone.

Rules enforced here:
- caller_confirmed must be true (the model reads the details back first).
- Name, callback number, hospital, bed or Device ID and a summary are required.
- Callback numbers must be Australian; they are normalised to +61.
- Anything that looks like a payment card number is refused, never sent.
- One ticket per call. A repeat request returns the existing ticket number. If Zoho's reply
  is lost after sending, the call is marked "unconfirmed" and no second ticket is attempted.
- The hospital location is re-checked against the inventory; the ticket says whether it matched.
- Mode "dry_run" (the default) writes the ticket locally instead of sending it to Zoho.
"""
import asyncio
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import private_path, restrict
from zoho_desk import ZohoError

DRY_RUN_LOG = private_path('pilot-dry-run-tickets.jsonl')
MODES = ('dry_run', 'live', 'off')
FREE_TEXT = ('hospital', 'bed_id', 'device_id', 'caller_name', 'issue_summary', 'steps_tried')
LIMITS = {'hospital': 80, 'bed_id': 40, 'device_id': 60, 'caller_name': 80,
          'issue_summary': 600, 'steps_tried': 600}


def au_phone(value):
    """Australian mobile or landline -> +61 form, else None."""
    raw = str(value or '').strip()
    if not re.fullmatch(r'\+?[\d\s()-]+', raw):
        return None
    digits = re.sub(r'\D', '', raw)
    if digits.startswith('61') and len(digits) == 11:
        national = '0' + digits[2:]          # +61 4xx..., 61 2xxxx...
    elif raw.startswith('+'):
        return None                          # another country's number
    else:
        national = digits                    # 04xx..., 02 xxxx...
    return '+61' + national[1:] if re.fullmatch(r'0[23478]\d{8}', national) else None


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def has_card_number(text):
    for run in re.findall(r'\d[\d -]{11,22}\d', str(text or '')):
        digits = re.sub(r'\D', '', run)
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def problems(args):
    if args.get('caller_confirmed') is not True:
        return 'not_confirmed'
    if any(has_card_number(args.get(k)) for k in FREE_TEXT + ('callback_number',)):
        return 'card_number_detected'
    missing = [k for k in ('hospital', 'caller_name', 'callback_number', 'issue_summary') if not str(args.get(k, '')).strip()]
    if not (str(args.get('bed_id', '')).strip() or str(args.get('device_id', '')).strip()):
        missing.append('bed_id or device_id')
    if missing:
        return 'missing_details: ' + ', '.join(missing)
    if au_phone(args['callback_number']) is None:
        return 'invalid_callback_number'
    return None


def build_body(args, phone, call_sid, location, subject_prefix):
    """Zoho Desk ticket body. Description is escaped HTML (Desk renders rich text)."""
    clean = {k: ' '.join(str(args.get(k, '')).split())[:LIMITS[k]] for k in FREE_TEXT}
    place = clean['bed_id'] or clean['device_id']
    if location and location.get('device_found'):
        verified = (f"Verified in inventory: {location.get('hospital_name') or location.get('hospital_code')}, "
                    f"ward {location.get('ward')}, bed {location.get('bed_id')}, device {location.get('device_id')}")
    else:
        verified = 'Location NOT verified against the inventory (caller-provided details only)'
    lines = [
        ('Issue', clean['issue_summary']),
        ('Steps already tried', clean['steps_tried'] or 'None recorded'),
        ('Hospital (as given)', clean['hospital']),
        ('Bed', clean['bed_id'] or '-'), ('Device ID', clean['device_id'] or '-'),
        ('Location check', verified),
        ('Caller', clean['caller_name']), ('Callback number', phone),
        ('Urgent', 'Yes' if args.get('urgent') else 'No'),
        ('Source', f'LSPcare voice agent (staff pilot), call {call_sid}'),
    ]
    body = {
        'subject': f"{subject_prefix}{clean['hospital']} {place}: {clean['issue_summary']}"[:250],
        'description': '<br>'.join(f'<b>{escape(k)}:</b> {escape(v)}' for k, v in lines),
        'contact': {'lastName': clean['caller_name'], 'phone': phone},
        'phone': phone,
        'channel': 'Phone',
    }
    if args.get('urgent'):
        body['priority'] = 'High'
    return body


def _write_dry_run(record):
    DRY_RUN_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    new = not DRY_RUN_LOG.exists()
    with DRY_RUN_LOG.open('a', encoding='utf-8') as handle:
        if new:
            restrict(DRY_RUN_LOG)
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')


async def create(state, args, cfg, desk, lookup, call_sid, on_sending=None):
    """Run create_ticket for one call. state is the call's session dict.

    on_sending() is awaited once every check has passed, just before the ticket is written (Zoho Desk
    or the dry-run log): the moment the caller can be told "one moment while I log that"."""
    mode = cfg.get('tickets', 'dry_run')
    if mode not in MODES or mode == 'off':
        return {'ticket_logged': False, 'reason': 'tickets_unavailable'}
    lock = state.setdefault('ticket_lock', asyncio.Lock())
    async with lock:
        previous = state.get('ticket')
        if previous and previous['status'] == 'logged':
            return {'ticket_logged': True, 'already_logged': True, 'ticket_number': previous['ticket_number'],
                    **({'dry_run': True} if previous.get('dry_run') else {})}
        if previous and previous['status'] == 'unconfirmed':
            return {'ticket_logged': False, 'reason': 'unconfirmed'}
        problem = problems(args)
        if problem:
            return {'ticket_logged': False, 'reason': problem}
        phone = au_phone(args['callback_number'])
        location = lookup(args['hospital'], args['bed_id'], args['device_id'])
        body = build_body(args, phone, call_sid, location, cfg.get('ticket_subject_prefix', '[TEST] '))
        if on_sending:
            await on_sending()
        if mode == 'dry_run':
            number = 'DRY-' + secrets.token_hex(3).upper()
            await asyncio.to_thread(_write_dry_run, {'at': datetime.now(timezone.utc).isoformat(),
                                                     'call': call_sid, 'ticket_number': number, 'body': body})
            state['ticket'] = {'status': 'logged', 'ticket_number': number, 'dry_run': True}
            return {'ticket_logged': True, 'ticket_number': number, 'dry_run': True}
        try:
            created = await asyncio.to_thread(desk.create_ticket, body)
        except ZohoError as error:
            print(f'[pilot] ticket failed on {call_sid}: {error}', file=sys.stderr, flush=True)
            if error.sent:
                state['ticket'] = {'status': 'unconfirmed'}
                return {'ticket_logged': False, 'reason': 'unconfirmed'}
            return {'ticket_logged': False, 'reason': 'ticket_system_unavailable'}
        state['ticket'] = {'status': 'logged', 'ticket_number': created['ticket_number']}
        print(f"[pilot] ticket #{created['ticket_number']} logged on {call_sid}", file=sys.stderr, flush=True)
        return {'ticket_logged': True, 'ticket_number': created['ticket_number']}
