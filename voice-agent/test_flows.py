"""Offline tests for the flow engine and the ticket flow. No API calls."""
import asyncio
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import claude_reply
import flows
import harness
from test_claude import CLAUDE_CFG, FakeClient, FakeStream, tool_use
import tickets
import tools

TICKET_FLOW = flows.load_flows()['log_ticket']


def run(coro):
    return asyncio.run(coro)


class DefinitionTests(unittest.TestCase):
    def test_shipped_flows_are_valid(self):
        self.assertIn('log_ticket', flows.load_flows())
        diagram = flows.mermaid(TICKET_FLOW)
        self.assertIn('confirm{', diagram); self.assertIn('caller says yes', diagram)

    def test_bad_definitions_rejected(self):
        bad_field = copy.deepcopy(TICKET_FLOW); bad_field['steps'][0]['fields'] = ['nope']
        action_first = copy.deepcopy(TICKET_FLOW); action_first['steps'] = [action_first['steps'][-1]]
        unknown_action = copy.deepcopy(TICKET_FLOW); unknown_action['steps'][-1]['action'] = 'send_sms'
        for flow in (bad_field, action_first, unknown_action):
            with self.assertRaises(flows.FlowError):
                flows.validate_definition(flow)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.session = {'caller_turns': 1}
        self.actions_run = []

        async def create_ticket(values):
            self.actions_run.append(values)
            return {'ok': True, 'ticket_logged': True, 'ticket_number': '1042'}
        self.engine = flows.FlowEngine(self.session, {'log_ticket': TICKET_FLOW}, {'create_ticket': create_ticket})

    def record(self, **values):
        return self.engine.record([{'field': k, 'value': v} for k, v in values.items()])

    def record_problem(self):
        return self.record(issue_summary='Arm on the LSPC15mf (15.6-inch) keeps dropping; caller wants a repair',
                           problem_spoken='the arm holding your screen keeps dropping')

    def fill(self):
        self.engine.start('log_ticket')
        self.record_problem()
        self.record(hospital='Royal Adelaide', bed_id='7G13101')
        return self.record(caller_name='Sam Taylor', callback_number='0400 123 456')

    def test_steps_follow_what_has_been_recorded(self):
        self.assertEqual(self.engine.start('log_ticket')['step'], 'problem')
        self.assertEqual(self.record(issue_summary='Arm keeps dropping')['step'], 'problem')  # spoken version still needed
        self.assertEqual(self.record_problem()['step'], 'location')
        self.assertEqual(self.record(hospital='Royal Adelaide')['step'], 'location')  # bed or Device ID still needed
        self.assertEqual(self.record(device_id='d79610')['step'], 'contact')
        status = self.record(caller_name='Sam', callback_number='0400123456')
        self.assertEqual(status['step'], 'confirm')
        self.assertEqual(status['readback'],
                         "Just to confirm: you're Sam at Royal Adelaide, Device ID D 7 9 6 1 0, and the best number to call you "
                         "back on is 0 4 0 0, 1 2 3, 4 5 6. The arm holding your screen keeps dropping. Is that right?")

    def test_spoken_read_back(self):
        status = self.fill()
        readback = status['readback']
        self.assertEqual(readback, "Just to confirm: you're Sam Taylor at Royal Adelaide, bed 7 G 1 3 1 0 1, and the best "
                                   "number to call you back on is 0 4 0 0, 1 2 3, 4 5 6. The arm holding your screen keeps "
                                   "dropping. Is that right?")
        for written in ('caller', '(', ';', ':', 'Callback number', '15.6'):
            self.assertNotIn(written, readback.split('confirm:', 1)[1])
        self.assertEqual(self.session['flow']['values']['issue_summary'],  # the ticket text keeps its detail
                         'Arm on the LSPC15mf (15.6-inch) keeps dropping; caller wants a repair')
        self.assertIn("I've marked it as urgent.", self.record(urgent='yes')['readback'])
        self.assertEqual(self.record(problem_spoken='the caller needs a repair')['problems'], {'problem_spoken': 'mentions_the_caller'})
        for written, said in [('black screen (orange button)', 'black screen, orange button'),
                              ('screen black; tried button', 'screen black, tried button'),
                              ('it shows: no signal.', 'it shows, no signal.')]:  # punctuation fixed in code, not bounced back
            self.assertEqual(self.record(problem_spoken=written)['problems'], {})
            self.assertEqual(self.session['flow']['values']['problem_spoken'], said)
        self.assertEqual(flows.tidy_spoken('a [b] ; c'), 'a, b, c')

    def test_read_back_template_checked(self):
        for broken, message in [(['Hi {nobody}.'], 'unknown field'), (['{caller_name'], 'not valid'), ('text', 'list'),
                                (["You're {caller_name}."], 'missing problem_spoken, hospital, callback_number')]:
            flow = copy.deepcopy(TICKET_FLOW); flow['readback'] = broken
            with self.assertRaises(flows.FlowError) as caught:
                flows.validate_definition(flow)
            self.assertIn(message, str(caught.exception))
        # A flow without a template still gets a read-back of every recorded detail.
        no_template = copy.deepcopy(TICKET_FLOW); del no_template['readback']
        engine = flows.FlowEngine({'caller_turns': 1}, {'log_ticket': flows.validate_definition(no_template)}, {})
        engine.start('log_ticket')
        status = engine.record([{'field': f, 'value': v} for f, v in [
            ('issue_summary', 'x'), ('problem_spoken', 'your screen is black'), ('hospital', 'RAH'), ('bed_id', '1'),
            ('caller_name', 'Sam'), ('callback_number', '0400 123 456')]])
        self.assertIn('Callback number: 0400 123 456', status['readback'])

    def test_spoken_characters(self):
        self.assertEqual(flows.say_characters('7G13101'), '7 G 1 3 1 0 1')
        self.assertEqual(flows.say_characters('lsp-00a1'), 'L S P, 0 0 A 1')
        self.assertEqual(flows.spoken_value({'type': 'au_phone'}, '+61489273624'), '0 4 8 9, 2 7 3, 6 2 4')
        self.assertEqual(flows.spoken_value({'type': 'au_phone'}, '+61298765432'), '0 2, 9 8 7 6, 5 4 3 2')

    def test_results_are_data_only(self):
        # Test call ref 6081: "The problem fields need a spoken sentence and a summary, so I'll record
        # those." came from how-to wording in a tool result. Results hold codes and field names only.
        asks = [f['ask'] for f in TICKET_FLOW['fields'].values()]
        results = [self.engine.start('log_ticket'), self.engine.start('log_ticket'), self.engine.start('nope'),
                   self.record(issue_summary='x'), self.record(problem_spoken='the caller needs a repair'),
                   self.record(callback_number='+44 20 7946 0000', urgent='maybe', voucher='x', issue_summary='4111 1111 1111 1111'),
                   self.engine.status(), run(self.engine.confirm('yes'))]
        self.fill(); results += [run(self.engine.confirm('yes'))]
        def strings(value):
            if isinstance(value, dict):
                return [s for k, v in value.items() if k != 'readback' for s in strings(k) + strings(v)]  # the read-back is the line to say
            if isinstance(value, list):
                return [s for v in value for s in strings(v)]
            return [value] if isinstance(value, str) else []
        said = ' '.join(s for r in results for s in strings(r))
        for ask in asks:
            self.assertNotIn(ask, said)
        for word in (' ask', 'say ', ' should', 'tell ', 'read aloud', 'please', 'e.g', 'for example', 'instead'):
            self.assertNotIn(word, said.lower(), word)
        self.assertEqual(results[0]['still_needed'], ['issue_summary', 'problem_spoken'])  # field names, not questions
        fresh = flows.FlowEngine({'caller_turns': 1}, {'log_ticket': TICKET_FLOW}, {}); fresh.start('log_ticket')
        self.assertIn(TICKET_FLOW['fields']['problem_spoken']['ask'], fresh.note())  # the asks live in the note

    def test_ticket_results_are_codes(self):
        args = {'caller_confirmed': True, 'hospital': 'Royal Adelaide', 'bed_id': '7G13101', 'device_id': '', 'caller_name': 'Sam',
                'callback_number': '0400 123 456', 'issue_summary': 'Arm keeps dropping'}
        cases = [({}, {'tickets': 'off'}), ({**args, 'caller_confirmed': False}, {}), ({**args, 'callback_number': '12'}, {}),
                 ({**args, 'issue_summary': 'card 4111 1111 1111 1111'}, {}), ({**args, 'hospital': ''}, {})]
        reasons = [run(tickets.create({}, a, c, None, lambda *_: None, 'CA-t'))['reason'] for a, c in cases]
        self.assertEqual(reasons, ['tickets_unavailable', 'not_confirmed', 'invalid_callback_number', 'card_number_detected',
                                   'missing_details: hospital'])
        with tempfile.TemporaryDirectory() as folder, patch.object(tickets, 'DRY_RUN_LOG', Path(folder) / 'd.jsonl'):
            logged = run(tickets.create({}, args, {'tickets': 'dry_run'}, None, lambda *_: None, 'CA-t'))
        self.assertEqual(set(logged), {'ticket_logged', 'ticket_number', 'dry_run'})  # no note to pass on

    def test_values_are_checked(self):
        self.engine.start('log_ticket')
        result = self.record(callback_number='+44 20 7946 0000', issue_summary='card 4111 1111 1111 1111', urgent='maybe')
        self.assertEqual(set(result['problems']), {'callback_number', 'issue_summary', 'urgent'})
        self.assertEqual(self.session['flow']['values'], {})
        self.assertEqual('not_a_field', self.engine.record([{'field': 'voucher_code', 'value': 'x'}])['problems']['voucher_code'])
        self.assertTrue(self.record(urgent='yes')['recorded'])
        self.assertIs(self.session['flow']['values']['urgent'], True)

    def test_yes_only_counts_after_the_read_back_in_a_later_turn(self):
        self.engine.start('log_ticket')
        too_early = run(self.engine.confirm('yes'))
        self.assertFalse(too_early['confirmed']); self.assertEqual(too_early['reason'], 'not_at_readback')
        self.fill()
        same_turn = run(self.engine.confirm('yes'))
        self.assertFalse(same_turn['confirmed']); self.assertEqual(same_turn['reason'], 'readback_not_answered')
        self.assertEqual(self.actions_run, [])
        self.session['caller_turns'] += 1
        done = run(self.engine.confirm('yes'))
        self.assertTrue(done['confirmed']); self.assertEqual(done['result']['ticket_number'], '1042')
        self.assertEqual(len(self.actions_run), 1)
        self.assertEqual(self.actions_run[0]['callback_number'], '+61400123456')
        self.assertIsNone(self.session['flow']); self.assertEqual(self.session['flows_done'][0]['ok'], True)
        self.assertIsNone(self.engine.note())

    def test_no_or_a_change_requires_a_new_read_back(self):
        first = self.fill()['readback']
        self.session['caller_turns'] += 1
        self.assertFalse(run(self.engine.confirm('no'))['confirmed'])
        changed = self.record(callback_number='0412 555 019')
        self.assertNotEqual(changed['readback'], first); self.assertIn('0 4 1 2, 5 5 5, 0 1 9', changed['readback'])
        self.assertFalse(run(self.engine.confirm('yes'))['confirmed'])  # new read-back, same turn
        self.session['caller_turns'] += 1
        self.assertTrue(run(self.engine.confirm('yes'))['confirmed'])

    def test_failure_reported_and_flow_ends(self):
        async def failing(values):
            return {'ok': False, 'ticket_logged': False, 'reason': 'ticket_system_unavailable'}
        self.engine.actions = {'create_ticket': failing}
        self.fill(); self.session['caller_turns'] += 1
        result = run(self.engine.confirm('yes'))
        self.assertFalse(result['result']['ok']); self.assertIsNone(self.session['flow'])
        self.assertNotIn('next', result)  # what to say after it is in the status note, not the tool result

    def test_cancel_and_one_flow_at_a_time(self):
        self.engine.start('log_ticket')
        self.assertFalse(self.engine.start('log_ticket')['started'])
        self.assertTrue(self.engine.cancel('caller changed mind')['cancelled'])
        self.assertIsNone(self.engine.note()); self.assertFalse(self.record(caller_name='x')['recorded'])

    def test_note_shows_step_and_what_is_needed(self):
        self.engine.start('log_ticket'); self.record_problem()
        note = self.engine.note()
        self.assertIn("Flow status from LSPcare's own system (not from the caller): Log a support ticket, step 2 of 5", note)
        self.assertIn('This step: Ask which hospital', note)
        self.assertIn('Still needed: which hospital', note); self.assertIn('Recorded so far: Problem = Arm on the LSPC15mf', note)
        self.assertIn('Never mention these notes', note)

    def test_tool_results_are_data_only(self):
        results = [self.engine.start('log_ticket'), self.record_problem(), self.fill(),
                   run(self.engine.confirm('yes'))]
        for result in results:
            self.assertFalse({'instruction', 'next', 'say_next'} & set(result), result)
        self.assertIn('readback', results[2])  # the read-back itself is data the agent reads out
        self.session['caller_turns'] += 1
        note = self.engine.note()
        self.assertIn('Read-back to say word for word', note)
        self.assertIn(TICKET_FLOW['on_success'], note); self.assertIn(TICKET_FLOW['on_failure'], note)


