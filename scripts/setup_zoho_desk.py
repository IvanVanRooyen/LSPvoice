"""Privately configure Zoho Desk OAuth and inspect departments; never creates tickets."""
import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import is_private, make_private_dir, private_path, restrict

CONFIG = private_path('zoho-desk.json')
SCOPES = 'Desk.tickets.CREATE,Desk.tickets.READ,Desk.contacts.READ,Desk.contacts.CREATE,Desk.organization.READ,Desk.departments.READ'
REGIONS = {
    'com': 'com', 'com.au': 'com.au', 'eu': 'eu', 'in': 'in',
    'jp': 'jp', 'com.cn': 'com.cn',
}


class SetupError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SetupError('Unexpected redirect refused; verify the Zoho data centre.')


def request(url, fields=None, headers=None):
    data = urlencode(fields).encode() if fields is not None else None
    req = Request(url, data=data, headers=headers or {}, method='POST' if data is not None else 'GET')
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        status = error.code
        error.close()
        raise SetupError(f'Zoho returned HTTP {status}; no automatic retry. Check region, scopes and permissions.') from None
    except (OSError, URLError, ValueError):
        raise SetupError('Zoho network/response error; no automatic retry. No ticket was created.') from None
    if isinstance(result, dict) and (result.get('error') or result.get('errorCode')):
        raise SetupError('Zoho rejected the request; check region, grant expiry, scopes and organisation.')
    return result


def validate_config(config):
    suffix = config.get('region')
    if suffix not in REGIONS:
        raise SetupError('Unsupported data centre; verify the official region endpoints before extending this helper.')
    domain = 'desk.zoho.' + suffix
    accounts = 'accounts.zoho.' + suffix
    if config.get('desk_base') != 'https://' + domain or config.get('accounts_base') != 'https://' + accounts:
        raise SetupError('Config endpoints do not match the selected data centre.')


def save(config, initial=False):
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CONFIG.parent.is_symlink() or CONFIG.is_symlink():
        raise SetupError('Refusing a symlink for private configuration.')
    make_private_dir(CONFIG.parent)
    if initial and CONFIG.exists():
        raise SetupError('Configuration exists; use --check. Do not overwrite an existing integration.')
    temp = CONFIG.with_suffix('.json.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        restrict(temp)
        json.dump(config, stream, indent=2)
        stream.write('\n')
    os.replace(temp, CONFIG)


def tokens(config, grant):
    result = request(config['accounts_base'] + '/oauth/v2/token', {
        'client_id': config['client_id'], 'client_secret': config['client_secret'], **grant})
    if not isinstance(result, dict) or not result.get('access_token'):
        raise SetupError('Zoho did not return an access token. No ticket was created.')
    return result


def choose_id(items, prompt, label):
    """Require an explicit listed ID, and let the operator correct blank/invalid input."""
    valid = {str(item['id']) for item in items if item.get('id') is not None}
    if not valid:
        raise SetupError(f'No selectable {label} IDs returned.')
    while True:
        answer = input(prompt + ' (type the numeric ID, or q to stop): ').strip()
        if answer.lower() == 'q':
            raise SetupError('Selection cancelled. No ticket was created.')
        if answer in valid:
            return answer
        if not answer:
            print('Nothing entered. Type the ID printed beside the name above, then press Enter.')
        else:
            print(f'That {label} ID is not in the returned list. Please choose one of the IDs above.')


def configure():
    if CONFIG.exists():
        raise SetupError('Private config already exists; run --check instead.')
    print('Use the data centre of your Zoho Desk account, not your physical location.')
    suffix = input('Desk domain suffix (e.g. com.au, com, eu): ').strip().lower()
    config = {'region': suffix,
              'desk_base': 'https://desk.zoho.' + suffix,
              'accounts_base': 'https://accounts.zoho.' + suffix}
    validate_config(config)
    config['client_id'] = getpass('Self Client ID (hidden): ').strip()
    config['client_secret'] = getpass('Self Client secret (hidden): ').strip()
    print('Now generate a Self Client code using these scopes:')
    print(SCOPES)
    print('Select the correct Desk organisation if prompted. Paste the fresh code below, not into chat.')
    code = getpass('Fresh one-use grant code (hidden): ').strip()
    if not all((config['client_id'], config['client_secret'], code)):
        raise SetupError('All credential fields are required.')
    result = tokens(config, {'grant_type': 'authorization_code', 'code': code})
    if not result.get('refresh_token'):
        raise SetupError('No refresh token returned; do not use an access-token-only setup for unattended calls.')
    config.update(refresh_token=result['refresh_token'], scopes=SCOPES, product_connection_enabled=False)
    save(config, initial=True)
    print('Credentials saved privately outside the repository:', CONFIG)
    print('No ticket created or Bland setting changed. Next run this command with --check.')


def check():
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise SetupError('Private configuration is missing or invalid; run --configure first.')
    if not is_private(CONFIG):
        raise SetupError('Private file permissions are too broad; restrict the config file to your user '
                         '(chmod 600 on Linux; re-run --configure on Windows).')
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    validate_config(config)
    token = tokens(config, {'grant_type': 'refresh_token', 'refresh_token': config['refresh_token']})['access_token']
    headers = {'Authorization': 'Zoho-oauthtoken ' + token}
    organisations = request(config['desk_base'] + '/api/v1/organizations', headers=headers).get('data', [])
    if not organisations:
        raise SetupError('No accessible Desk organisation returned.')
    print('Accessible organisations:')
    for org in organisations:
        print(str(org.get('id')), org.get('companyName') or org.get('portalName'))
    org_id = choose_id(organisations, 'Organisation', 'organisation')
    headers['orgId'] = org_id
    departments = request(config['desk_base'] + '/api/v1/departments', headers=headers).get('data', [])
    print('Returned departments (if the desired one is absent, stop and review access/pagination):')
    for dept in departments:
        print(str(dept.get('id')), dept.get('name'), 'enabled=' + str(dept.get('isEnabled')))
    department_id = choose_id([d for d in departments if d.get('isEnabled') is not False],
                              'Department (choose Support for this integration)', 'department')
    config.update(org_id=org_id, department_id=department_id, last_checked_utc=int(time.time()))
    save(config)
    print('OAuth refresh and organisation/department reads verified. Settings saved privately.')
    print('Ticket submission is NOT connected. No ticket/contact created and no Bland change made.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--configure', action='store_true')
    modes.add_argument('--check', action='store_true')
    args = parser.parse_args()
    configure() if args.configure else check()


if __name__ == '__main__':
    try:
        main()
    except (SetupError, OSError, ValueError, KeyError, EOFError, KeyboardInterrupt) as error:
        print('Stopped:', str(error) if isinstance(error, SetupError) else 'Local setup interrupted or response validation failed.')
        raise SystemExit(1)
