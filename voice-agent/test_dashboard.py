"""Offline tests for the local dashboard: access rules, pages, settings and actions. No API calls."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import configure
import dashboard
import harness
import pilot_log
from private_files import is_private
import review
import test_pilot
import transcripts


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'transcripts').mkdir(); (root / 'reviews').mkdir(); (root / 'runs').mkdir()
        self.config = root / 'twilio-pilot.json'
        configure.save_config({**test_pilot.CFG, 'provider': 'anthropic', 'anthropic_model': 'claude-sonnet-5-5',
                               'effort': 'low', 'anthropic_api_key': 'sk-ant-synthetic-never-shown',
                               'transcripts': 'on'}, self.config)
        scenarios = root / 'scenarios.json'
        scenarios.write_text(json.dumps({'scenarios': [{'id': 'X-1', 'title': 'Example', 'tags': ['core'],
                                                        'caller': {'script': ['Hi']}}]}), encoding='utf-8')
        self.patches = [patch.object(configure, 'CONFIG', self.config), patch.object(transcripts, 'FOLDER', root / 'transcripts'),
                        patch.object(review, 'FOLDER', root / 'reviews'), patch.object(review, 'STATE', root / 'reviews' / 'reviewed.json'),
                        patch.object(harness, 'RUNS', root / 'runs'), patch.object(harness, 'SCENARIOS', scenarios),
                        patch.object(pilot_log, 'FEEDBACK_LOG', root / 'feedback.jsonl'),
                        patch.object(dashboard, 'PILOT_HEALTH', 'http://127.0.0.1:9/health')]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.client = TestClient(dashboard.app, base_url='http://127.0.0.1:8102')
        self.root = root

    def make_call(self):
        with patch('sys.stderr', io.StringIO()):
            t = transcripts.CallTranscript('CA' + '8' * 32, True, details={'reference': '4721', 'caller': '019',
                                                                           'version': {'code': 'abc1234'}})
            t.event('greeting', {'text': 'Hi, this is LSPcare support.'})
            t.event('caller', {'text': "It's Sam, my screen is <b>black</b>"})
            t.event('tool', {'name': 'report_problem', 'args': {'description': 'It talked over me'}, 'result': {'recorded': True}})
            t.event('closed', {'reason': 'goodbye', 'flagged': True})
        return t.path

    def test_only_local_host_names_are_served(self):
        self.assertEqual(TestClient(dashboard.app, base_url='http://evil.example').get('/').status_code, 403)
        self.assertEqual(TestClient(dashboard.app, base_url='http://localhost:8102').get('/knowledge').status_code, 200)

    def test_every_page_renders_and_never_shows_secrets(self):
        path = self.make_call()
        for url in ['/', '/calls', '/calls?show=attention', f'/calls/{path.name}', '/reports', '/reviews', '/tests',
                    '/settings', '/knowledge']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotIn('sk-ant-synthetic', response.text, url)
            self.assertNotIn(test_pilot.CFG['auth_token'], response.text, url)
        detail = self.client.get(f'/calls/{path.name}').text
        self.assertIn('&lt;b&gt;black&lt;/b&gt;', detail)  # caller text is escaped
        self.assertIn('Problem reported: It talked over me', detail)
        self.assertIn('not running', self.client.get('/').text)

    def test_paths_cannot_escape_their_folders(self):
        for url in ['/calls/..%2Ftwilio-pilot.json', '/calls/nothing.jsonl', '/tests/..%2F..%2Fprivate-config', '/tests/20261126', '/reviews?file=../x.md']:
            self.assertIn(self.client.get(url).status_code, (403, 404), url)

    def test_actions_need_the_token(self):
        self.assertEqual(self.client.post('/settings', data={'key': 'effort', 'value': 'medium'}).status_code, 403)
        self.assertEqual(self.client.post('/reviews/run', data={}).status_code, 403)
        self.assertEqual(self.client.post('/tests/run', data={'token': 'wrong'}).status_code, 403)

    def test_settings_saved_privately_and_validated(self):
        ok = self.client.post('/settings', data={'token': dashboard.TOKEN, 'key': 'effort', 'value': 'medium'}, follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        saved = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(saved['effort'], 'medium'); self.assertEqual(saved['auth_token'], test_pilot.CFG['auth_token'])
        self.assertTrue(is_private(self.config))
        for key, value in [('tickets', 'sometimes'), ('auth_token', 'x'), ('effort', 'max')]:
            if key == 'effort':
                self.client.post('/settings', data={'token': dashboard.TOKEN, 'key': 'thinking', 'value': 'between_tools'})
            bad = self.client.post('/settings', data={'token': dashboard.TOKEN, 'key': key, 'value': value})
            self.assertEqual(bad.status_code, 400, key)
        self.assertEqual(json.loads(self.config.read_text(encoding='utf-8'))['effort'], 'medium')

    def test_call_becomes_scenario(self):
        path = self.make_call()
        response = self.client.post(f'/calls/{path.name}/scenario', data={'token': dashboard.TOKEN}, follow_redirects=False)
        self.assertEqual(response.status_code, 303); self.assertIn('made=CALL-4721', response.headers['location'])
        ids = [s['id'] for s in json.loads(harness.SCENARIOS.read_text(encoding='utf-8'))['scenarios']]
        self.assertEqual(ids, ['X-1', 'CALL-4721'])

    def test_test_run_command_is_bounded(self):
        started = []
        with patch.object(dashboard, 'start_job', side_effect=lambda title, command: started.append(command) or 'job1'):
            self.client.post('/tests/run', data={'token': dashboard.TOKEN, 'selection': 'tag:core', 'repeat': '9',
                                                 'budget': '5', 'thinking': 'between_tools'}, follow_redirects=False)
            bad = self.client.post('/tests/run', data={'token': dashboard.TOKEN, 'only': 'HW-01; rm -rf'})
        command = started[0]
        self.assertEqual(command[command.index('--budget') + 1], '1.0')
        self.assertEqual(command[command.index('--repeat') + 1], '3')
        self.assertIn('--tag', command); self.assertIn('thinking=between_tools', command)
        self.assertEqual(bad.status_code, 400)

    def test_markdown_renderer_escapes(self):
        html = dashboard.markdown('# Title\n- item <script>\n```\ncode <x>\n```\n**bold**')
        self.assertIn('<h2>Title</h2>', html); self.assertIn('&lt;script&gt;', html); self.assertIn('code &lt;x&gt;', html)
        self.assertIn('<b>bold</b>', html)


if __name__ == '__main__':
    unittest.main()