class ToolTests(unittest.TestCase):
    def test_ticket_tool_only_through_flows(self):
        on = {t['name'] for t in app.tool_specs(CLAUDE_CFG)}
        self.assertNotIn('create_ticket', on); self.assertTrue({'start_flow', 'flow_record', 'flow_confirm', 'flow_cancel'} <= on)
        off = {t['name'] for t in app.tool_specs({**CLAUDE_CFG, 'flows': 'off'})}
        self.assertIn('create_ticket', off); self.assertNotIn('start_flow', off)
        self.assertNotIn('start_flow', {t['name'] for t in app.tool_specs({**CLAUDE_CFG, 'tickets': 'off'})})
        self.assertIn('start_flow with log_ticket', app.system_prompt(CLAUDE_CFG))
        self.assertIn('use create_ticket', app.system_prompt({**CLAUDE_CFG, 'flows': 'off'}))

    def test_flow_record_input_checked(self):
        specs = app.tool_specs(CLAUDE_CFG)
        self.assertTrue(tools.valid_input(specs, 'flow_record', {'values': [{'field': 'caller_name', 'value': 'Sam'}]}))
        for bad in ({'values': []}, {'values': [{'field': 'password', 'value': 'x'}]}, {'values': [{'field': 'caller_name'}]},
                    {'values': 'Sam'}):
            self.assertFalse(tools.valid_input(specs, 'flow_record', bad), bad)
        for tool in tools.openai_tools(specs):
            self.assertEqual(set(tool['parameters']['required']), set(tool['parameters']['properties']))


