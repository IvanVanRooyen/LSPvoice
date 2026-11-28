"""Offline tests for dashboard authentication: cookie signing, rate limiting, role enforcement,
session invalidation and audit logging. No API calls."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import auth
import configure
import dashboard
import harness
import issues
import pilot_log
import review
import test_pilot
import transcripts

TEST_ENV = {'DASHBOARD_SESSION_SECRET': 'x' * 32,
            'DASHBOARD_PASSWORD_ADMIN': 'admin-pw',
            'DASHBOARD_PASSWORD_REVIEWER': 'reviewer-pw',
            'DASHBOARD_PASSWORD_READER': 'reader-pw',
            'DASHBOARD_HOSTS': '127.0.0.1:8102,localhost:8102,testserver'}


class ConfigTests(unittest.TestCase):
    def test_reads_env(self):
        with patch.dict(os.environ, TEST_ENV):
            cfg = auth.read_config()
        self.assertEqual(cfg.secret, b'x' * 32)
        self.assertEqual(cfg.passwords['admin'], 'admin-pw')
        self.assertIn('127.0.0.1:8102', cfg.allowed_hosts)

    def test_missing_secret(self):
        env = {**TEST_ENV, 'DASHBOARD_SESSION_SECRET': 'short'}
        with patch.dict(os.environ, env):
            with self.assertRaises(ValueError):
                auth.read_config()

    def test_missing_password(self):
        env = {**TEST_ENV, 'DASHBOARD_PASSWORD_READER': ''}
        with patch.dict(os.environ, env, clear=False):
            del os.environ['DASHBOARD_PASSWORD_READER']
            with self.assertRaises(ValueError):
                auth.read_config()

    def test_missing_hosts(self):
        env = {**TEST_ENV, 'DASHBOARD_HOSTS': ''}
        with patch.dict(os.environ, env):
            with self.assertRaises(ValueError):
                auth.read_config()


class CookieTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, TEST_ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.cfg = auth.read_config()

    def test_round_trip(self):
        cookie = auth.sign_cookie(self.cfg, 'admin', generation=3)
        self.assertEqual(auth.read_cookie(self.cfg, cookie, generation=3), 'admin')

    def test_wrong_generation(self):
        cookie = auth.sign_cookie(self.cfg, 'admin', generation=3)
        self.assertIsNone(auth.read_cookie(self.cfg, cookie, generation=4))

    def test_expired(self):
        now = int(time.time()) - auth.SESSION_SECONDS - 10
        cookie = auth.sign_cookie(self.cfg, 'admin', generation=1, now=now)
        self.assertIsNone(auth.read_cookie(self.cfg, cookie, generation=1))

    def test_tampered_payload(self):
        cookie = auth.sign_cookie(self.cfg, 'reader', generation=1)
        payload, sig = cookie.split('.')
        # Flip the last character of the payload: the signature no longer matches.
        bad_payload = payload[:-1] + ('a' if payload[-1] != 'a' else 'b')
        self.assertIsNone(auth.read_cookie(self.cfg, bad_payload + '.' + sig, generation=1))

    def test_wrong_secret(self):
        cookie = auth.sign_cookie(self.cfg, 'admin', generation=1)
        other = auth.AuthConfig(secret=b'y' * 32, passwords=self.cfg.passwords, allowed_hosts=self.cfg.allowed_hosts)
        self.assertIsNone(auth.read_cookie(other, cookie, generation=1))

    def test_malformed_cookie(self):
        self.assertIsNone(auth.read_cookie(self.cfg, '', generation=1))
        self.assertIsNone(auth.read_cookie(self.cfg, 'nodot', generation=1))
        self.assertIsNone(auth.read_cookie(self.cfg, '@@@.@@@', generation=1))


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        auth.LOGIN_ATTEMPTS.clear()
        self.addCleanup(auth.LOGIN_ATTEMPTS.clear)

    def test_locks_out_after_max_failures(self):
        ip = '10.0.0.1'
        for _ in range(auth.LOGIN_MAX_FAILURES):
            self.assertTrue(auth.rate_limit_allowed(ip, now=0))
            auth.rate_limit_record(ip, success=False, now=0)
        self.assertFalse(auth.rate_limit_allowed(ip, now=0))
        # Window elapses.
        self.assertTrue(auth.rate_limit_allowed(ip, now=auth.LOGIN_WINDOW_SECONDS + 1))

    def test_success_clears(self):
        ip = '10.0.0.2'
        for _ in range(auth.LOGIN_MAX_FAILURES):
            auth.rate_limit_record(ip, success=False, now=0)
        self.assertFalse(auth.rate_limit_allowed(ip, now=0))
        auth.rate_limit_record(ip, success=True, now=0)
        self.assertTrue(auth.rate_limit_allowed(ip, now=0))


class GenerationTests(unittest.TestCase):
    def test_bump_persists_and_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(auth, 'SESSION_GEN', Path(tmp) / 'gen'):
                self.assertEqual(auth.current_generation(), 1)
                new = auth.bump_generation()
                self.assertEqual(new, 2)
                self.assertEqual(auth.current_generation(), 2)
                mode = (Path(tmp) / 'gen').stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)


class AuditTests(unittest.TestCase):
    def test_append_and_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(auth, 'AUDIT_LOG', Path(tmp) / 'audit.jsonl'):
                auth.audit('login_ok', login='admin', ip='1.2.3.4', detail='x')
                auth.audit('setting_change', login='admin', ip='1.2.3.4', key='effort', from_value='low', to_value='medium')
                entries = auth.recent_audit(10)
                self.assertEqual(len(entries), 2)
                self.assertEqual(entries[0]['action'], 'login_ok')
                self.assertEqual(entries[1]['key'], 'effort')
                mode = (Path(tmp) / 'audit.jsonl').stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)


class RoleCheckTests(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(auth.role_allows('admin', 'reader'))
        self.assertTrue(auth.role_allows('admin', 'reviewer'))
        self.assertTrue(auth.role_allows('admin', 'admin'))
        self.assertTrue(auth.role_allows('reviewer', 'reader'))
        self.assertFalse(auth.role_allows('reviewer', 'admin'))
        self.assertFalse(auth.role_allows('reader', 'reviewer'))
        self.assertFalse(auth.role_allows('', 'reader'))
        self.assertFalse(auth.role_allows('admin', 'bogus'))


class EndToEndTests(unittest.TestCase):
    """Drive the real dashboard FastAPI app through the sign-in flow and check role enforcement."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'transcripts').mkdir(); (root / 'reviews').mkdir(); (root / 'runs').mkdir()
        config = root / 'twilio-pilot.json'
        configure.save_config({**test_pilot.CFG, 'provider': 'anthropic', 'anthropic_model': 'claude-sonnet-5-5',
                               'effort': 'low', 'anthropic_api_key': 'sk-ant-synthetic-never-shown',
                               'transcripts': 'on'}, config)
        scenarios = root / 'scenarios.json'
        scenarios.write_text(json.dumps({'scenarios': [{'id': 'X-1', 'title': 'Example', 'tags': ['core'],
                                                        'caller': {'script': ['Hi']}}]}), encoding='utf-8')
        self.env = patch.dict(os.environ, TEST_ENV); self.env.start(); self.addCleanup(self.env.stop)
        auth.LOGIN_ATTEMPTS.clear()
        self.patches = [patch.object(configure, 'CONFIG', config),
                        patch.object(transcripts, 'FOLDER', root / 'transcripts'),
                        patch.object(review, 'FOLDER', root / 'reviews'),
                        patch.object(review, 'STATE', root / 'reviews' / 'reviewed.json'),
                        patch.object(harness, 'RUNS', root / 'runs'), patch.object(harness, 'SCENARIOS', scenarios),
                        patch.object(pilot_log, 'FEEDBACK_LOG', root / 'feedback.jsonl'),
                        patch.object(issues, 'STORE', root / 'issues.json'),
                        patch.object(dashboard, 'SETTINGS_LOG', root / 'settings-log.jsonl'),
                        patch.object(dashboard, 'PILOT_HEALTH', 'http://127.0.0.1:9/health'),
                        patch.object(auth, 'AUDIT_LOG', root / 'audit.jsonl'),
                        patch.object(auth, 'SESSION_GEN', root / 'session_generation')]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        # https:// so the Secure cookie set by /auth/login is sent back on later requests.
        self.client = TestClient(dashboard.app, base_url='https://127.0.0.1:8102')
        self.audit = root / 'audit.jsonl'

    def _login(self, login: str, password: str):
        return self.client.post('/auth/login', data={'login': login, 'password': password, 'next': '/'}, follow_redirects=False)

    def test_sign_in_sets_cookie_and_redirects(self):
        response = self._login('admin', 'admin-pw')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/')
        self.assertIn(auth.SESSION_COOKIE, response.cookies)
        home = self.client.get('/', follow_redirects=False)
        self.assertEqual(home.status_code, 200)

    def test_wrong_password_returns_401(self):
        response = self._login('admin', 'nope')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(auth.SESSION_COOKIE, response.cookies)

    def test_unauthenticated_get_redirects_to_login(self):
        home = self.client.get('/', follow_redirects=False)
        self.assertEqual(home.status_code, 303)
        self.assertTrue(home.headers['location'].startswith('/login'))

    def test_unauthenticated_post_returns_401(self):
        response = self.client.post('/settings', data={'key': 'effort', 'value': 'medium'})
        self.assertEqual(response.status_code, 401)

    def test_reader_cannot_change_settings(self):
        self.client.cookies.set(auth.SESSION_COOKIE, auth.sign_cookie(auth.read_config(), 'reader', auth.current_generation()))
        # Reader can see settings but not change them.
        self.assertEqual(self.client.get('/settings').status_code, 200)
        self.assertEqual(self.client.post('/settings', data={'token': dashboard.TOKEN, 'key': 'effort', 'value': 'medium'}).status_code, 403)

    def test_reviewer_can_review_and_cannot_change_settings(self):
        self.client.cookies.set(auth.SESSION_COOKIE, auth.sign_cookie(auth.read_config(), 'reviewer', auth.current_generation()))
        self.assertEqual(self.client.post('/settings', data={'token': dashboard.TOKEN, 'key': 'effort', 'value': 'medium'}).status_code, 403)
        self.assertEqual(self.client.post('/reviews/run', data={'token': dashboard.TOKEN}).status_code, 403)
        self.assertEqual(self.client.post('/tests/run', data={'token': dashboard.TOKEN}).status_code, 403)
        # Reviewer can mark issues and post reviews. We don't care about the exact 200/400 here,
        # only that it isn't 403 from the role check.
        for url in ['/issues/new']:
            response = self.client.post(url, data={'token': dashboard.TOKEN, 'title': 'x'})
            self.assertNotEqual(response.status_code, 403, url)

    def test_reader_cannot_see_admin_page(self):
        self.client.cookies.set(auth.SESSION_COOKIE, auth.sign_cookie(auth.read_config(), 'reader', auth.current_generation()))
        self.assertEqual(self.client.get('/admin').status_code, 403)

    def test_admin_page_shows_logins_and_audit(self):
        self.client.cookies.set(auth.SESSION_COOKIE, auth.sign_cookie(auth.read_config(), 'admin', auth.current_generation()))
        page_text = self.client.get('/admin').text
        self.assertIn('admin', page_text); self.assertIn('reviewer', page_text); self.assertIn('reader', page_text)
        self.assertIn('Sign everyone out', page_text)

    def test_invalidate_sessions_signs_everyone_out(self):
        cfg = auth.read_config()
        self.client.cookies.set(auth.SESSION_COOKIE, auth.sign_cookie(cfg, 'admin', auth.current_generation()))
        before = auth.current_generation()
        response = self.client.post('/admin/invalidate-sessions', data={'token': dashboard.TOKEN}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login')
        self.assertEqual(auth.current_generation(), before + 1)
        # The old cookie no longer works: next GET redirects to /login. TestClient keeps the cookie, but
        # the generation has changed so the server treats it as invalid.
        self.client.cookies.set(auth.SESSION_COOKIE, auth.sign_cookie(cfg, 'admin', before))
        follow = self.client.get('/', follow_redirects=False)
        self.assertEqual(follow.status_code, 303)
        self.assertTrue(follow.headers['location'].startswith('/login'))

    def test_logout_clears_cookie(self):
        self._login('admin', 'admin-pw')
        response = self.client.post('/auth/logout', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login')
        # The Set-Cookie header should expire the session cookie.
        set_cookie = response.headers.get('set-cookie', '')
        self.assertIn(auth.SESSION_COOKIE, set_cookie)

    def test_audit_records_sign_in_and_setting_change(self):
        self._login('admin', 'admin-pw')
        self.client.post('/settings', data={'token': dashboard.TOKEN, 'key': 'effort', 'value': 'medium'}, follow_redirects=False)
        entries = auth.recent_audit(50)
        actions = [e['action'] for e in entries]
        self.assertIn('login_ok', actions); self.assertIn('setting_change', actions)
        change = next(e for e in entries if e['action'] == 'setting_change')
        self.assertEqual((change['key'], change['to_value']), ('effort', 'medium'))

    def test_login_page_is_public(self):
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Sign in', response.text)

    def test_next_restricted_to_same_site(self):
        response = self.client.post('/auth/login', data={'login': 'admin', 'password': 'admin-pw', 'next': 'https://evil/x'},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303); self.assertEqual(response.headers['location'], '/')


if __name__ == '__main__':
    unittest.main()
