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

    def make_call(self, open_call=False):
        with patch('sys.stderr', io.StringIO()):
            t = transcripts.CallTranscript('CA' + '8' * 32, True, details={'reference': '4721', 'caller': '019',
                                                                           'version': {'code': 'abc1234'}})
            t.event('greeting', {'text': 'Hi, this is LSPcare support.'})
            t.event('caller', {'text': "It's Sam, my screen is <b>black</b>"})
            t.event('tool', {'name': 'report_problem', 'args': {'description': 'It talked over me'}, 'result': {'recorded': True}})
            if not open_call:
                t.event('closed', {'reason': 'goodbye', 'flagged': True})
        return t.path

    def call_with_usage(self):
        usage = type('U', (), {'input_tokens': 1000, 'output_tokens': 100, 'cache_read_input_tokens': 9000,
                               'cache_creation_input_tokens': 0})()
        with patch('sys.stderr', io.StringIO()) as err:
            t = transcripts.CallTranscript('CA' + '7' * 32, True, details={'reference': '5150'})
            t.event('caller', {'text': 'My screen is black'})
            t.event('usage', transcripts.usage_record('claude-sonnet-5-5', usage))
            t.event('usage', transcripts.usage_record('claude-sonnet-5-5', usage))
            t.event('tool', {'name': 'flow_confirm', 'args': {'answer': 'yes'},
                             'result': {'confirmed': True, 'result': {'ticket_logged': True, 'ticket_number': '881', 'dry_run': True}}})
            t.event('turn', {'first_word_ms': 3100, 'total_ms': 4200, 'tools': ['flow_confirm']})
            t.event('closed', {'reason': 'goodbye', 'flagged': False})
        self.assertNotIn('usage', err.getvalue())  # token lines are not printed in the pilot window
        return t.path

    def test_usage_is_recorded_and_costed(self):
        path = self.call_with_usage()
        s = transcripts.stats(transcripts.summary(path)['records'])
        self.assertEqual(s['tokens'], {'input': 2000, 'output': 200, 'cache_read': 18000, 'cache_write': 0})
        self.assertEqual(s['ticket'], {'number': '881', 'dry_run': True})
        self.assertEqual(s['first_word_avg_ms'], 3100)
        # 2000*2 + 200*10 + 18000*0.10 per million
        self.assertAlmostEqual(dashboard.usd(s['by_model']), (4000 + 2000 + 1800) / 1e6)
        page_text = self.client.get(dashboard.call_url(path.name)).text
        self.assertIn('Tokens in', page_text); self.assertIn('#881 (dry run)', page_text)
        self.assertIn('2 requests', page_text); self.assertIn('class="slow"', page_text)

    def test_call_pages_use_plain_addresses(self):
        path = self.make_call()
        listing = self.client.get('/calls').text
        self.assertIn(f'href="/calls/{path.stem}"', listing); self.assertNotIn('.jsonl"', listing)
        old = self.client.get(f'/calls/{path.name}', follow_redirects=False)
        self.assertEqual(old.status_code, 307); self.assertEqual(old.headers['location'], f'/calls/{path.stem}')
        raw = self.client.get(f'/calls/{path.stem}/raw')
        self.assertEqual(raw.status_code, 200); self.assertIn('"kind": "greeting"', raw.text)

    def test_live_feed_follows_open_call(self):
        self.assertFalse(self.client.get('/live/feed').json()['active'])
        path = self.make_call(open_call=True)
        feed = self.client.get('/live/feed').json()
        self.assertTrue(feed['active']); self.assertIn('&lt;b&gt;black&lt;/b&gt;', feed['html'])
        self.assertTrue(self.client.get('/live/feed', params={'since': feed['sig']}).json()['same'])
        self.assertIn('class="dot"', self.client.get('/calls').text)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'at': '2026-11-27T10:00:00', 'kind': 'closed', 'reason': 'goodbye'}) + '\n')
        self.assertFalse(self.client.get('/live/feed').json()['active'])

    def test_docs_are_listed_and_only_known_files_served(self):
        docs = Path(self.tmp.name) / 'repo' / 'docs'; docs.mkdir(parents=True)
        (docs / 'guide.md').write_text('# Staff guide\n\nHow to test.\n\n| A | B |\n|---|---|\n| 1 | <x> |\n', encoding='utf-8')
        (docs / 'ref.pdf').write_bytes(b'%PDF-1.4 synthetic')
        (docs / 'page.html').write_text('<script>alert(1)</script>', encoding='utf-8')
        with patch.object(dashboard, 'REPO', docs.parent):
            index = self.client.get('/docs').text
            self.assertIn('Staff guide', index); self.assertIn('Training scenarios', index)
            view = self.client.get('/docs/view/guide.md').text
            self.assertIn('<th>A</th>', view); self.assertIn('&lt;x&gt;', view)
            pdf = self.client.get('/docs/file/ref.pdf')
            self.assertEqual(pdf.headers['content-type'], 'application/pdf')
            self.assertTrue(pdf.headers['content-disposition'].startswith('inline'))
            self.assertEqual(self.client.get('/docs/file/page.html').headers['content-security-policy'], 'sandbox')
            for url in ['/docs/view/..%2F..%2Ftwilio-pilot.json', '/docs/file/missing.pdf', '/docs/view/twilio-pilot.json']:
                self.assertEqual(self.client.get(url).status_code, 404, url)
        scenarios = self.client.get('/docs/scenarios?tag=core').text
        self.assertIn('X-1', scenarios); self.assertIn('Scripted caller', scenarios)

    def test_only_local_host_names_are_served(self):
        self.assertEqual(TestClient(dashboard.app, base_url='http://evil.example').get('/').status_code, 403)
        self.assertEqual(TestClient(dashboard.app, base_url='http://localhost:8102').get('/knowledge').status_code, 200)

    def test_every_page_renders_and_never_shows_secrets(self):
        path = self.make_call()
        for url in ['/', '/calls', '/calls?show=attention', f'/calls/{path.stem}', '/reports', '/reviews', '/tests',
                    '/flows', '/settings', '/knowledge', '/live', '/live/feed', '/docs', '/docs/scenarios',
                    '/docs/view/testing.md', '/docs/view/issue-register.csv', '/docs/view/technical-reference.pdf']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotIn('sk-ant-synthetic', response.text, url)
            self.assertNotIn(test_pilot.CFG['auth_token'], response.text, url)
        detail = self.client.get(f'/calls/{path.stem}').text
        self.assertIn('&lt;b&gt;black&lt;/b&gt;', detail)  # caller text is escaped
        self.assertIn('Problem reported: It talked over me', detail)
        self.assertIn('not running', self.client.get('/').text)

    def test_paths_cannot_escape_their_folders(self):
        for url in ['/calls/..%2Ftwilio-pilot.json', '/calls/nothing.jsonl', '/calls/nothing', '/calls/..%2Ftwilio-pilot/raw', '/tests/..%2F..%2Fprivate-config', '/tests/20261126', '/reviews?file=../x.md']:
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
        response = self.client.post(f'/calls/{path.stem}/scenario', data={'token': dashboard.TOKEN}, follow_redirects=False)
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
        html = dashboard.markdown('1. one\n2. two\n\n[safe](flows.md) [bad](javascript:alert(1)) *it*\n\n| a | b |\n|---|---|\n| <i> | 2 |')
        self.assertIn('<ol>\n<li>one</li>', html); self.assertIn('href="/docs/view/flows.md"', html)
        self.assertNotIn('javascript:', html.replace('bad', '')); self.assertIn('<i>it</i>', html)
        self.assertIn('<td>&lt;i&gt;</td>', html)


if __name__ == '__main__':
    unittest.main()
