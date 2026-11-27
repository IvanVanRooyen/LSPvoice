"""Private credentials for the isolated, read-only Twilio comparison pilot."""
import argparse
from getpass import getpass
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import make_private_dir, private_path, restrict

CONFIG = private_path('twilio-pilot.json')
DEFAULT_CLAUDE_MODEL = 'claude-sonnet-5-5'
# Non-secret settings that --set may change, with the values each accepts (None = free text).
SETTINGS = {
    'anthropic_model': None,
    'effort': {'low', 'medium', 'high', 'xhigh', 'max'},
    'thinking': {'adaptive', 'between_tools'},
    'tickets': {'dry_run', 'live', 'off'},
    'transcripts': {'on', 'off'},
    'call_reference': {'on', 'off'},
    'staff_feedback': {'on', 'off'},
    'flows': {'on', 'off'},
    'say_voice': None,
    'ticket_subject_prefix': None,
    'voice': None, 'ttsProvider': {'ElevenLabs', 'Google', 'Amazon'}, 'language': None, 'hints': None,
    'echoPromptFiltering': {'true', 'false'}, 'welcomeGreetingInterruptible': {'none', 'dtmf', 'speech', 'any'},
    'interruptSensitivity': {'high', 'medium', 'low'}, 'ignoreBackchannel': {'true', 'false'},
}


def apply_setting(config, pair):
    key, sep, value = pair.partition('=')
    key, value = key.strip(), value.strip()
    if not sep or key not in SETTINGS:
        raise ValueError('Use --set KEY=VALUE with one of: ' + ', '.join(SETTINGS))
    allowed = SETTINGS[key]
    if allowed is not None and value not in allowed:
        raise ValueError(f'{key} must be one of: ' + ', '.join(sorted(allowed)))
    config[key] = value
    print(f'{key} set to {value!r}.')


