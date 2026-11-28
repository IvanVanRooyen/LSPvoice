"""Session authentication, role enforcement and audit logging for the dashboard.

Three static logins (`admin`, `reviewer`, `reader`) with passwords in environment variables
(`DASHBOARD_PASSWORD_ADMIN`, `DASHBOARD_PASSWORD_REVIEWER`, `DASHBOARD_PASSWORD_READER`). The
session cookie is signed with `DASHBOARD_SESSION_SECRET` and carries a generation number read
from a file in the private folder: bumping the file (admin action) invalidates every live cookie.
Host headers are checked against `DASHBOARD_HOSTS` (comma-separated, exact match).

The audit log and the generation file live in the private folder with the other owner-only
state, so they're kept with the call records that reference them.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request

ROLES = ('admin', 'reviewer', 'reader')
ROLE_RANK = {'reader': 0, 'reviewer': 1, 'admin': 2}

SESSION_COOKIE = 'lspvoice_session'
SESSION_SECONDS = 8 * 3600
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_FAILURES = 5

# Resolved from the environment at import time; tests patch these to point into a temp folder.
_PRIVATE_DIR = Path(os.environ.get('LSPCARE_PRIVATE_DIR') or '/data/private')
AUDIT_LOG = _PRIVATE_DIR / 'audit.jsonl'
SESSION_GEN = _PRIVATE_DIR / 'session_generation'

# IP -> [first_seen_monotonic, failures, locked_until_monotonic]; cleared on success.
LOGIN_ATTEMPTS: dict = {}


class AuthConfig:
    def __init__(self, secret: bytes, passwords: dict, allowed_hosts: set):
        self.secret = secret
        self.passwords = passwords
        self.allowed_hosts = allowed_hosts


def read_config() -> AuthConfig:
    """Load the auth config from environment variables. Raises ValueError if anything is missing."""
    secret = os.environ.get('DASHBOARD_SESSION_SECRET', '').encode('utf-8')
    if len(secret) < 16:
        raise ValueError('DASHBOARD_SESSION_SECRET must be at least 16 bytes')
    passwords = {role: os.environ.get(f'DASHBOARD_PASSWORD_{role.upper()}', '') for role in ROLES}
    for role, pw in passwords.items():
        if not pw:
            raise ValueError(f'DASHBOARD_PASSWORD_{role.upper()} must be set')
    hosts = {h.strip() for h in os.environ.get('DASHBOARD_HOSTS', '').split(',') if h.strip()}
    if not hosts:
        raise ValueError('DASHBOARD_HOSTS must be set (comma-separated list of allowed Host headers)')
    return AuthConfig(secret=secret, passwords=passwords, allowed_hosts=hosts)


# ---- Session cookie: base64url(payload JSON) + "." + base64url(HMAC-SHA256) -----------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(data: str) -> bytes:
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def sign_cookie(cfg: AuthConfig, login: str, generation: int, now: Optional[int] = None) -> str:
    now = int(time.time() if now is None else now)
    payload = json.dumps({'login': login, 'iat': now, 'exp': now + SESSION_SECONDS, 'gen': int(generation)},
                         separators=(',', ':'), sort_keys=True).encode('utf-8')
    sig = hmac.new(cfg.secret, payload, hashlib.sha256).digest()
    return _b64url_encode(payload) + '.' + _b64url_encode(sig)


def read_cookie(cfg: AuthConfig, value: str, generation: int, now: Optional[float] = None) -> Optional[str]:
    """Return the login name when the cookie is valid for this generation and not expired, else None."""
    if not value or '.' not in value:
        return None
    try:
        encoded_payload, encoded_sig = value.split('.', 1)
        payload = _b64url_decode(encoded_payload)
        sig = _b64url_decode(encoded_sig)
    except (ValueError, base64.binascii.Error):
        return None
    expected = hmac.new(cfg.secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if int(data.get('gen', -1)) != int(generation):
        return None
    now = int(time.time() if now is None else now)
    if int(data.get('exp', 0)) <= now:
        return None
    login = data.get('login')
    if login not in ROLES:
        return None
    return login


# ---- Password check ------------------------------------------------------------------------

def check_password(cfg: AuthConfig, login: str, password: str) -> bool:
    expected = cfg.passwords.get(login, '')
    if not expected or not password:
        return False
    return secrets.compare_digest(password.encode('utf-8'), expected.encode('utf-8'))


# ---- Rate limiting (per-IP, in-memory) -----------------------------------------------------

def rate_limit_allowed(ip: str, now: Optional[float] = None) -> bool:
    if not ip:
        return True
    now = time.monotonic() if now is None else now
    row = LOGIN_ATTEMPTS.get(ip)
    if not row:
        return True
    start, count, locked_until = row
    if locked_until > now:
        return False
    if now - start > LOGIN_WINDOW_SECONDS:
        LOGIN_ATTEMPTS.pop(ip, None)
        return True
    return count < LOGIN_MAX_FAILURES


def rate_limit_record(ip: str, success: bool, now: Optional[float] = None) -> None:
    if not ip:
        return
    now = time.monotonic() if now is None else now
    if success:
        LOGIN_ATTEMPTS.pop(ip, None)
        return
    row = LOGIN_ATTEMPTS.get(ip)
    if not row or now - row[0] > LOGIN_WINDOW_SECONDS:
        LOGIN_ATTEMPTS[ip] = [now, 1, 0.0]
        return
    row[1] += 1
    if row[1] >= LOGIN_MAX_FAILURES:
        row[2] = now + LOGIN_WINDOW_SECONDS


def client_ip(request: Request) -> str:
    for header in ('cf-connecting-ip', 'x-forwarded-for'):
        value = request.headers.get(header, '')
        if value:
            return value.split(',')[0].strip()
    return request.client.host if request.client else ''


# ---- Session generation (global invalidation) ----------------------------------------------

def current_generation() -> int:
    try:
        return int(SESSION_GEN.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return 1


def bump_generation() -> int:
    new_gen = current_generation() + 1
    SESSION_GEN.parent.mkdir(parents=True, exist_ok=True)
    SESSION_GEN.write_text(str(new_gen), encoding='utf-8')
    try:
        os.chmod(SESSION_GEN, 0o600)
    except OSError:
        pass
    return new_gen


# ---- Audit log -----------------------------------------------------------------------------

def audit(action: str, *, login: str = '', ip: str = '', **detail) -> None:
    """Append one JSON line to the audit log. Never raises: auditing failures must not block work."""
    entry = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'action': action, 'login': login, 'ip': ip, **detail}
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, separators=(',', ':'), ensure_ascii=False) + '\n')
        try:
            os.chmod(AUDIT_LOG, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def recent_audit(limit: int = 200) -> list:
    if not AUDIT_LOG.exists():
        return []
    try:
        lines = AUDIT_LOG.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---- Role check ---------------------------------------------------------------------------

def role_allows(user_role: str, required_role: str) -> bool:
    if required_role not in ROLE_RANK or user_role not in ROLE_RANK:
        return False
    return ROLE_RANK[user_role] >= ROLE_RANK[required_role]


def requires(role: str):
    """FastAPI dependency: 403 if request.state.role isn't at least `role`."""
    async def dep(request: Request):
        current = getattr(request.state, 'role', '')
        if not role_allows(current, role):
            raise HTTPException(403, f'This action needs the {role} role.')
    return Depends(dep)
