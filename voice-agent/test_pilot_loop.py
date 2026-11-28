"""Offline tests for the staff-pilot loop: spoken-text filter, call references, problem reports,
transcript lookup, automatic review conversion and call-to-test drafts. No API calls."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree.ElementTree import fromstring

from fastapi.testclient import TestClient

import app
import claude_reply
from conversation import Conversation, SpokenText
import harness
import pilot_log
from private_files import is_private
import review
import test_pilot
from test_claude import CLAUDE_CFG, FakeClient, FakeStream, tool_use
import tools
import transcripts


def spoken(*chunks):
    f = SpokenText()
    return ''.join(f.feed(c) for c in chunks) + f.flush()


class SpokenTextTests(unittest.TestCase):
    def test_removes_markdown_and_list_markers(self):
        self.assertEqual(spoken('Tap **Click to Pay**, then ', '*Order*.'), 'Tap Click to Pay, then Order.')
        self.assertEqual(spoken('Steps:\n1. Tap Click to Pay\n2) Tap Order\n- Pay on screen'),
                         'Steps:\nTap Click to Pay\nTap Order\nPay on screen')
        self.assertEqual(spoken('# Heading\n• dot point'), 'Heading\ndot point')

    def test_markers_split_across_tokens(self):
        self.assertEqual(spoken('Do this:\n', '1', '.', ' Tap', ' it'), 'Do this:\nTap it')
        self.assertEqual(spoken('*', '*bold', '*', '*'), 'bold')

    def test_ordinary_text_unchanged(self):
        for text in ['3 days is $27.50.', '1.5 hours', '12 hours\n24 hours', 'A well-known step - then wait.',
                     '10 days', "It's $9.00 with a concession card."]:
            self.assertEqual(spoken(text), text, text)
            self.assertEqual(spoken(*list(text)), text, 'char by char: ' + text)

    def test_space_added_between_holding_line_and_answer(self):
        # Text the model says before a tool call is dropped; a holding line is said in its place.
        fake = FakeClient([FakeStream("That's the LSPC15mf.", content=[tool_use('lookup_device', {'hospital': 'RAH', 'bed_id': '1', 'device_id': ''})]),
                           FakeStream('Found it.')])
        said = []
        async def on_text(d): said.append(d)
        async def run_tool(name, args): return {'found': True}
        convo = Conversation(CLAUDE_CFG, lambda: 'system', lambda: app.tool_specs(CLAUDE_CFG), run_tool, on_text)
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake):
            kind, text = asyncio.run(convo.respond('Black screen, orange button.'))
        self.assertEqual(text, 'Got it. Found it.')
        self.assertEqual(''.join(said), text)


class ReferenceAndEndTests(unittest.TestCase):
    _base = test_pilot.PilotTests
    post, begin, ws, setup = _base.post, _base.begin, _base.ws, _base.setup

    def setUp(self):
        app.sessions.clear()
        self.cfg = dict(CLAUDE_CFG)
        patcher = patch.object(app, 'config', side_effect=lambda: self.cfg); patcher.start(); self.addCleanup(patcher.stop)
        self.client = TestClient(app.app); self.sid = 'CA' + '6' * 32

    def test_reference_and_caller_digits_kept_and_spoken_at_end(self):
        self.post('/voice', {'From': '+61 412 555 019'})
        state = app.sessions[self.sid]
        self.assertRegex(state['reference'], r'^\d{4}$'); self.assertEqual(state['caller'], '019')
        reference = state['reference']
        end = fromstring(self.post('/end').text)
        say = end.find('Say')
        self.assertEqual((say.get('language'), say.get('voice')), ('en-AU', 'Polly.Olivia-Neural'))
        self.assertIn('Your call reference is ' + ' '.join(reference) + '.', say.text)
        self.assertIsNotNone(end.find('Hangup'))

    def test_reference_can_be_switched_off(self):
        self.cfg['call_reference'] = 'off'
        self.post('/voice')
        self.assertNotIn('reference', fromstring(self.post('/end').text).find('Say').text)

    def test_references_unique_among_active_calls(self):
        with patch.object(pilot_log.secrets, 'randbelow', side_effect=[4721, 4721, 88]):
            self.assertEqual(pilot_log.new_reference(set()), '4721')
            self.assertEqual(pilot_log.new_reference({'4721'}), '0088')


class ProblemReportTests(unittest.TestCase):
    def test_tool_offered_only_when_staff_feedback_on(self):
        self.assertIn('report_problem', {t['name'] for t in app.tool_specs(CLAUDE_CFG)})
        self.assertNotIn('report_problem', {t['name'] for t in app.tool_specs({**CLAUDE_CFG, 'staff_feedback': 'off'})})
        self.assertTrue(tools.valid_input(tools.specs([], feedback=True), 'report_problem', {'description': 'misheard'}))

    def test_report_saved_privately_and_call_flagged(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pilot_log, 'FEEDBACK_LOG', Path(folder) / 'f.jsonl'):
            session = {'reference': '4721', 'caller': '019'}
            result = asyncio.run(app.make_runner(CLAUDE_CFG, 'CA123456', session)('report_problem', {'description': 'It misheard RNSH'}))
            self.assertEqual(result, {'recorded': True, 'call_reference': '4 7 2 1'})
            self.assertTrue(session['flagged']); self.assertTrue(is_private(pilot_log.FEEDBACK_LOG))
            self.assertEqual(pilot_log.read_reports()[0]['description'], 'It misheard RNSH')

    def test_version_stamp(self):
        stamp = pilot_log.version_stamp(CLAUDE_CFG, 'rules', 'knowledge')
        self.assertEqual((stamp['model'], stamp['effort'], stamp['thinking']), ('claude-sonnet-5-5', 'low', 'between_tools'))
        self.assertEqual(len(stamp['prompt']), 8); self.assertTrue(stamp['code'])


class TranscriptLookupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        patcher = patch.object(transcripts, 'FOLDER', Path(self.folder.name)); patcher.start(); self.addCleanup(patcher.stop)

    def make_call(self, reference='4721', report=None):
        with patch('sys.stderr', io.StringIO()):
            t = transcripts.CallTranscript('CA' + '7' * 32, True, details={'reference': reference, 'caller': '019',
                                                                           'version': {'code': 'abc1234', 'model': 'm'}})
            t.event('greeting', {'text': app.WELCOME})
            t.event('caller', {'text': "Hi, it's Sam. My screen is black."})
            t.event('agent', {'text': 'Is there an orange button below the screen?'})
            if report:
                t.event('tool', {'name': 'report_problem', 'args': {'description': report}, 'result': {'recorded': True}})
            t.event('caller', {'text': 'Yes there is.'})
            t.event('closed', {'reason': 'goodbye', 'flagged': bool(report)})
        return t.path

    def test_file_named_by_reference_and_found(self):
        path = self.make_call('4721', report='It asked two questions at once')
        self.assertIn('-ref4721-', path.name)
        info = transcripts.summary(path)
        self.assertEqual((info['reference'], info['caller'], info['turns'], info['flagged']), ('4721', '019', 2, True))
        self.assertEqual(info['reports'], ['It asked two questions at once'])
        self.assertEqual(transcripts.find('4721'), [path]); self.assertEqual(transcripts.find('019'), [path])
        self.assertEqual(transcripts.find('9999'), [])
        shown = transcripts.format_event(json.loads(path.read_text(encoding='utf-8').splitlines()[0]))
        self.assertIn('ref 4721', shown); self.assertIn('version abc1234', shown)

    def test_review_conversion_and_sources(self):
        path = self.make_call()
        run = review.to_run(transcripts.summary(path)['records'])
        self.assertEqual([s for s, _ in run['transcript']], ['agent', 'caller', 'agent', 'caller'])
        run['tools'].append({'name': 'get_guide', 'args': {'guide_id': 'KB-TERMINAL-LSPC15MF'}, 'result': {'found': True}})
        sources = review.sources_for(run)
        self.assertIn('KB-PRICES', sources); self.assertIn('KB-TERMINAL-LSPC15MF', sources)
        self.assertNotIn('KB-TERMINAL-TOUCH-KEYS', sources)

    def test_call_becomes_draft_scenario(self):
        self.make_call('4721', report='It asked two questions at once')
        scenarios = Path(self.folder.name) / 'scenarios.json'
        scenarios.write_text(json.dumps({'scenarios': []}), encoding='utf-8')
        with patch.object(harness, 'SCENARIOS', scenarios):
            draft = harness.scenario_from_call('4721')
            harness.add_scenario(draft)
            with self.assertRaises(SystemExit):
                harness.add_scenario(draft)  # no duplicates
        self.assertEqual(draft['id'], 'CALL-4721')
        self.assertEqual(draft['caller']['script'], ["Hi, it's Sam. My screen is black.", 'Yes there is.'])
        self.assertIn('It asked two questions at once', draft['judge'][0])
        self.assertEqual(json.loads(scenarios.read_text(encoding='utf-8'))['scenarios'][0]['id'], 'CALL-4721')


if __name__ == '__main__':
    unittest.main()
