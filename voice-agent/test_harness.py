"""Offline tests for the harness, the shared conversation engine and live transcripts. No API calls."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app
import claude_reply
import harness
from private_files import is_private
import test_pilot
from test_claude import CLAUDE_CFG, FakeClient, FakeStream, tool_use
import tickets
import transcripts


def usage(i=0, o=0, r=0, w=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=r, cache_creation_input_tokens=w)


class MeterTests(unittest.TestCase):
    def test_cost_at_list_prices_and_budget(self):
        meter = harness.Meter(0.01)
        meter.add('claude-sonnet-5-5', usage(i=1000, o=200, r=4000, w=0))   # 0.002 + 0.002 + 0.0004
        self.assertAlmostEqual(meter.cost, 0.0044)
        meter.add('claude-haiku-5-5', usage(i=10000, o=1000))               # 0.001 + 0.0005
        self.assertAlmostEqual(meter.cost, 0.0059)
        meter.check()
        meter.add('mystery-model', usage(o=1000))                            # costed at the dearest rate: 0.02
        self.assertIn('mystery-model', meter.unpriced)
        with self.assertRaises(harness.BudgetExceeded):
            meter.check()


class HardCheckTests(unittest.TestCase):
    def run_record(self, **extra):
        return {'transcript': [('agent', 'Please press the orange button once.'), ('caller', 'Done')],
                'tools': [{'name': 'get_guide', 'args': {'guide_id': 'KB-TERMINAL-LSPC15MF'}, 'result': {'found': True}},
                          {'name': 'create_ticket', 'args': {'bed_id': '7G13101', 'issue_summary': 'black'},
                           'result': {'ticket_logged': True, 'ticket_number': 'DRY-1'}}],
                'finish': None, 'turns': [], 'error': None, **extra}

    def test_all_kinds_of_check(self):
        expect = {'tools_called': [{'name': 'get_guide', 'args': {'guide_id': 'kb-terminal-lspc15mf'}}],
                  'tools_not_called': [{'name': 'get_guide', 'args': {'guide_id': 'KB-TERMINAL-TOUCH-KEYS'}}],
                  'ticket': 'created', 'ticket_args': {'bed_id': '7g13101'}, 'finish': 'none',
                  'said_any': [['ORANGE BUTTON', 'nothing']], 'said_none': ['unplug']}
        checks = harness.hard_checks(expect, self.run_record())
        self.assertTrue(all(c['passed'] for c in checks), [c for c in checks if not c['passed']])
        failing = harness.hard_checks({'finish': 'clinical', 'said_none': ['orange']}, self.run_record())
        self.assertEqual([c['passed'] for c in failing][:2], [False, False])

    def test_card_numbers_in_tool_args_always_fail(self):
        record = self.run_record()
        record['tools'][1]['args']['issue_summary'] = 'refund to 4111 1111 1111 1111'
        card = [c for c in harness.hard_checks({}, record) if c['check'] == 'no card numbers passed to tools'][0]
        self.assertFalse(card['passed'])

    def test_markdown_in_spoken_reply_fails(self):
        for text in ['Tap **Click to Pay**.', 'Steps:\n1. Tap Click to Pay', '- tap the house']:
            record = self.run_record(transcript=[('agent', text)])
            style = [c for c in harness.hard_checks({}, record) if c['check'].startswith('spoken style')][0]
            self.assertFalse(style['passed'], text)
        clean = [c for c in harness.hard_checks({}, self.run_record()) if c['check'].startswith('spoken style')][0]
        self.assertTrue(clean['passed'])

    def test_errors_fail_the_scenario(self):
        checks = harness.hard_checks({}, self.run_record(error='RuntimeError: boom'))
        self.assertFalse(checks[0]['passed'])


class ScenarioRunTests(unittest.TestCase):
    def test_scripted_scenario_runs_through_real_engine_with_dry_run_tickets(self):
        args = {'hospital': 'RAH', 'bed_id': '7G13101', 'device_id': '', 'caller_name': 'Sam Taylor',
                'callback_number': '0400 123 456', 'issue_summary': 'Arm keeps dropping.', 'steps_tried': '',
                'urgent': False, 'caller_confirmed': True}
        fake = FakeClient([FakeStream("Please don't try to adjust it. Can I take your name and number?"),
                           FakeStream(content=[tool_use('create_ticket', args)]), FakeStream('Logged.'),
                           FakeStream(content=[tool_use('finish_call', {'reason': 'goodbye'}, 'toolu_9')])])
        scenario = {'id': 'T-1', 'title': 't', 'caller': {'script': ['Arm dropping, RAH 7G13101', 'Sam Taylor 0400 123 456, yes', 'Bye']}}
        meter = harness.Meter(1)
        with tempfile.TemporaryDirectory() as folder, patch.object(tickets, 'DRY_RUN_LOG', Path(folder) / 't.jsonl'), \
                patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
            cfg = {**CLAUDE_CFG, 'tickets': 'dry_run', 'flows': 'off'}  # direct ticket path
            run = asyncio.run(harness.run_scenario(scenario, cfg, meter, None, 'CA-test'))
            self.assertTrue(Path(folder, 't.jsonl').exists())
        self.assertIsNone(run['error']); self.assertEqual(run['finish'], 'goodbye')
        self.assertTrue(run['tools'][0]['result']['dry_run'])
        speakers = [s for s, _ in run['transcript']]
        self.assertEqual(speakers[:3], ['agent', 'caller', 'agent'])
        self.assertEqual(len(run['turns']), 4 - 1)  # 3 caller turns; the ticket and 'Logged.' share one turn
        checks = harness.hard_checks({'ticket': 'created', 'ticket_args': {'bed_id': '7G13101'}, 'finish': 'goodbye'}, run)
        self.assertTrue(all(c['passed'] for c in checks))

    def test_tool_results_stay_in_history_for_later_turns(self):
        from conversation import Conversation
        lookup_args = {'hospital': 'RAH', 'bed_id': '7G13101', 'device_id': ''}
        fake = FakeClient([FakeStream(content=[tool_use('lookup_device', lookup_args)]), FakeStream('Found bed 7G13101.'),
                           FakeStream('Is the screen completely black?')])
        async def run_tool(name, args): return {'device_found': True}
        async def on_text(_): pass
        convo = Conversation(CLAUDE_CFG, lambda: 'system', lambda: app.tool_specs(CLAUDE_CFG), run_tool, on_text)
        async def two_turns():
            await convo.respond('Royal Adelaide, bed 7G13101.')
            await convo.respond('The screen is broken.')
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake):
            asyncio.run(two_turns())
        sent = fake.requests[2]['messages']  # the second caller turn's request
        self.assertEqual([m['role'] for m in sent], ['user', 'assistant', 'user', 'assistant', 'user'])
        self.assertEqual(sent[1]['content'][0], {'type': 'tool_use', 'id': 'toolu_1', 'name': 'lookup_device', 'input': lookup_args})
        self.assertEqual(sent[2]['content'][0]['type'], 'tool_result')
        self.assertEqual(sent[3]['content'], 'Got it. Found bed 7G13101.')  # the holding line is part of what was said
        convo.interrupted('Is the screen')  # repair still targets the spoken text turn
        self.assertEqual(convo.history[-1], {'role': 'assistant', 'content': 'Is the screen'})

    def test_scenarios_file_is_valid(self):
        data = json.loads(harness.SCENARIOS.read_text(encoding='utf-8'))
        ids = [s['id'] for s in data['scenarios']]
        self.assertEqual(len(ids), len(set(ids)))
        allowed = {'tools_called', 'tools_not_called', 'finish', 'ticket', 'ticket_args', 'said_any', 'said_none'}
        registry = {e['id'] for e in json.loads((app.ROOT / 'knowledge' / 'registry.json').read_text(encoding='utf-8'))['entries']}
        for s in data['scenarios']:
            self.assertTrue(('script' in s['caller']) ^ ('brief' in s['caller']), s['id'])
            self.assertLessEqual(set(s.get('expect', {})), allowed, s['id'])
            self.assertLessEqual(set(s.get('judge_sources', [])), registry, s['id'])
            for spec in s.get('expect', {}).get('tools_called', []) + s.get('expect', {}).get('tools_not_called', []):
                name = spec if isinstance(spec, str) else spec['name']
                self.assertIn(name, {'lookup_device', 'get_guide', 'create_ticket', 'finish_call', 'search_knowledge', 'report_problem'})


class TranscriptTests(unittest.TestCase):
    _base = test_pilot.PilotTests
    post, begin, ws, setup = _base.post, _base.begin, _base.ws, _base.setup

    def test_live_transcript_written_privately_and_printed(self):
        from fastapi.testclient import TestClient
        app.sessions.clear()
        self.sid = 'CA' + '5' * 32
        cfg = {**CLAUDE_CFG, 'transcripts': 'on'}
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'config', return_value=cfg), \
                patch.object(transcripts, 'FOLDER', Path(folder)):
            self.client = TestClient(app.app)
            nonce = self.begin()
            fake = FakeClient([FakeStream(content=[tool_use('lookup_device', {'hospital': 'RAH', 'bed_id': '7G13101', 'device_id': ''})]),
                               FakeStream('Found it.')])
            with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), \
                    patch.object(app, 'lookup', return_value={'device_found': True, 'hospital_code': 'RAH', 'ward': '7G', 'bed_id': '7G13101'}), \
                    patch('sys.stderr', io.StringIO()) as console:
                with self.ws() as ws:
                    self.setup(ws, nonce)
                    ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': 'Royal Adelaide, bed 7G13101.'})
                    while not ws.receive_json().get('last'): pass
            files = list(Path(folder).glob('*.jsonl'))
            self.assertEqual(len(files), 1); self.assertTrue(is_private(files[0]))
            kinds = [json.loads(line)['kind'] for line in files[0].read_text(encoding='utf-8').splitlines()]
        self.assertEqual(kinds[:3], ['started', 'relay', 'greeting'])  # the setup message is logged too
        self.assertIn('caller', kinds); self.assertIn('tool', kinds); self.assertIn('agent', kinds); self.assertEqual(kinds[-1], 'closed')
        shown = console.getvalue()
        self.assertIn('CALLER  │ Royal Adelaide, bed 7G13101.', shown)
        self.assertIn('lookup_device(RAH, bed=7G13101, id=-) → found: RAH ward 7G', shown)
        self.assertIn('AGENT   │ Got it. Found it.', shown)

    def test_follow_reads_new_lines_and_waits_for_partial_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'call.jsonl'
            path.write_bytes('{"kind": "caller", "text": "Bed 7G13101 — black"}\n{"kind": "ag'.encode('utf-8'))
            records, position = transcripts.read_new(path, 0)
            self.assertEqual([r['kind'] for r in records], ['caller'])
            with path.open('ab') as handle:
                handle.write(b'ent", "text": "OK"}\n')
            records, position = transcripts.read_new(path, position)
            self.assertEqual(records, [{'kind': 'agent', 'text': 'OK'}])
            self.assertEqual(transcripts.read_new(path, position), ([], position))

    def test_off_by_default_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(transcripts, 'FOLDER', Path(folder)):
            transcripts.CallTranscript('CA1', False).event('caller', {'text': 'x'})
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
