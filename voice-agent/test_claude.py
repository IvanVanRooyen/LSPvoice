"""Offline tests for the Claude provider path. A fake client stands in for the API: no paid request."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import claude_reply
import tools
import configure
from private_files import is_private
import test_pilot
from test_pilot import CFG

CLAUDE_CFG = {**CFG, 'provider': 'anthropic', 'anthropic_api_key': 'sk-ant-synthetic-never-sent',
              'anthropic_model': 'claude-sonnet-5-5', 'effort': 'low'}


def tool_use(name, args, block_id='toolu_1'):
    return SimpleNamespace(type='tool_use', name=name, input=args, id=block_id)


class FakeStream:
    def __init__(self, text='', content=None, stop_reason=None):
        self.text = text
        self.content = content if content is not None else ([SimpleNamespace(type='text', text=text)] if text else [])
        uses = any(b.type == 'tool_use' for b in self.content)
        self.stop_reason = stop_reason or ('tool_use' if uses else 'end_turn')
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def __aiter__(self): return self.events()
    async def events(self):
        if self.text: yield SimpleNamespace(type='text', text=self.text)
    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self.stop_reason, content=self.content, stop_details=None)


class FakeClient:
    def __init__(self, streams): self.streams = list(streams); self.requests = []; self.beta = self; self.messages = self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def stream(self, **kwargs):
        # Snapshot messages: the loop appends to the same list after this call.
        self.requests.append({**kwargs, 'messages': list(kwargs['messages'])})
        return self.streams.pop(0)


class ClaudeTests(unittest.TestCase):
    _base = test_pilot.PilotTests
    post, begin, ws, setup = _base.post, _base.begin, _base.ws, _base.setup

    def setUp(self):
        app.sessions.clear()
        started = patch.object(app, 'config', return_value=CLAUDE_CFG); started.start(); self.addCleanup(started.stop)
        self.client = TestClient(app.app)
        self.sid = 'CA' + '3' * 32

    def call(self, fake, *prompts):
        nonce = self.begin(); received = []
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake):
            with self.ws() as ws:
                self.setup(ws, nonce)
                for prompt in prompts:
                    ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': prompt})
                    while True:
                        event = ws.receive_json(); received.append(event)
                        if event['type'] == 'end' or event.get('last'): break
        return received

    def test_lookup_tool_then_spoken_answer(self):
        fake = FakeClient([FakeStream(content=[tool_use('lookup_device', {'hospital': 'RAH', 'bed_id': '7G13101', 'device_id': ''})]),
                           FakeStream('Found it. Payment status is unknown.')])
        with patch.object(app, 'lookup', return_value={'device_found': True, 'lookup_source': 'static_inventory'}) as lookup:
            received = self.call(fake, 'Royal Adelaide, bed 7G13101.')
        lookup.assert_called_once_with('RAH', '7G13101', '')
        spoken = ''.join(r['token'] for r in received if r.get('type') == 'text')
        self.assertEqual(spoken, 'Found it. Payment status is unknown.'); self.assertTrue(received[-1]['last'])
        first, second = fake.requests
        self.assertEqual(first['model'], 'claude-sonnet-5-5')
        self.assertEqual(first['output_config'], {'effort': 'low'})
        self.assertEqual(first['fallbacks'], 'default'); self.assertIn('server-side-fallback-2026-07-01', first['betas'])
        self.assertEqual(first['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(first['thinking'], {'type': 'between_tools'})  # thinking off by default on Sonnet 5.5
        self.assertEqual({t['name'] for t in first['tools']}, {'lookup_device', 'finish_call', 'get_guide', 'report_problem', 'start_flow', 'flow_record', 'flow_confirm', 'flow_cancel'})
        self.assertIn('SOURCE KB-PRICES', first['system'])
        result = second['messages'][-1]['content'][0]
        self.assertEqual(result['tool_use_id'], 'toolu_1'); self.assertTrue(json.loads(result['content'])['device_found'])

    def test_clinical_finish_ends_with_fixed_nurse_message(self):
        received = self.call(FakeClient([FakeStream(content=[tool_use('finish_call', {'reason': 'clinical'})])]), 'I have fallen.')
        self.assertEqual(received[-1]['type'], 'end')
        self.assertIn('nurse call button', self.post('/end').text)

    def test_invalid_tool_input_is_refused_not_run(self):
        fake = FakeClient([FakeStream(content=[tool_use('lookup_device', {'hospital': 'RAH'})]), FakeStream('Which bed are you in?')])
        with patch.object(app, 'lookup') as lookup:
            self.call(fake, 'Royal Adelaide.')
        lookup.assert_not_called()
        self.assertTrue(fake.requests[1]['messages'][-1]['content'][0]['is_error'])

    def test_later_turns_replay_text_only_and_skip_empty_interrupted_turns(self):
        fake = FakeClient([FakeStream('Which hospital?'), FakeStream('Thanks.')])
        self.call(fake, 'Hello.', 'Royal Adelaide.')
        # The call details (reference) are a system message after the first caller turn, kept for the call.
        messages = fake.requests[1]['messages']
        self.assertEqual([m['role'] for m in messages], ['user', 'system', 'assistant', 'user'])
        self.assertIn("this call's reference is", messages[1]['content'])
        self.assertEqual([m['content'] for m in messages if m['role'] != 'system'], ['Hello.', 'Which hospital?', 'Royal Adelaide.'])
        history = [{'role': 'user', 'content': 'Hi'}, {'role': 'assistant', 'content': ''}, {'role': 'user', 'content': 'Bed 1'}]
        empty = FakeClient([FakeStream('OK.')])
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=empty):
            import asyncio
            async def on_text(_): pass
            async def run_tool(name, args): return {}
            asyncio.run(claude_reply.reply(CLAUDE_CFG, 'system', history, tools.specs([]), run_tool, on_text))
        self.assertEqual([m['content'] for m in empty.requests[0]['messages']], ['Hi', 'Bed 1'])

    def test_api_error_ends_call_with_generic_message_and_logs_reason(self):
        class Failing(FakeClient):
            def stream(self, **kwargs): raise RuntimeError('credit_balance_exhausted')
        log = io.StringIO()
        with patch('sys.stderr', log):
            received = self.call(Failing([]), 'Royal Adelaide.')
        self.assertEqual(received[-1]['type'], 'end')
        self.assertIn('encountered a problem', self.post('/end').text)
        self.assertIn('credit_balance_exhausted', log.getvalue())

    def test_use_claude_adds_key_privately_and_keeps_twilio_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(CFG), encoding='utf-8')
            with patch.object(configure, 'CONFIG', path), patch('sys.argv', ['configure.py', '--use-claude']), \
                 patch.object(configure, 'getpass', return_value='sk-ant-private-key'), \
                 patch('builtins.input', return_value=''), redirect_stdout(io.StringIO()) as out:
                configure.main()
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(is_private(path)); self.assertNotIn('sk-ant-private-key', out.getvalue())
        self.assertEqual((saved['provider'], saved['anthropic_model'], saved['effort']), ('anthropic', 'claude-sonnet-5-5', 'low'))
        self.assertEqual((saved['auth_token'], saved['openai_api_key']), (CFG['auth_token'], CFG['openai_api_key']))



class NumberRoutingTests(unittest.TestCase):
    """configure.py --update-number: only after a health check, only the one pilot number."""
    def fake(self, numbers):
        updates = []
        class Handle:
            def __init__(self, sid): self.sid = sid
            def update(self, voice_url, voice_method):
                updates.append((self.sid, voice_url, voice_method))
                return SimpleNamespace(voice_url=voice_url, voice_method=voice_method)
        class Numbers:
            def list(self, limit): return numbers
            def __call__(self, sid): return Handle(sid)
        return SimpleNamespace(incoming_phone_numbers=Numbers()), updates

    def number(self, sid='PN1', app_sid=None):
        return SimpleNamespace(sid=sid, phone_number='+61255503624', voice_url='https://old.example/voice',
                               voice_application_sid=app_sid)

    def test_points_single_number_and_pins_it(self):
        client, updates = self.fake([self.number()])
        config = dict(CFG)
        with patch.object(configure, 'wait_until_public'), redirect_stdout(io.StringIO()) as out:
            configure.point_number(config, client)
        self.assertEqual(updates, [('PN1', 'https://pilot.example.invalid/voice', 'POST')])
        self.assertEqual(config['number_sid'], 'PN1'); self.assertIn('3624', out.getvalue())

    def test_unreachable_tunnel_leaves_number_alone(self):
        client, updates = self.fake([self.number()])
        with patch.object(configure, 'wait_until_public', side_effect=ValueError('not reachable')):
            with self.assertRaises(ValueError): configure.point_number(dict(CFG), client)
        self.assertEqual(updates, [])

    def test_ambiguous_or_app_routed_number_is_not_changed(self):
        for numbers in ([self.number('PN1'), self.number('PN2')], [self.number(app_sid='AP1')]):
            client, updates = self.fake(numbers)
            with patch.object(configure, 'wait_until_public'):
                with self.assertRaises(ValueError): configure.point_number(dict(CFG), client)
            self.assertEqual(updates, [])
        client, updates = self.fake([self.number('PN1'), self.number('PN2')])
        with patch.object(configure, 'wait_until_public'), redirect_stdout(io.StringIO()):
            configure.point_number({**CFG, 'number_sid': 'PN2'}, client)
        self.assertEqual(updates[0][0], 'PN2')

    def test_server_keeps_the_number_unless_forced(self):
        quick = {**CFG, 'public_base': 'https://word-word-word.trycloudflare.com'}
        server = SimpleNamespace(sid='PN1', phone_number='+61255503624', voice_url='https://voice.example.org/voice',
                                 voice_application_sid=None)
        client, updates = self.fake([server])
        with patch.object(configure, 'wait_until_public'):
            with self.assertRaises(configure.NumberProtected):  # a PC's run.ps1 cannot take it from the server
                configure.point_number(dict(quick), client)
        self.assertEqual(updates, [])
        with patch.object(configure, 'wait_until_public'), redirect_stdout(io.StringIO()):
            configure.point_number(dict(quick), client, force=True)
            self.assertEqual(updates[-1][1], 'https://word-word-word.trycloudflare.com/voice')
            # Quick tunnel to quick tunnel (normal PC restarts) and anything to a fixed address still work.
            old_quick = SimpleNamespace(**{**vars(server), 'voice_url': 'https://old-old-old.trycloudflare.com/voice'})
            configure.point_number(dict(quick), self.fake([old_quick])[0])
            configure.point_number({**CFG, 'public_base': 'https://voice.example.org'}, self.fake([old_quick])[0])


if __name__ == '__main__':
    unittest.main()