def save_config(config, path=None):
    """Write the private config atomically, readable by the current user only."""
    path = Path(path or CONFIG)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Refusing symlink configuration')
    make_private_dir(path.parent)
    temp = path.with_suffix('.json.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        restrict(temp)
        json.dump(config, stream, indent=2)
        stream.write('\n')
    os.replace(temp, path)


def validate_url(value):
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/') or url.port not in (None,443)):
        raise ValueError('Use the public HTTPS base URL only, without a path/query/credentials.')
    return value.rstrip('/')


SERVICE = 'lspcare-twilio-comparison'


class _EdgeConnection(http.client.HTTPSConnection):
    """HTTPS to a chosen Cloudflare edge IP, with TLS and Host for the tunnel's own name."""
    def __init__(self, ip, host):
        super().__init__(host, 443, timeout=8, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        self.sock = self._context.wrap_socket(socket.create_connection((self.ip, 443), self.timeout),
                                              server_hostname=self.host)


def _health_via_edge(host):
    """Reach a new trycloudflare.com tunnel without resolving its name on this PC.

    Windows remembers a failed lookup for several minutes, and a brand-new tunnel name
    fails its first lookup, so a normal check can keep failing after the tunnel is up.
    Cloudflare's edge routes by name, so connecting to the edge IPs of trycloudflare.com
    itself (with full certificate checks for the tunnel's name) reaches the same tunnel.
    """
    if not host.endswith('.trycloudflare.com'):
        return False
    for ip in sorted({a[4][0] for a in socket.getaddrinfo('trycloudflare.com', 443, type=socket.SOCK_STREAM)})[:2]:
        connection = _EdgeConnection(ip, host)
        try:
            connection.request('GET', '/health', headers={'Host': host})
            response = connection.getresponse()
            if response.status == 200 and json.loads(response.read()).get('service') == SERVICE:
                return True
        finally:
            connection.close()
    return False


def wait_until_public(base, seconds=180):
    """Only route calls to a tunnel that is already serving this pilot."""
    deadline = time.monotonic() + seconds
    host = urlsplit(base).hostname
    while True:
        try:
            with urlopen(base + '/health', timeout=5) as response:
                if json.load(response).get('service') == SERVICE:
                    return
        except (OSError, ValueError):
            pass
        try:
            if _health_via_edge(host):
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        if time.monotonic() > deadline:
            raise ValueError(f'{base}/health is not reachable; the number was left unchanged.')
        time.sleep(3)


def point_number(config, client=None):
    """Set the pilot number's own Voice webhook to this run's /voice URL, then read it back."""
    if not config.get('public_base'):
        raise ValueError('No public URL saved yet; run with --public-url first.')
    wait_until_public(config['public_base'])
    if client is None:
        from twilio.rest import Client
        client = Client(config['account_sid'], config['auth_token'])
    numbers = client.incoming_phone_numbers.list(limit=50)
    if config.get('number_sid'):
        numbers = [n for n in numbers if n.sid == config['number_sid']]
    if len(numbers) != 1:
        raise ValueError('Expected exactly one pilot number on the account; set "number_sid" in the private config.')
    number = numbers[0]
    if number.voice_application_sid:
        raise ValueError('This number is routed through a TwiML App, which overrides its webhook; '
                         'change routing in the Twilio Console instead.')
    url = config['public_base'] + '/voice'
    updated = client.incoming_phone_numbers(number.sid).update(voice_url=url, voice_method='POST')
    if (updated.voice_url, updated.voice_method) != (url, 'POST'):
        raise ValueError('Twilio did not confirm the new webhook; check the number in the Console.')
    config['number_sid'] = number.sid  # pin the number for later runs
    print(f'Number ending {number.phone_number[-4:]} now routes calls to {url} (was {number.voice_url}).')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--public-url', help='Update only the public tunnel base URL')
    modes.add_argument('--update-number', action='store_true',
                       help="Point the Twilio number's Voice webhook at the saved public URL (after a health check)")
    modes.add_argument('--use-claude', action='store_true',
                       help='Answer calls with Claude: privately add/replace the Anthropic API key and model')
    modes.add_argument('--use-openai', action='store_true', help='Answer calls with OpenAI again')
    modes.add_argument('--set', action='append', metavar='KEY=VALUE',
                       help='Change a non-secret setting (repeatable): ' + ', '.join(SETTINGS))
    args = parser.parse_args()
    if CONFIG.is_symlink() or CONFIG.parent.is_symlink():
        raise ValueError('Refusing symlink configuration')
    if args.public_url or args.use_claude or args.use_openai or args.update_number or args.set:
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
    if args.set:
        for pair in args.set:
            apply_setting(config, pair)
    elif args.update_number:
        point_number(config)
    elif args.public_url:
        config['public_base'] = validate_url(args.public_url)
    elif args.use_claude:
        print('This saves the key only. No call or paid API request is made.')
        key = getpass('Anthropic API key from the company Claude Console (hidden): ').strip()
        if not key.startswith('sk-ant-'):
            raise ValueError('Enter an Anthropic API key (it starts with sk-ant-).')
        config.update(provider='anthropic', anthropic_api_key=key,
                      anthropic_model=input(f'Claude model [{DEFAULT_CLAUDE_MODEL}]: ').strip() or DEFAULT_CLAUDE_MODEL,
                      effort=input('Effort, low keeps replies quick [low]: ').strip() or 'low')
    elif args.use_openai:
        if not config.get('openai_api_key'):
            raise ValueError('No OpenAI key saved; run a fresh setup instead.')
        config['provider'] = 'openai'
    else:
        if CONFIG.exists():
            raise ValueError('Configuration exists; use --public-url to set its tunnel URL.')
        print('This saves credentials only. No account, purchase, call or paid API request is made.')
        config = {'account_sid': getpass('Twilio Account SID (hidden): ').strip(),
                  'auth_token': getpass('Twilio Auth Token (hidden): ').strip(),
                  'openai_api_key': getpass('OpenAI API key (hidden): ').strip(),
                  'model': input('OpenAI text model [gpt-6.1-terra]: ').strip() or 'gpt-6.1-terra',
                  'public_base': ''}
        if not config['account_sid'].startswith('AC') or not config['auth_token'] or not config['openai_api_key']:
            raise ValueError('Enter the real Twilio Account SID/auth token and OpenAI API key privately.')
    save_config(config)
    print('Private configuration saved outside the repository.')
    print('Calls are answered by:', 'Claude ' + config['anthropic_model'] if config.get('provider') == 'anthropic'
          else 'OpenAI ' + config['model'])
    if config['public_base']:
        print('Twilio incoming-call webhook (POST):', config['public_base'] + '/voice')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        if '--update-number' in sys.argv:
            print('Number update stopped:', error)
        elif '--set' in sys.argv:
            print('Setting not changed:', error)
        else:
            print('Setup stopped. Check the private inputs; no call or API request was made.')
        raise SystemExit(1)
    except Exception as error:  # e.g. TwilioRestException: report without a traceback full of request data
        print('Twilio request failed:', type(error).__name__, getattr(error, 'msg', '') or getattr(error, 'status', ''))
        raise SystemExit(1)
