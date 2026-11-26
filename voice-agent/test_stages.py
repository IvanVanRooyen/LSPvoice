"""Offline tests: staged knowledge, create_ticket guards, Zoho client, voice and model settings.

No network: Zoho, Twilio and the AI providers are all fakes. No paid request, no real ticket.
"""
import asyncio
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from xml.etree.ElementTree import fromstring

import app
import claude_reply
import configure
from knowledge import Library, KnowledgeError, LIBRARY
from private_files import restrict
import tickets
import tools
from zoho_desk import ZohoDesk, ZohoError
import test_pilot
from test_claude import CLAUDE_CFG, FakeClient, FakeStream, tool_use

GOOD = {'hospital': 'RAH', 'bed_id': '7G13101', 'device_id': '', 'caller_name': 'Alex Test',
        'callback_number': '0412 345 678', 'issue_summary': 'Screen stays black after the wake step.',
        'steps_tried': 'Pressed the orange button once.', 'urgent': False, 'caller_confirmed': True}


def run(coro):
    return asyncio.run(coro)


class KnowledgeStageTests(unittest.TestCase):
    def test_core_is_inline_guides_are_menu_only(self):
        text = Library().system_text()
        for source in ('KB-PURCHASE', 'KB-PRICES', 'KB-UNKNOWN-HARDWARE', 'KB-ARM-FAULTS'):
            self.assertIn('SOURCE ' + source, text)
        self.assertIn('- KB-TERMINAL-LSPC15MF: Use only when the caller confirms an orange button', text)
        self.assertIn('- KB-TERMINAL-TOUCH-KEYS:', text)
        guide = (LIBRARY / 'content/terminal-lspc15mf.md').read_text(encoding='utf-8')
        self.assertNotIn(guide.splitlines()[0], text)  # guide body is not sent until fetched
        self.assertNotIn('KB-STAFF-CONTACTS', text); self.assertNotIn('support@lsp.io', text)

    def test_get_guide_by_exact_id_only(self):
        library = Library()
        found = library.get_guide('KB-TERMINAL-LSPC15MF')
        self.assertTrue(found['found']); self.assertIn('orange button', found['text']); self.assertEqual(found['version'], '1.2')
        missing = library.get_guide('KB-STAFF-CONTACTS')
        self.assertFalse(missing['found']); self.assertNotIn('text', missing)
        self.assertEqual(library.guide_ids(), ['KB-TERMINAL-LSPC15MF', 'KB-TERMINAL-TOUCH-KEYS'])

    def synthetic(self, folder, files, delivery):
        root = Path(folder); (root / 'content').mkdir()
        entries = []
        for source_id, text in files.items():
            path = root / 'content' / (source_id + '.md'); path.write_text(text, encoding='utf-8')
            entries.append({'id': source_id, 'version': '1.0', 'path': 'content/' + path.name, 'export': True,
                            'readiness': 'development_ready', 'description': 'Use for ' + source_id,
                            'content_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (root / 'registry.json').write_text(json.dumps({'entries': entries}), encoding='utf-8')
        return Library(root, delivery)

    def test_search_stage_finds_sections_and_enables_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            library = self.synthetic(folder, {
                'KB-CORE': '# Core\nAlways relevant.',
                'KB-MANUAL': '# Terminal manual\nIntro.\n## Bluetooth pairing\nHold nothing; open Settings then Bluetooth.\n'
                             '## Volume\nUse the plus and minus symbols.'},
                {'KB-CORE': 'core', 'KB-MANUAL': 'search'})
            self.assertTrue(library.has_search())
            results = library.search('how do I pair bluetooth headphones')['results']
            self.assertEqual(results[0]['section'], 'Bluetooth pairing'); self.assertEqual(results[0]['source_id'], 'KB-MANUAL')
            self.assertEqual(library.search('"; DROP TABLE s; --')['results'], [])  # query is sanitised, not SQL
            self.assertNotIn('Bluetooth', library.system_text())
            self.assertIn('search_knowledge', {t['name'] for t in tools.specs([], search=True)})

    def test_changed_or_oversized_sources_stop_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            library = self.synthetic(folder, {'KB-CORE': '# Core\nText.'}, {'KB-CORE': 'core'})
            library.system_text()
            (Path(folder) / 'content/KB-CORE.md').write_text('# Core\nEdited without review.', encoding='utf-8')
            with self.assertRaises(KnowledgeError): library.system_text()
        with tempfile.TemporaryDirectory() as folder:
            library = self.synthetic(folder, {'KB-BIG': 'x' * 40001}, {'KB-BIG': 'core'})
            with self.assertRaises(KnowledgeError): library.system_text()

    def test_live_library_matches_registry_copy(self):
        # Guard against the real registry drifting from the pilot's delivery plan.
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / 'knowledge'; shutil.copytree(LIBRARY, copy)
            self.assertEqual(Library(copy).system_text(), Library().system_text())


class TicketRuleTests(unittest.TestCase):
    def test_phone_numbers(self):
        for raw, expected in [('0412 345 678', '+61412345678'), ('+61 412 345 678', '+61412345678'),
                              ('(02) 9876 5432', '+61298765432'), ('61 8 8123 4567', '+61881234567'),
                              ('+1 415 555 0100', None), ('12345', None), ('0912 345 678', None), ('call me', None)]:
            self.assertEqual(tickets.au_phone(raw), expected, raw)

    def test_card_numbers_detected_but_not_phones_or_ids(self):
        self.assertTrue(tickets.has_card_number('my card is 4111 1111 1111 1111'))
        self.assertTrue(tickets.has_card_number('5555-5555-5555-4444'))
        self.assertFalse(tickets.has_card_number('0412 345 678 and bed 7G13101'))
        self.assertFalse(tickets.has_card_number('1234 5678 9012 3456'))  # fails the card checksum

    def test_problems(self):
        self.assertIsNone(tickets.problems(GOOD))
        self.assertTrue(tickets.problems({**GOOD, 'caller_confirmed': False}).startswith('not_confirmed'))
        self.assertIn('callback_number', tickets.problems({**GOOD, 'callback_number': ''}))
        self.assertIn('bed_id or device_id', tickets.problems({**GOOD, 'bed_id': ''}))
        self.assertTrue(tickets.problems({**GOOD, 'callback_number': '+44 20 7946 0000'}).startswith('invalid_callback'))
        self.assertTrue(tickets.problems({**GOOD, 'steps_tried': 'card 4111111111111111'}).startswith('card_number'))

    def test_body_escapes_text_and_records_location_check(self):
        location = {'device_found': True, 'hospital_name': 'Royal Adelaide Hospital', 'ward': '7G',
                    'bed_id': '7G13101', 'device_id': 'D1'}
        body = tickets.build_body({**GOOD, 'issue_summary': '<script>x</script> black screen', 'urgent': True},
                                  '+61412345678', 'CA1', location, '[TEST] ')
        self.assertTrue(body['subject'].startswith('[TEST] RAH 7G13101:'))
        self.assertNotIn('<script>', body['description']); self.assertIn('&lt;script&gt;', body['description'])
        self.assertIn('Verified in inventory: Royal Adelaide Hospital, ward 7G', body['description'])
        self.assertEqual(body['priority'], 'High'); self.assertEqual(body['contact'], {'lastName': 'Alex Test', 'phone': '+61412345678'})
        self.assertNotIn('departmentId', body)  # always added from config by the client
        unverified = tickets.build_body(GOOD, '+61412345678', 'CA1', {'device_found': False}, '')
        self.assertIn('NOT verified', unverified['description']); self.assertNotIn('priority', unverified)


class FakeDesk:
    def __init__(self, error=None): self.calls = []; self.error = error
    def create_ticket(self, body):
        self.calls.append(body)
        if self.error: raise self.error
        return {'id': '9001', 'ticket_number': '1042'}


class TicketCreateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        patcher = patch.object(tickets, 'DRY_RUN_LOG', Path(self.folder.name) / 'dry.jsonl'); patcher.start(); self.addCleanup(patcher.stop)
        self.lookup = lambda h, b, d: {'device_found': False}

    def create(self, state, desk, mode='live', args=GOOD):
        with patch('sys.stderr', io.StringIO()):
            return run(tickets.create(state, args, {'tickets': mode}, desk, self.lookup, 'CA1'))

    def test_dry_run_records_locally_and_never_calls_zoho(self):
        desk = FakeDesk(); state = {}
        first = self.create(state, desk, 'dry_run')
        self.assertTrue(first['ticket_logged'] and first['dry_run']); self.assertTrue(first['ticket_number'].startswith('DRY-'))
        record = json.loads(tickets.DRY_RUN_LOG.read_text(encoding='utf-8'))
        self.assertEqual(record['ticket_number'], first['ticket_number']); self.assertEqual(desk.calls, [])
        again = self.create(state, desk, 'dry_run')
        self.assertTrue(again['already_logged']); self.assertEqual(again['ticket_number'], first['ticket_number'])

    def test_live_creates_once_per_call(self):
        desk = FakeDesk(); state = {}
        self.assertEqual(self.create(state, desk)['ticket_number'], '1042')
        repeat = self.create(state, desk, args={**GOOD, 'issue_summary': 'A different problem.'})
        self.assertTrue(repeat['already_logged']); self.assertEqual(len(desk.calls), 1)

    def test_lost_response_blocks_any_second_attempt(self):
        desk = FakeDesk(ZohoError('No usable response', sent=True)); state = {}
        self.assertTrue(self.create(state, desk)['reason'].startswith('unconfirmed'))
        desk.error = None
        self.assertTrue(self.create(state, desk)['reason'].startswith('unconfirmed'))
        self.assertEqual(len(desk.calls), 1)

    def test_refused_request_may_be_retried(self):
        desk = FakeDesk(ZohoError('Zoho returned HTTP 401')); state = {}
        self.assertTrue(self.create(state, desk)['reason'].startswith('ticket_system_unavailable'))
        desk.error = None
        self.assertEqual(self.create(state, desk)['ticket_number'], '1042')

    def test_guards_stop_before_zoho(self):
        desk = FakeDesk()
        for args in ({**GOOD, 'caller_confirmed': False}, {**GOOD, 'issue_summary': 'charged twice to 4111 1111 1111 1111'}):
            self.assertFalse(self.create({}, desk, args=args)['ticket_logged'])
        self.assertFalse(self.create({}, desk, 'off')['ticket_logged'])
        self.assertEqual(desk.calls, [])


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class FakeOpener:
    def __init__(self, *replies): self.replies = list(replies); self.requests = []
    def open(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception): raise reply
        return FakeResponse(json.dumps(reply).encode())


class ZohoClientTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'zoho-desk.json'
        self.path.write_text(json.dumps({'region': 'com', 'desk_base': 'https://desk.zoho.com',
            'accounts_base': 'https://accounts.zoho.com', 'client_id': 'id', 'client_secret': 'secret',
            'refresh_token': 'refresh', 'org_id': '675072785', 'department_id': '320665000000006907'}), encoding='utf-8')
        restrict(self.path)

    def test_token_reused_and_department_from_config(self):
        opener = FakeOpener({'access_token': 'tok', 'expires_in': 3600}, {'id': '1', 'ticketNumber': '101'},
                            {'id': '2', 'ticketNumber': '102'})
        desk = ZohoDesk(self.path, opener)
        self.assertEqual(desk.create_ticket({'subject': 's', 'departmentId': 'attacker'})['ticket_number'], '101')
        desk.create_ticket({'subject': 's2'})
        token, ticket, _ = opener.requests
        self.assertTrue(token.full_url.endswith('/oauth/v2/token'))
        self.assertEqual(ticket.full_url, 'https://desk.zoho.com/api/v1/tickets')
        self.assertEqual(ticket.get_header('Authorization'), 'Zoho-oauthtoken tok')
        self.assertEqual(ticket.get_header('Orgid'), '675072785')
        self.assertEqual(json.loads(ticket.data)['departmentId'], '320665000000006907')
        self.assertEqual(len(opener.requests), 3)  # one token request for two tickets

    def test_errors_say_whether_the_ticket_may_exist(self):
        refused = HTTPError('https://desk.zoho.com/api/v1/tickets', 422, 'bad', {}, io.BytesIO(b'{"errorCode":"INVALID_DATA"}'))
        with self.assertRaises(ZohoError) as caught:
            ZohoDesk(self.path, FakeOpener({'access_token': 't'}, refused)).create_ticket({})
        self.assertFalse(caught.exception.sent); self.assertIn('INVALID_DATA', str(caught.exception))
        with self.assertRaises(ZohoError) as caught:
            ZohoDesk(self.path, FakeOpener({'access_token': 't'}, URLError('timed out'))).create_ticket({})
        self.assertTrue(caught.exception.sent)
        with self.assertRaises(ZohoError) as caught:
            ZohoDesk(self.path, FakeOpener(URLError('dns'))).create_ticket({})
        self.assertFalse(caught.exception.sent)  # failed at sign-in: nothing was sent

    def test_shared_or_wrong_region_config_refused(self):
        config = json.loads(self.path.read_text(encoding='utf-8'))
        self.path.write_text(json.dumps({**config, 'desk_base': 'https://desk.zoho.eu'}), encoding='utf-8')
        with self.assertRaises(ZohoError): ZohoDesk(self.path, FakeOpener()).config()


class CallIntegrationTests(unittest.TestCase):
    _base = test_pilot.PilotTests
    post, begin, ws, setup = _base.post, _base.begin, _base.ws, _base.setup

    def setUp(self):
        app.sessions.clear()
        self.cfg = dict(CLAUDE_CFG)
        patcher = patch.object(app, 'config', side_effect=lambda: self.cfg); patcher.start(); self.addCleanup(patcher.stop)
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        patcher = patch.object(tickets, 'DRY_RUN_LOG', Path(self.folder.name) / 'dry.jsonl'); patcher.start(); self.addCleanup(patcher.stop)
        from fastapi.testclient import TestClient
        self.client = TestClient(app.app); self.sid = 'CA' + '4' * 32

    def call(self, fake, prompt):
        nonce = self.begin()
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()) as log:
            with self.ws() as ws:
                self.setup(ws, nonce)
                ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': prompt})
                while not ws.receive_json().get('last'): pass
        return log.getvalue()

    def test_voice_twiml_uses_australian_elevenlabs_voice(self):
        relay = fromstring(self.post('/voice').text).find('Connect/ConversationRelay')
        self.assertEqual((relay.get('language'), relay.get('ttsProvider'), relay.get('voice')),
                         ('en-AU', 'ElevenLabs', '56bWURjYFHyYyVf490Dp'))
        self.assertIn('RNSH', relay.get('hints'))
        app.sessions.clear(); self.cfg['voice'] = 'otherVoiceId1234567890'
        self.assertEqual(fromstring(self.post('/voice').text).find('Connect/ConversationRelay').get('voice'), 'otherVoiceId1234567890')

    def test_guide_fetch_and_ticket_in_one_call_with_timing_log(self):
        fake = FakeClient([
            FakeStream(content=[tool_use('get_guide', {'guide_id': 'KB-TERMINAL-LSPC15MF'})]),
            FakeStream(content=[tool_use('create_ticket', dict(GOOD), 'toolu_2')]),
            FakeStream('Your test ticket is logged.')])
        log = self.call(fake, 'Orange button, still black. Yes, those details are right.')
        guide = json.loads(fake.requests[1]['messages'][-1]['content'][0]['content'])
        self.assertTrue(guide['found']); self.assertIn('orange button', guide['text'])
        ticket = json.loads(fake.requests[2]['messages'][-1]['content'][0]['content'])
        self.assertTrue(ticket['ticket_logged'] and ticket['dry_run'])
        self.assertEqual(app.sessions[self.sid]['ticket']['ticket_number'], ticket['ticket_number'])
        self.assertIn('tools get_guide:KB-TERMINAL-LSPC15MF,create_ticket', log)
        self.assertNotIn('Alex Test', log)  # timing log carries no caller details

    def test_tickets_off_removes_tool(self):
        self.cfg['tickets'] = 'off'
        self.assertNotIn('create_ticket', {t['name'] for t in app.tool_specs(self.cfg)})


class ModelOptionTests(unittest.TestCase):
    def test_sonnet_default_and_between_tools(self):
        self.assertEqual(claude_reply.request_options({}), {'model': 'claude-sonnet-5-5', 'output_config': {'effort': 'low'},
                                                            'thinking': {'type': 'between_tools'}})
        self.assertNotIn('thinking', claude_reply.request_options({'thinking': 'adaptive'}))
        # Models or efforts that can't turn thinking off fall back to adaptive instead of failing.
        self.assertNotIn('thinking', claude_reply.request_options({'anthropic_model': 'claude-opus-5-5'}))
        self.assertNotIn('thinking', claude_reply.request_options({'effort': 'max'}))
        with self.assertRaises(ValueError):
            claude_reply.request_options({'thinking': 'between_tools', 'anthropic_model': 'claude-opus-5-5'})
        with self.assertRaises(ValueError):
            claude_reply.request_options({'thinking': 'between_tools', 'effort': 'max'})

    def test_set_changes_only_allowed_non_secret_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(test_pilot.CFG), encoding='utf-8')
            argv = ['configure.py', '--set', 'tickets=live', '--set', 'anthropic_model=claude-sonnet-5-5']
            with patch.object(configure, 'CONFIG', path), patch('sys.argv', argv), redirect_stdout(io.StringIO()):
                configure.main()
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual((saved['tickets'], saved['anthropic_model']), ('live', 'claude-sonnet-5-5'))
            self.assertEqual(saved['auth_token'], test_pilot.CFG['auth_token'])
            for bad in ('tickets=sometimes', 'auth_token=x', 'nonsense'):
                with self.assertRaises(ValueError): configure.apply_setting(dict(saved), bad)


class ToolValidationTests(unittest.TestCase):
    def test_exact_schema(self):
        specs = tools.specs(['KB-TERMINAL-LSPC15MF'])
        self.assertTrue(tools.valid_input(specs, 'create_ticket', GOOD))
        self.assertFalse(tools.valid_input(specs, 'create_ticket', {**GOOD, 'urgent': 'yes'}))
        self.assertFalse(tools.valid_input(specs, 'create_ticket', {**GOOD, 'extra': ''}))
        self.assertFalse(tools.valid_input(specs, 'get_guide', {'guide_id': 'KB-STAFF-CONTACTS'}))
        for tool in tools.openai_tools(specs):  # OpenAI strict mode: every property required
            self.assertEqual(set(tool['parameters']['required']), set(tool['parameters']['properties']))


if __name__ == '__main__':
    unittest.main()
