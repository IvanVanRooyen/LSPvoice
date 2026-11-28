"""Offline tests: caller answers are never lost to echo filtering, relay messages are logged, and a
silent caller is asked again once. Server test call of 28 Nov: "Royal Adelaide" never arrived."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from xml.etree.ElementTree import fromstring

from fastapi.testclient import TestClient

import app
import claude_reply
import listening
import test_pilot
from test_claude import CLAUDE_CFG, FakeClient, FakeStream
import transcripts

QUESTION = 'Thanks, Sam. Which hospital are you at, Royal Adelaide or Royal North Shore?'


class ListeningRulesTests(unittest.TestCase):
    def test_answers_that_repeat_the_agent_are_never_echo(self):
        for answer in ['Royal Adelaide.', 'Royal Adelaide', 'Yes.', "Yes, that's right.", 'Sam.', '7G13101',
                       'Royal Adelaide, bed 7G13101.', "It's the Royal Adelaide."]:
            self.assertFalse(listening.is_echo(answer, QUESTION), answer)
        self.assertFalse(listening.is_echo("Yes, that's right", 'Just to confirm: you are Sam. Is that right?'))

    def test_the_agents_own_words_are_echo(self):
        self.assertTrue(listening.is_echo('Royal Adelaide or Royal North Shore', QUESTION))
        self.assertTrue(listening.is_echo("Which hospital are you at", QUESTION))
        self.assertFalse(listening.is_echo('hospital are you at Royal', 'Which hospital are we at? Royal'))

    def test_reprompt_wording(self):
        self.assertEqual(listening.reprompt_for(QUESTION), "Sorry, I didn't catch that. Which hospital are you at, "
                                                           "Royal Adelaide or Royal North Shore?")
        self.assertEqual(listening.reprompt_for('The bed name is on the wall. Can you see it?'),
                         "Sorry, I didn't catch that. The bed name is on the wall. Can you see it?")
        readback = "Just to confirm: you're Sam at Royal Adelaide. Is that right?"
        self.assertEqual(listening.reprompt_for('Got it. ' + readback, readback), "Sorry, I didn't catch that. " + readback)
        self.assertIsNone(listening.reprompt_for('Your ticket is logged.'))  # no question: nothing to re-ask

    def test_playback_estimate_grows_with_the_words(self):
        self.assertLess(listening.playback_seconds('Yes.'), listening.playback_seconds(QUESTION))

    def test_relay_record_leaves_out_private_fields(self):
        record = listening.relay_record({'type': 'setup', 'callSid': 'CA1', 'from': '+61400123456', 'to': '+61255503624',
                                         'customParameters': {'nonce': 'secret'}, 'sessionId': 'VX1', 'x': 'y' * 500})
        self.assertEqual(set(record), {'type', 'sessionId', 'x'}); self.assertLessEqual(len(record['x']), 300)
        self.assertEqual(listening.relay_record('garbage'), {'type': 'unreadable'})


class RelayListeningTests(unittest.TestCase):
    _base = test_pilot.PilotTests
    post, begin, ws, setup = _base.post, _base.begin, _base.ws, _base.setup

    def setUp(self):
        app.sessions.clear()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        cfg = {**CLAUDE_CFG, 'transcripts': 'on', 'tickets': 'dry_run'}
        for p in [patch.object(app, 'config', side_effect=lambda: cfg), patch.object(transcripts, 'FOLDER', Path(self.tmp.name)),
                  patch('sys.stderr', io.StringIO())]:
            p.start(); self.addCleanup(p.stop)
        self.client = TestClient(app.app); self.sid = 'CA' + '5' * 32

    def call(self, streams):
        self.fake = FakeClient(streams)
        p = patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=self.fake); p.start(); self.addCleanup(p.stop)
        nonce = self.begin()
        return nonce

    def reply(self, ws):
        """Text tokens until the end of a reply."""
        out = []
        while True:
            message = ws.receive_json()
            if message['type'] != 'text':
                return ''.join(out), message
            out.append(message['token'])
            if message['last']:
                return ''.join(out), message

    def events(self):
        path = next(Path(self.tmp.name).glob('*.jsonl'))
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_echo_filtering_off_and_events_on_in_twiml(self):
        relay = fromstring(self.post('/voice').text).find('Connect/ConversationRelay')
        self.assertEqual(relay.get('echoPromptFiltering'), 'false')
        self.assertEqual(relay.get('events'), 'speaker-events tokens-played')

    def test_answer_repeating_the_agent_reaches_it_and_echo_does_not(self):
        nonce = self.call([FakeStream(QUESTION), FakeStream('Thanks. What is your bed number?')])
        with self.ws() as ws:
            self.setup(ws, nonce)
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': "It's Sam."})
            self.assertEqual(self.reply(ws)[0], QUESTION)
            # During playback: the agent's own words picked up by the microphone are ignored...
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': 'Royal Adelaide or Royal North Shore'})
            # ...but the caller's answer, made only of the agent's words, gets through.
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': 'Royal Adelaide.'})
            self.assertEqual(self.reply(ws)[0], 'Thanks. What is your bed number?')
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.fake.requests[1]['messages'][-1]['content'], 'Royal Adelaide.')
        kinds = [e['kind'] for e in self.events()]
        self.assertIn('echo_ignored', kinds)

    def test_every_relay_message_is_logged_and_only_caller_messages_count(self):
        nonce = self.call([FakeStream('Hi Sam. How can I help?')])
        with patch.object(app, 'MAX_EVENTS', 2), self.ws() as ws:
            self.setup(ws, nonce)
            for n in range(5):  # speaker and played-token events don't use up the caller limit
                ws.send_json({'type': 'info', 'name': 'tokensPlayed', 'value': f'word {n}'})
            ws.send_json({'type': 'something-new', 'detail': 'x'})
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': "It's Sam."})
            self.assertEqual(self.reply(ws)[0], 'Hi Sam. How can I help?')
        relay = [e for e in self.events() if e['kind'] == 'relay']
        self.assertEqual([e['type'] for e in relay], ['setup'] + ['info'] * 5 + ['something-new', 'prompt'])
        self.assertNotIn('customParameters', relay[0]); self.assertNotIn('nonce', json.dumps(relay))

    def fast(self, words_per_second=1000.0, grace=0.05):
        stack = contextlib.ExitStack()
        for name, value in [('SILENCE_SECONDS', 0.3), ('PLAYBACK_MARGIN', 0.0), ('WORDS_PER_SECOND', words_per_second),
                            ('EVENT_GRACE', grace)]:
            stack.enter_context(patch.object(listening, name, value))
        return stack

    def speaking(self, ws, value):
        ws.send_json({'type': 'info', 'name': 'agentSpeaking', 'value': value})

    def test_re_ask_timed_from_agent_speaking_off(self):
        # Ref 6081: the re-ask came ~5 s late, timed from an estimate. Twilio says when playback ends.
        nonce = self.call([FakeStream(QUESTION), FakeStream('Thanks.')])
        with self.fast(words_per_second=0.01), self.ws() as ws:  # the estimate alone would be minutes
            self.setup(ws, nonce)
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': "It's Sam."})
            self.assertEqual(self.reply(ws)[0], QUESTION)
            self.speaking(ws, 'on'); time.sleep(0.5)  # still playing: no re-ask yet
            self.speaking(ws, 'off'); off = time.monotonic()
            self.assertTrue(self.reply(ws)[0].startswith("Sorry, I didn't catch that."))
            self.assertLess(time.monotonic() - off, 1.5)
            waited = time.monotonic() - off
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': 'Royal Adelaide.'})
            self.reply(ws)
        self.assertGreaterEqual(waited, 0.3)
        self.assertEqual([e['timed_from'] for e in self.events() if e['kind'] == 'silence'], ['playback_event'])

    def test_agent_speaking_again_restarts_the_wait(self):
        nonce = self.call([FakeStream(QUESTION), FakeStream('Thanks.')])
        with self.fast(words_per_second=0.01), self.ws() as ws:
            self.setup(ws, nonce)
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': "It's Sam."})
            self.reply(ws)
            self.speaking(ws, 'on'); self.speaking(ws, 'off')
            time.sleep(0.15); self.speaking(ws, 'on')  # playback resumed inside the silence window
            time.sleep(0.6)
            self.speaking(ws, 'off'); off = time.monotonic()
            self.assertTrue(self.reply(ws)[0].startswith("Sorry, I didn't catch that."))
            waited = time.monotonic() - off
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': 'Royal Adelaide.'})
            self.reply(ws)
        self.assertGreaterEqual(waited, 0.3)  # counted from the second "off", not the first
        self.assertEqual([e['kind'] for e in self.events()].count('reprompt'), 1)

    def test_silent_caller_is_asked_again_once(self):
        nonce = self.call([FakeStream(QUESTION), FakeStream('Thanks. What is your bed number?')])
        with self.fast(), self.ws() as ws:  # no speaker events: the estimate is the fallback
            self.setup(ws, nonce)
            greeting_again, _ = self.reply(ws)  # nothing said after the greeting
            self.assertEqual(greeting_again, app.GREETING_REPROMPT)
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': "It's Sam."})
            self.assertEqual(self.reply(ws)[0], QUESTION)
            self.assertEqual(self.reply(ws)[0], "Sorry, I didn't catch that. Which hospital are you at, Royal Adelaide "
                                                "or Royal North Shore?")
            time.sleep(0.8)  # only once: no second re-ask
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': 'Royal Adelaide.'})
            self.assertEqual(self.reply(ws)[0], 'Thanks. What is your bed number?')
        self.assertEqual(self.fake.requests[0]['messages'][0]['role'], 'user')  # the greeting re-ask isn't stored
        history = self.fake.requests[1]['messages']
        self.assertEqual([m['role'] for m in history][-3:], ['assistant', 'assistant', 'user'])  # the re-ask is in history
        self.assertTrue(history[-2]['content'].startswith("Sorry, I didn't catch that."))
        self.assertEqual([e['kind'] for e in self.events()].count('reprompt'), 2)  # greeting + question
        self.assertEqual({e['timed_from'] for e in self.events() if e['kind'] == 'silence'}, {'estimate'})

    def test_caller_speaking_cancels_the_re_ask(self):
        nonce = self.call([FakeStream(QUESTION), FakeStream('Thanks. What is your bed number?')])
        with self.fast(), self.ws() as ws:
            self.setup(ws, nonce)
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': "It's Sam."})
            self.assertEqual(self.reply(ws)[0], QUESTION)
            self.speaking(ws, 'on'); self.speaking(ws, 'off')
            ws.send_json({'type': 'info', 'name': 'clientSpeaking', 'value': 'on'})
            time.sleep(0.8)
            ws.send_json({'type': 'prompt', 'last': True, 'voicePrompt': 'Royal Adelaide.'})
            self.assertEqual(self.reply(ws)[0], 'Thanks. What is your bed number?')  # no re-ask came first
        self.assertNotIn('reprompt', [e['kind'] for e in self.events()])


if __name__ == '__main__':
    unittest.main()
