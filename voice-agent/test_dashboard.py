"""Offline tests for the local dashboard: access rules, pages, settings and actions. No API calls."""
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import configure
import dashboard
import harness
import issues
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
                        patch.object(issues, 'STORE', root / 'issues.json'),
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
                    '/flows', '/settings', '/knowledge', '/live', '/issues', '/issues?status=all', '/live/feed', '/docs', '/docs/scenarios',
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

    def review_finding(self, path, *texts):
        review._save_state({path.name: {'at': '2026-11-27T10:00:00', 'reference': '4721', 'flagged': True,
                                        'issues': list(texts), 'reviewed': '2026-11-27T10:05:00'}})

    def test_issues_flow_from_review_to_verified(self):
        path = self.make_call()
        self.review_finding(path, 'Asks one clear question at a time: asked two questions in one turn')
        with patch('sys.stderr', io.StringIO()):
            t = transcripts.CallTranscript('CA' + '6' * 32, True, details={'reference': '9001'})
            t.event('closed', {'reason': 'goodbye'})
        review._save_state({**review._load_state(), t.path.name: {'reference': '9001', 'issues': [
            'Asks one clear question at a time: again two questions'], 'reviewed': '2026-11-27T10:06:00'}})
        listing = self.client.get('/issues').text  # syncs: one issue per review finding
        self.assertIn('ISS-001', listing); self.assertIn('ISS-002', listing); self.assertIn('Recurring themes', listing)
        self.assertIn('<span class="count"', listing)  # new-issue badge in the sidebar
        self.assertIn('ISS-001', self.client.get(dashboard.call_url(path.name)).text)
        self.client.get('/issues')  # a second sync adds nothing
        self.assertEqual(len(issues.load()['issues']), 2)

        self.assertEqual(self.client.post('/issues/ISS-001', data={'status': 'open'}).status_code, 403)
        made = self.client.post('/issues/ISS-001/scenario', data={'token': dashboard.TOKEN}, follow_redirects=False)
        self.assertEqual(made.status_code, 303)
        issue = issues.get(issues.load(), 'ISS-001')
        self.assertEqual(issue['scenario'], 'CALL-4721')
        scenario = json.loads(harness.SCENARIOS.read_text(encoding='utf-8'))['scenarios'][-1]
        self.assertIn('Does not repeat this problem: Asks one clear question at a time', scenario['judge'])

        self.client.post('/issues/ISS-001', data={'token': dashboard.TOKEN, 'status': 'fixed', 'category': 'rule or prompt',
                                                  'scenario': 'CALL-4721', 'note': 'tightened the rule'})
        issue = issues.get(issues.load(), 'ISS-001')
        self.assertEqual((issue['status'], issue['category']), ('fixed', 'rule or prompt'))
        self.assertTrue(issue['fixed_in']); self.assertIn('note: tightened the rule', issue['history'][-1]['change'])
        self.assertEqual(issues.verification(issue, {})['state'], 'not run')
        later = (issue['fixed_at'][:10] + ' 23:59')
        self.assertEqual(issues.verification(issue, {'CALL-4721': [(True, later)]})['state'], 'verified')
        self.assertEqual(issues.verification(issue, {'CALL-4721': [(False, later)]})['state'], 'failing')
        self.assertEqual(issues.verification(issue, {'CALL-4721': [(True, '2000-01-01 00:00')]})['state'], 'waiting')

        bulk = self.client.post('/issues/bulk', data={'token': dashboard.TOKEN, 'ids': ['ISS-002'], 'status': 'dismissed',
                                                      'note': 'duplicate of ISS-001'}, follow_redirects=False)
        self.assertEqual(bulk.status_code, 303)
        self.assertEqual(issues.get(issues.load(), 'ISS-002')['status'], 'dismissed')
        self.assertEqual(self.client.post('/issues/bulk', data={'token': dashboard.TOKEN, 'ids': ['ISS-999'],
                                                                'status': 'open'}).status_code, 404)
        self.assertEqual(self.client.post('/issues/ISS-001', data={'token': dashboard.TOKEN, 'scenario': 'NOPE'}).status_code, 400)

    def test_manual_issues_reports_and_export(self):
        path = self.make_call()
        pilot_log.FEEDBACK_LOG.write_text(json.dumps({'at': '2026-11-27T11:00:00', 'reference': '4721', 'caller': '019',
                                                      'call': '888888', 'description': 'It talked over me'}) + '\n', encoding='utf-8')
        added = self.client.post('/issues/new', data={'token': dashboard.TOKEN, 'title': 'Voice too fast <b>',
                                                      'reference': '4721', 'category': 'voice'}, follow_redirects=False)
        self.assertEqual(added.status_code, 303)
        listing = self.client.get('/issues').text
        self.assertIn('Voice too fast &lt;b&gt;', listing); self.assertIn('It talked over me', listing)
        found = {i['source']: i for i in issues.load()['issues']}
        self.assertEqual(found['report']['call_file'], path.name); self.assertEqual(found['manual']['call_file'], path.name)
        self.assertIn('Show the conversation', self.client.get(f"/issues/{found['report']['id']}").text)
        for bad in [{'title': ''}, {'title': 'x', 'reference': '12'}, {'title': 'x', 'category': 'nonsense'}]:
            self.assertEqual(self.client.post('/issues/new', data={'token': dashboard.TOKEN, **bad}).status_code, 400, bad)
        csv_text = self.client.get('/issues.csv')
        self.assertTrue(csv_text.headers['content-disposition'].startswith('attachment'))
        self.assertIn('Voice too fast <b>', csv_text.text)
        self.assertTrue(is_private(issues.STORE))
        for url in ['/issues/ISS-999', '/issues/..%2Fx', '/issues/nothing']:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def write_run(self, name, outcomes, version=None, cost=0.10, first=1500, skipped=()):
        """outcomes: scenario id -> list of passed flags (one per repeat)."""
        results = []
        for sid, flags in outcomes.items():
            for n, ok in enumerate(flags, 1):
                results.append({'id': sid, 'title': f'Title {sid}', 'repeat': n, 'passed': ok, 'cost': 0.01,
                                'checks': [{'check': 'said_none', 'passed': ok, 'detail': '' if ok else 'said "bed number"'}],
                                'verdicts': [], 'run': {'transcript': [], 'tools': [], 'finish': None, 'error': None, 'turns': [{'first_word_ms': first, 'total_ms': first + 500}]}})
        settings = {'started': f'{name[:4]}-{name[4:6]}-{name[6:8]} {name[9:11]}:{name[11:13]}', 'model': 'claude-sonnet-5-5',
                    'effort': 'low', 'thinking': None, 'judge_model': 'claude-haiku-5-5', 'budget': 0.5}
        if version:
            settings['version'] = version
        folder = harness.RUNS / name; folder.mkdir()
        (folder / 'results.json').write_text(json.dumps({'settings': settings, 'cost': cost, 'usage': {}, 'skipped': list(skipped),
                                                         'results': results}), encoding='utf-8')

    def test_compare_runs(self):
        v1 = {'code': 'aaa1111', 'prompt': 'p1', 'knowledge': 'k1'}
        self.write_run('20261127-090000', {'A-1': [True], 'B-1': [False], 'C-1': [True], 'D-1': [False], 'E-1': [True]}, v1)
        self.write_run('20261127-100000', {'A-1': [True], 'B-1': [True], 'C-1': [False], 'D-1': [False, True], 'F-1': [True]},
                       {**v1, 'prompt': 'p2'}, cost=0.08, first=1200)
        c = harness.compare_runs(*(json.loads((harness.RUNS / n / 'results.json').read_text(encoding='utf-8'))
                                   for n in ('20261127-090000', '20261127-100000')))
        changes = {r['id']: r['change'] for r in c['rows']}
        self.assertEqual(changes, {'A-1': 'still passing', 'B-1': 'fixed', 'C-1': 'broke', 'D-1': 'fixed',
                                   'E-1': 'only before', 'F-1': 'only after'})
        self.assertEqual(c['rows'][0]['id'], 'C-1')  # regressions first
        self.assertEqual(c['changed'], [('prompt', 'p1', 'p2')])
        self.assertEqual(c['shared'], {'count': 4, 'a': 2, 'b': 2})  # A,B,C,D: A+C pass before; A+B after (D mixed)
        self.assertEqual(next(r for r in c['rows'] if r['id'] == 'D-1')['b']['state'], 'mixed')

        page_text = self.client.get('/tests/compare').text  # defaults: previous run vs newest
        self.assertIn('▼ broke', page_text); self.assertIn('▲ fixed', page_text)
        self.assertIn('<code>p1</code> → <code>p2</code>', page_text)
        self.assertIn('did not cover the same scenarios', page_text)
        self.assertIn('name="only" value="C-1"', page_text)  # re-run button covers what still fails
        self.assertIn('/tests/compare?a=20261127-090000&b=20261127-100000', self.client.get('/tests/20261127-100000').text)
        self.assertIn('name="a"', self.client.get('/tests').text)
        self.assertEqual(self.client.get('/tests/compare?a=20261127-090000&b=20261127-090000').status_code, 400)
        self.assertEqual(self.client.get('/tests/compare?a=..%2F..&b=20261127-090000').status_code, 404)

    def test_trends(self):
        from datetime import date
        self.call_with_usage()  # today: 1 call, first word 3.1 s, costed
        self.make_call()        # today: 1 call, no timing or tokens
        self.write_run(datetime_name := date.today().strftime('%Y%m%d') + '-090000', {'A-1': [True], 'B-1': [False]})
        rows, runs = dashboard.calls(), dashboard.harness_runs()
        data, tests = dashboard.trends(rows, runs, 14)
        self.assertEqual(len(data), 14); self.assertEqual(data[-1]['day'], date.today())
        self.assertEqual((data[-1]['calls'], data[-1]['first_ms'], data[-1]['costed']), (2, 3100, 1))
        self.assertAlmostEqual(data[-1]['cost'], (4000 + 2000 + 1800) / 1e6)
        self.assertEqual(data[0]['calls'], 0); self.assertIsNone(data[0]['first_ms'])
        self.assertEqual([(t['name'], t['rate']) for t in tests], [(datetime_name, 0.5)])
        page_text = self.client.get('/?days=30').text
        for text in ['Calls per day', 'Average time to first word', 'AI cost per call', 'Test pass rate', '50%',
                     'class="spark"', 'Daily figures as a table', 'href="/?days=90"']:
            self.assertIn(text, page_text)
        self.assertEqual(self.client.get('/?days=7').status_code, 200)  # unknown ranges fall back to 14 days
        self.assertIn('No data in this period yet.', dashboard.spark([('a', None, '')], 'line', str))

    def test_calls_filters_sorting_and_pages(self):
        slow = self.call_with_usage()   # ref 5150: ticket #881, first word 3.1 s, costed, ended goodbye
        plain = self.make_call()         # ref 4721: "black" screen, reported, ended goodbye
        with patch('sys.stderr', io.StringIO()):
            t = transcripts.CallTranscript('CA' + '5' * 32, True, details={'reference': '6006', 'version': {'code': 'new1234'}})
            t.event('caller', {'text': 'What channels are there?'})
            t.event('closed', {'reason': 'clinical'})
        refs = lambda url: re.findall(r'<b>(\d{4})</b>', self.client.get(url).text)
        self.assertEqual(sorted(refs('/calls')), ['4721', '5150', '6006'])
        self.assertEqual(refs('/calls?q=channels'), ['6006'])         # words said in the call
        self.assertEqual(refs('/calls?q=5150'), ['5150'])
        self.assertEqual(refs('/calls?ticket=yes'), ['5150'])
        self.assertEqual(sorted(refs('/calls?ticket=no')), ['4721', '6006'])
        self.assertEqual(refs('/calls?speed=slow'), ['5150'])
        self.assertEqual(refs('/calls?ended=clinical'), ['6006'])
        self.assertEqual(refs('/calls?version=new1234'), ['6006'])
        self.assertEqual(refs('/calls?show=reported'), ['4721'])
        self.assertEqual(refs('/calls?range=today&start=2000-01-01'), refs('/calls?range=today'))
        self.assertEqual(refs('/calls?end=2000-01-01'), [])
        self.assertEqual(refs('/calls?sort=first&dir=desc')[0], '5150')
        self.assertEqual(refs('/calls?sort=cost&dir=asc')[-1], '5150')  # the only costed call sorts last when ascending
        page_text = self.client.get('/calls?ticket=no').text
        self.assertIn('<b>2</b> of 3 calls', page_text); self.assertIn('href="/calls.csv?ticket=no"', page_text)
        with patch.object(dashboard, 'PAGE_SIZE', 1):
            first, second = self.client.get('/calls').text, self.client.get('/calls?page=2').text
            self.assertIn('page 1 of 3', first); self.assertIn('page 2 of 3', second)
            self.assertIn('href="/calls?page=2"', first)
            self.assertIn('page 3 of 3', self.client.get('/calls?page=99').text)
        csv_text = self.client.get('/calls.csv?ticket=yes').text.splitlines()
        self.assertEqual(len(csv_text), 2); self.assertIn('5150', csv_text[1]); self.assertIn('881', csv_text[1])
        self.assertEqual(self.client.get('/calls?q=<script>').status_code, 200)
        self.assertNotIn('<script>"', self.client.get('/calls?q=<script>').text)

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
