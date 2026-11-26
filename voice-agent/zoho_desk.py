"""Zoho Desk client for the pilot: refresh-token sign-in and ticket creation.

Uses the private zoho-desk.json written by scripts/setup_zoho_desk.py. No redirects are
followed and nothing is retried automatically, so a lost response is never re-sent.

    python zoho_desk.py --check        read-only: refresh the token, read the saved department
    python zoho_desk.py --test-ticket  create ONE clearly marked [TEST] ticket (commissioning)
"""
import argparse
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import is_private, private_path

CONFIG = private_path('zoho-desk.json')
REGIONS = {'com', 'com.au', 'eu', 'in', 'jp', 'com.cn'}
TIMEOUT = 15


class ZohoError(Exception):
    """sent=True means the ticket request may have reached Zoho (outcome unknown)."""
    def __init__(self, message, sent=False):
        super().__init__(message)
        self.sent = sent


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ZohoError('Unexpected redirect refused; check the Zoho data centre')


class ZohoDesk:
    def __init__(self, path=CONFIG, opener=None, clock=time.monotonic):
        self.path = Path(path)
        self.opener = opener or build_opener(_NoRedirect())
        self.clock = clock
        self.token = None
        self.expires = 0

    def config(self):
        if self.path.is_symlink() or not self.path.is_file() or not is_private(self.path):
            raise ZohoError('Zoho configuration missing or not private')
        config = json.loads(self.path.read_text(encoding='utf-8'))
        region = config.get('region')
        if (region not in REGIONS or config.get('desk_base') != 'https://desk.zoho.' + region
                or config.get('accounts_base') != 'https://accounts.zoho.' + region):
            raise ZohoError('Zoho endpoints do not match the configured data centre')
        for key in ('client_id', 'client_secret', 'refresh_token', 'org_id', 'department_id'):
            if not config.get(key):
                raise ZohoError('Zoho configuration incomplete: run scripts/setup_zoho_desk.py --check')
        return config

    def _call(self, request, sent_on_failure=False):
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                body = json.load(response)
        except HTTPError as error:
            status = error.code
            try:
                detail = json.load(error)
                detail = detail.get('errorCode') or detail.get('message') or ''
            except Exception:
                detail = ''
            error.close()
            # An HTTP error response means Zoho answered: the request was refused, not half-done.
            raise ZohoError(f'Zoho returned HTTP {status} {str(detail)[:120]}'.strip()) from None
        except ZohoError:
            raise
        except (OSError, URLError, ValueError):
            raise ZohoError('No usable response from Zoho', sent=sent_on_failure) from None
        if isinstance(body, dict) and (body.get('error') or body.get('errorCode')):
            raise ZohoError('Zoho rejected the request: ' + str(body.get('errorCode') or body.get('error'))[:120])
        return body

    def access_token(self, config):
        if self.token and self.clock() < self.expires:
            return self.token
        request = Request(config['accounts_base'] + '/oauth/v2/token', method='POST', data=urlencode({
            'grant_type': 'refresh_token', 'refresh_token': config['refresh_token'],
            'client_id': config['client_id'], 'client_secret': config['client_secret']}).encode())
        result = self._call(request)
        if not result.get('access_token'):
            raise ZohoError('Zoho did not return an access token')
        self.token = result['access_token']
        self.expires = self.clock() + max(60, int(result.get('expires_in', 3600)) - 120)
        return self.token

    def _headers(self, config):
        return {'Authorization': 'Zoho-oauthtoken ' + self.access_token(config), 'orgId': str(config['org_id'])}

    def department(self):
        config = self.config()
        request = Request(config['desk_base'] + '/api/v1/departments/' + str(config['department_id']),
                          headers=self._headers(config))
        return self._call(request)

    def create_ticket(self, body):
        """POST one ticket. The department is always the configured one, never model input."""
        config = self.config()
        headers = {**self._headers(config), 'Content-Type': 'application/json'}
        payload = {**body, 'departmentId': str(config['department_id'])}
        request = Request(config['desk_base'] + '/api/v1/tickets', method='POST', headers=headers,
                          data=json.dumps(payload).encode())
        result = self._call(request, sent_on_failure=True)
        if not result.get('id'):
            raise ZohoError('Zoho response had no ticket id', sent=True)
        return {'id': str(result['id']), 'ticket_number': str(result.get('ticketNumber') or result['id'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--test-ticket', action='store_true')
    args = parser.parse_args()
    desk = ZohoDesk()
    if args.check:
        department = desk.department()
        print('Zoho sign-in OK. Tickets go to department:', department.get('name'),
              '| enabled:', department.get('isEnabled'))
        print('Read-only check: nothing was created.')
        return
    from tickets import build_body
    body = build_body({'hospital': 'RAH', 'bed_id': 'TEST', 'device_id': '', 'caller_name': 'Pilot Test',
                       'callback_number': '+61400000000', 'issue_summary': 'Commissioning test ticket. Please delete.',
                       'steps_tried': '', 'urgent': False}, '+61400000000', 'CA-commissioning', None, '[TEST] ')
    created = desk.create_ticket(body)
    print('Test ticket created: #' + created['ticket_number'], '(id ' + created['id'] + '). Check it in Zoho, then delete it.')


if __name__ == '__main__':
    try:
        main()
    except ZohoError as error:
        print('Zoho:', error, '(the outcome is unknown; check Zoho before retrying)' if error.sent else '')
        raise SystemExit(1)