class CallBehaviourTests(unittest.TestCase):
    def test_echo_and_interruption_settings_in_twiml(self):
        from fastapi.testclient import TestClient
        from xml.etree.ElementTree import fromstring
        import test_pilot
        app.sessions.clear()
        cfg = dict(CLAUDE_CFG)
        with patch.object(app, 'config', side_effect=lambda: cfg):
            client = TestClient(app.app)
            post = lambda sid: client.post('/voice', data={'AccountSid': cfg['account_sid'], 'CallSid': sid},
                                           headers={'X-Twilio-Signature': test_pilot.RequestValidator(cfg['auth_token']).compute_signature(
                                               cfg['public_base'] + '/voice', {'AccountSid': cfg['account_sid'], 'CallSid': sid})})
            relay = fromstring(post('CA' + '1' * 32).text).find('Connect/ConversationRelay')
            self.assertEqual((relay.get('echoPromptFiltering'), relay.get('welcomeGreetingInterruptible'),
                              relay.get('interruptSensitivity'), relay.get('ignoreBackchannel')), ('false', 'none', 'medium', 'true'))
            self.assertEqual(relay.get('events'), 'speaker-events tokens-played')
            cfg['interruptSensitivity'] = 'low'
            relay = fromstring(post('CA' + '2' * 32).text).find('Connect/ConversationRelay')
            self.assertEqual(relay.get('interruptSensitivity'), 'low')
        app.sessions.clear()

    def test_question_before_a_tool_is_asked_once(self):
        # Text before a tool call is held and dropped, so the question is only asked after the tool.
        fake = FakeClient([FakeStream('Which hospital are you at?', content=[tool_use('start_flow', {'flow_id': 'log_ticket'})]),
                           FakeStream('Which hospital are you at, Jo?')])
        session = {}
        events = []
        heard = []
        async def on_text(delta): heard.append(delta)
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
            convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-q', session, on_text,
                                           lambda k, d: events.append((k, d)))
            kind, spoken = run(convo.respond('The arm on my screen keeps dropping, please log a ticket.'))
        self.assertEqual(spoken, 'Which hospital are you at, Jo?')
        self.assertEqual(''.join(heard), spoken)  # the caller hears it once
        self.assertIn(('suppressed', {'text': 'Which hospital are you at?', 'reason': 'narration before a tool call'}), events)
        self.assertEqual(convo.history[-1]['content'], spoken)
        result = json.loads(fake.requests[1]['messages'][-1]['content'][0]['content'])
        self.assertNotIn('already_asked_caller', result)
        self.assertNotIn('say_next', result)  # data only: no instruction inside a tool result

    def test_narration_before_a_tool_is_not_spoken(self):
        # The failing call of 28 Nov: remarks between tool calls were spoken, caller in the third person.
        fake = FakeClient([
            FakeStream("I can log a ticket for you. Since a black screen was the issue, I'll identify the terminal.",
                       content=[tool_use('start_flow', {'flow_id': 'log_ticket'})]),
            FakeStream("Sam says he's dealt with this before, so I'll record the problem now.",
                       content=[tool_use('flow_record', {'values': [{'field': 'issue_summary', 'value': 'Black screen'}, {'field': 'problem_spoken', 'value': 'your screen is black'}]}, 'toolu_2')]),
            FakeStream('Understood, Sam. Which hospital are you at?')])
        events, heard = [], []
        async def on_text(delta): heard.append(delta)
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
            convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-n', {}, on_text,
                                           lambda k, d: events.append((k, d)))
            kind, spoken = run(convo.respond('I just want to resubmit a ticket for repair.'))
        self.assertEqual(''.join(heard), 'Understood, Sam. Which hospital are you at?')
        self.assertEqual(spoken, ''.join(heard))
        dropped = [d['text'] for k, d in events if k == 'suppressed' and d['reason'] == 'narration before a tool call']
        self.assertEqual(dropped, ["I can log a ticket for you. Since a black screen was the issue, I'll identify the terminal.",
                                   "Sam says he's dealt with this before, so I'll record the problem now."])

    def test_narration_as_the_opening_sentence_is_held(self):
        # Harness run of 28 Nov: "Let me record your number and see how the ticket handles the missing bed."
        def converse(*streams):
            fake = FakeClient(list(streams))
            heard = []
            async def on_text(delta): heard.append(delta)
            with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
                convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-o', {}, on_text)
                run(convo.respond('Can you log it under my mobile?'))
            return ''.join(heard)
        record = tool_use('lookup_device', {'hospital': 'RAH', 'bed_id': '1', 'device_id': ''})
        with patch.object(app, 'lookup', return_value={'device_found': True}):
            self.assertEqual(converse(FakeStream('Let me record your number and see how the ticket handles it.', content=[record]),
                                      FakeStream("Thanks, Sam. What's your bed number?")),
                             "Got it. Thanks, Sam. What's your bed number?")  # narration dropped, a holding line instead
            # An opening like that which is the answer itself (no tool call) is still spoken.
            self.assertEqual(converse(FakeStream("I'll log a ticket for the support team. What's your bed number?")),
                             "I'll log a ticket for the support team. What's your bed number?")
            self.assertEqual(converse(FakeStream('Sure.')), 'Sure.')

    def test_answer_and_goodbye_are_spoken_in_full(self):
        fake = FakeClient([FakeStream('Thanks for calling. Have a good day.', content=[tool_use('finish_call', {'reason': 'goodbye'})])])
        heard = []
        async def on_text(delta): heard.append(delta)
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
            convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-g', {}, on_text)
            self.assertEqual(run(convo.respond("That's all, bye."))[0], 'finish')
        self.assertEqual(''.join(heard), 'Thanks for calling. Have a good day.')

    def test_unheard_read_back_cannot_be_confirmed(self):
        readback = "Just to confirm: you're Sam at Royal Adelaide, bed 7 G 1. Is that right?"
        lookup = tool_use('lookup_device', {'hospital': 'RAH', 'bed_id': '1', 'device_id': ''})
        def after(*streams):
            session = {'caller_turns': 3, 'flow': {'id': 'log_ticket', 'values': {}, 'confirmed': False,
                                                   'readback': readback, 'readback_turn': 4, 'result': None}}
            async def on_text(_): pass
            with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=FakeClient(list(streams))), \
                    patch('sys.stderr', io.StringIO()), patch.object(app, 'lookup', return_value={'device_found': True}):
                run(app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-s', session, on_text).respond('0400 123 456'))
            return session['flow']['readback']
        # Said before a tool call, so dropped with that round: discarded, so a yes can't confirm it.
        self.assertIsNone(after(FakeStream(readback, content=[lookup]), FakeStream('Is that your mobile?')))
        # Said as the answer: kept.
        self.assertEqual(after(FakeStream(content=[lookup]), FakeStream('Thanks. ' + readback)), readback)

    def test_no_guard_when_no_question_was_asked(self):
        fake = FakeClient([FakeStream('Please leave the arm as it is.', content=[tool_use('start_flow', {'flow_id': 'log_ticket'})]),
                           FakeStream('Which hospital are you at?')])
        async def on_text(_): pass
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
            convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-r', {}, on_text)
            kind, spoken = run(convo.respond('The arm keeps dropping.'))
        self.assertEqual(spoken, 'Which hospital are you at?')  # the remark before the tool is dropped
        self.assertNotIn('already_asked_caller', json.loads(fake.requests[1]['messages'][-1]['content'][0]['content']))


class HoldingLineTests(unittest.TestCase):
    """Fixed lines spoken by code while a reply waits on a tool and a second model response."""

    def call(self, streams, lines, session=None, cfg=None):
        fake = FakeClient(list(streams))
        heard, events, turns = [], [], []
        async def on_text(delta): heard.append(delta)
        with tempfile.TemporaryDirectory() as folder, patch.object(tickets, 'DRY_RUN_LOG', Path(folder) / 'd.jsonl'), \
                patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), \
                patch.object(app, 'lookup', return_value={'device_found': True}), patch('sys.stderr', io.StringIO()):
            convo = app.build_conversation(cfg or {**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-h',
                                           session if session is not None else {}, on_text, lambda k, d: events.append((k, d)))
            async def run_all():
                for line in lines:
                    start = len(heard)
                    await convo.respond(line)
                    turns.append(''.join(heard[start:]))
            run(run_all())
        return turns, events, convo, fake

    def at_read_back(self, readback_turn):
        values = {'issue_summary': 'Arm keeps dropping', 'problem_spoken': 'the arm keeps dropping', 'hospital': 'Royal Adelaide',
                  'bed_id': '7G13101', 'caller_name': 'Sam', 'callback_number': '+61400123456'}
        return {'caller_turns': 3, 'flow': {'id': 'log_ticket', 'values': values, 'confirmed': False,
                                           'readback': 'Just to confirm…', 'readback_turn': readback_turn, 'result': None}}

    def test_ticket_line_spoken_when_logging_starts(self):
        confirm = tool_use('flow_confirm', {'answer': 'yes'})
        turns, events, convo, _ = self.call([FakeStream(content=[confirm]), FakeStream('Your ticket number is 1 0 4 2.')],
                                            ["Yes, that's right."], session=self.at_read_back(3))
        self.assertEqual(turns, ['One moment while I log that for you. Your ticket number is 1 0 4 2.'])
        self.assertEqual(convo.history[-1]['content'], turns[0])  # in history, so the model won't repeat it
        self.assertFalse([d for k, d in events if k == 'suppressed'])  # never dropped as narration

    def test_no_ticket_line_when_nothing_is_logged(self):
        confirm = tool_use('flow_confirm', {'answer': 'yes'})  # too early: the read-back was in this same turn
        turns, _, _, _ = self.call([FakeStream(content=[confirm]), FakeStream('Is that all correct?')], ['Yes.'],
                                   session=self.at_read_back(4))
        self.assertEqual(turns, ['Is that all correct?'])

    def test_acknowledgement_after_a_silent_record_only(self):
        record = lambda n: tool_use('flow_record', {'values': [{'field': 'hospital', 'value': 'Royal Adelaide'}]}, f'toolu_r{n}')
        guide = tool_use('get_guide', {'guide_id': 'KB-TERMINAL-LSPC15MF'}, 'toolu_g')
        turns, events, convo, fake = self.call(
            [FakeStream(content=[record(1)]), FakeStream("What's your bed number?"),            # silent record: ack
             FakeStream(content=[record(2)]), FakeStream("And your name?"),                     # again: a different ack
             FakeStream('Thanks, Sam.', content=[record(3)]), FakeStream('And your number?'),   # words before a tool: dropped
             FakeStream(content=[guide]), FakeStream('Press the orange button once.')],        # opening a guide: none
            ['Royal Adelaide.', 'Bed 7G13101.', "It's Sam.", 'It has an orange button.'],
            session={'caller_turns': 0, 'flow': {'id': 'log_ticket', 'values': {}, 'confirmed': False, 'readback': None,
                                                 'readback_turn': None, 'result': None}})
        self.assertEqual(turns, ["Got it. What's your bed number?", 'Thanks. And your name?',
                                 'Okay. And your number?', 'Press the orange button once.'])
        first_result = json.loads(fake.requests[1]['messages'][-1]['content'][0]['content'])
        self.assertNotIn('already_asked_caller', first_result)
        self.assertEqual([d['text'] for k, d in events if k == 'suppressed'], ['Thanks, Sam.'])
        convo.interrupted('Got it.')  # the caller spoke over the rest: only what was heard is kept
        self.assertEqual(convo.history[-1]['content'], 'Got it.')

    def test_no_double_acknowledgement(self):
        # Harness run of 28 Nov: "Thanks. Thanks. What's your bed number?"
        record = lambda n: tool_use('flow_record', {'values': [{'field': 'hospital', 'value': 'Royal Adelaide'}]}, f'toolu_{n}')
        flow = {'caller_turns': 0, 'flow': {'id': 'log_ticket', 'values': {}, 'confirmed': False, 'readback': None,
                                            'readback_turn': None, 'result': None}}
        turns, _, _, _ = self.call(
            [FakeStream(content=[record(1)]), FakeStream("Thanks. What's your bed number?"),
             FakeStream(content=[record(2)]), FakeStream('Thanks, Sam. And your number?'),   # not a bare repeat: kept
             FakeStream(content=[record(3)]), FakeStream('Okay.')],                          # only that: said once
            ['Royal Adelaide.', 'Bed 7G13101.', "It's Sam."], session=flow)
        self.assertEqual(turns, ["Got it. What's your bed number?", 'Thanks. Thanks, Sam. And your number?', 'Okay.'])

    def test_whole_round_held_until_it_ends(self):
        # Test call ref 6081: the first sentence of a round that ended in a tool call was spoken.
        lookup = tool_use('lookup_device', {'hospital': 'Royal Adelaide', 'bed_id': '7G13101', 'device_id': ''})
        turns, events, _, _ = self.call([FakeStream("The value sounds like a bed ID, so I'll check it first. ", content=[lookup]),
                                         FakeStream("That's the LSPC15mf in bed 7G13101. What's happening with it?")],
                                        ['7G13101.'])
        self.assertEqual(turns, ["Got it. That's the LSPC15mf in bed 7G13101. What's happening with it?"])
        self.assertEqual([d['text'] for k, d in events if k == 'suppressed'],
                         ["The value sounds like a bed ID, so I'll check it first."])


class TicketFlowConversationTests(unittest.TestCase):
    """A four-turn call through the real engine with a fake model, including a too-early confirm."""

    def test_full_ticket_flow(self):
        def use(name, args, n):
            return FakeStream(content=[tool_use(name, args, f'toolu_{n}')])
        record = lambda pairs: {'values': [{'field': k, 'value': v} for k, v in pairs]}
        fake = FakeClient([
            use('start_flow', {'flow_id': 'log_ticket'}, 1),
            use('flow_record', record([('issue_summary', 'Arm keeps dropping'), ('problem_spoken', 'the arm holding your screen keeps dropping'), ('steps_tried', '')]), 2),
            FakeStream("Please don't adjust it. Which hospital are you in?"),
            use('flow_record', record([('hospital', 'Royal Adelaide'), ('bed_id', '7G13101')]), 3),
            FakeStream('And your name?'),
            use('flow_record', record([('caller_name', 'Sam Taylor'), ('callback_number', '0400 123 456')]), 4),
            use('flow_confirm', {'answer': 'yes'}, 5),  # too early: the caller hasn't answered the read-back
            FakeStream('Just to confirm, Sam Taylor, Royal Adelaide bed 7G13101, 0400 123 456. Is that right?'),
            use('flow_confirm', {'answer': 'yes'}, 6),
            FakeStream('Your ticket is logged.'),
        ])
        session = {'reference': '4242'}
        events = []
        async def on_text(_): pass
        with tempfile.TemporaryDirectory() as folder, patch.object(tickets, 'DRY_RUN_LOG', Path(folder) / 'd.jsonl'), \
                patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), \
                patch.object(app, 'lookup', return_value={'device_found': True}), patch('sys.stderr', io.StringIO()):
            cfg = {**CLAUDE_CFG, 'tickets': 'dry_run'}
            convo = app.build_conversation(cfg, 'CA-flow', session, on_text, lambda k, d: events.append((k, d)))
            async def call():
                for line in ['The arm on my screen keeps dropping, can someone fix it?', 'Royal Adelaide, bed 7G13101.',
                             'Sam Taylor, 0400 123 456.', "Yes, that's right."]:
                    await convo.respond(line)
            run(call())
        tool_results = [d['result'] for k, d in events if k == 'tool']
        early, final = tool_results[4], tool_results[5]
        self.assertFalse(early['confirmed']); self.assertEqual(early['reason'], 'readback_not_answered')
        self.assertTrue(final['confirmed']); self.assertTrue(final['result']['ticket_logged'])
        self.assertEqual(final['result']['args']['callback_number'], '+61400123456')
        self.assertEqual(session['ticket']['status'], 'logged'); self.assertIsNone(session['flow'])
        # Status notes are system messages at the start of a turn, right after the caller's words, and
        # stay in history: a note sent once is in every later request (append-only), never removed.
        sent_notes = []
        for request in fake.requests:
            messages = request['messages']
            notes = [m['content'] for m in messages if m['role'] == 'system']
            self.assertEqual(notes[:len(sent_notes)], sent_notes)  # nothing earlier disappears
            sent_notes = notes
            for i, m in enumerate(messages):
                if m['role'] == 'system':
                    self.assertEqual(messages[i - 1]['role'], 'user')  # after the caller's words...
                    self.assertTrue(isinstance(messages[i - 1]['content'], str))  # ...never after a tool result
                if m['role'] == 'user' and isinstance(m['content'], list):
                    for block in m['content']:
                        self.assertNotIn('"instruction"', block['content'])  # tool results carry data only
        self.assertTrue(any('Flow status' in n for n in sent_notes))
        self.assertTrue(any('Read-back to say' in n for n in sent_notes))
        stored = [m['content'] for m in convo.history if m['role'] == 'system']
        self.assertTrue(stored[0].startswith("Call details from LSPcare's own system: this call's reference is 4 2 4 2."))
        self.assertTrue(any('Flow status' in n for n in stored))
        self.assertEqual(session['caller_turns'], 4)
        # The next turn says the flow has ended, so the stored notes of a finished flow are not followed.
        later = FakeClient([FakeStream('Anything else?')])
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=later), patch('sys.stderr', io.StringIO()):
            run(convo.respond('Thanks.'))
        self.assertIn('the flow has ended', later.requests[0]['messages'][-1]['content'])
        # The harness counts flow-logged tickets like direct ones.
        run_record = {'transcript': [], 'tools': [{'name': d['name'], 'args': d['args'], 'result': d['result']} for k, d in events if k == 'tool'],
                      'finish': None, 'turns': [], 'error': None}
        checks = harness.hard_checks({'ticket': 'created', 'ticket_args': {'bed_id': '7G13101'}}, run_record)
        self.assertTrue(all(c['passed'] for c in checks), [c for c in checks if not c['passed']])

    def test_card_number_inside_flow_record_is_caught_by_harness(self):
        record = {'transcript': [], 'finish': None, 'turns': [], 'error': None,
                  'tools': [{'name': 'flow_record', 'args': {'values': [{'field': 'issue_summary', 'value': 'card 4111 1111 1111 1111'}]},
                             'result': {'problems': {'issue_summary': 'card'}}}]}
        card = [c for c in harness.hard_checks({}, record) if c['check'] == 'no card numbers passed to tools'][0]
        self.assertFalse(card['passed'])


if __name__ == '__main__':
    unittest.main()
