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

    def fill(self):
        self.engine.start('log_ticket')
        self.record(issue_summary='Arm keeps dropping')
        self.record(hospital='Royal Adelaide', bed_id='7G13101')
        return self.record(caller_name='Sam Taylor', callback_number='0400 123 456')

    def test_steps_follow_what_has_been_recorded(self):
        self.assertEqual(self.engine.start('log_ticket')['step'], 'problem')
        self.assertEqual(self.record(issue_summary='Arm keeps dropping')['step'], 'location')
        self.assertEqual(self.record(hospital='Royal Adelaide')['step'], 'location')  # bed or Device ID still needed
        self.assertEqual(self.record(device_id='d79610')['step'], 'contact')
        status = self.record(caller_name='Sam', callback_number='0400123456')
        self.assertEqual(status['step'], 'confirm')
        self.assertIn('Callback number: 0400 123 456', status['readback'])

    def test_values_are_checked(self):
        self.engine.start('log_ticket')
        result = self.record(callback_number='+44 20 7946 0000', issue_summary='card 4111 1111 1111 1111', urgent='maybe')
        self.assertEqual(set(result['problems']), {'callback_number', 'issue_summary', 'urgent'})
        self.assertEqual(self.session['flow']['values'], {})
        self.assertIn('not a field', self.engine.record([{'field': 'voucher_code', 'value': 'x'}])['problems']['voucher_code'])
        self.assertTrue(self.record(urgent='yes')['recorded'])
        self.assertIs(self.session['flow']['values']['urgent'], True)

    def test_yes_only_counts_after_the_read_back_in_a_later_turn(self):
        self.engine.start('log_ticket')
        too_early = run(self.engine.confirm('yes'))
        self.assertFalse(too_early['confirmed']); self.assertIn('not at the read-back step', too_early['reason'])
        self.fill()
        same_turn = run(self.engine.confirm('yes'))
        self.assertFalse(same_turn['confirmed']); self.assertIn('wait for the caller', same_turn['reason'])
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
        self.assertNotEqual(changed['readback'], first); self.assertIn('0412 555 019', changed['readback'])
        self.assertFalse(run(self.engine.confirm('yes'))['confirmed'])  # new read-back, same turn
        self.session['caller_turns'] += 1
        self.assertTrue(run(self.engine.confirm('yes'))['confirmed'])

    def test_failure_reported_and_flow_ends(self):
        async def failing(values):
            return {'ok': False, 'ticket_logged': False, 'reason': 'ticket_system_unavailable'}
        self.engine.actions = {'create_ticket': failing}
        self.fill(); self.session['caller_turns'] += 1
        result = run(self.engine.confirm('yes'))
        self.assertEqual(result['next'], TICKET_FLOW['on_failure']); self.assertIsNone(self.session['flow'])

    def test_cancel_and_one_flow_at_a_time(self):
        self.engine.start('log_ticket')
        self.assertFalse(self.engine.start('log_ticket')['started'])
        self.assertTrue(self.engine.cancel('caller changed mind')['cancelled'])
        self.assertIsNone(self.engine.note()); self.assertFalse(self.record(caller_name='x')['recorded'])

    def test_note_shows_step_and_what_is_needed(self):
        self.engine.start('log_ticket'); self.record(issue_summary='Arm keeps dropping')
        note = self.engine.note()
        self.assertIn('ACTIVE FLOW: Log a support ticket (step 2 of 5)', note)
        self.assertIn('Still needed: which hospital', note); self.assertIn('Recorded: Problem = Arm keeps dropping', note)


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
                              relay.get('interruptSensitivity'), relay.get('ignoreBackchannel')), ('true', 'none', 'medium', 'true'))
            cfg['interruptSensitivity'] = 'low'
            relay = fromstring(post('CA' + '2' * 32).text).find('Connect/ConversationRelay')
            self.assertEqual(relay.get('interruptSensitivity'), 'low')
        app.sessions.clear()

    def test_question_before_a_tool_is_not_asked_again(self):
        fake = FakeClient([FakeStream('Which hospital are you at?', content=[tool_use('start_flow', {'flow_id': 'log_ticket'})]),
                           FakeStream('Which hospital are you at, Jo?')])  # the model asks again anyway
        session = {}
        events = []
        heard = []
        async def on_text(delta): heard.append(delta)
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
            convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-q', session, on_text,
                                           lambda k, d: events.append((k, d)))
            kind, spoken = run(convo.respond('The arm on my screen keeps dropping, please log a ticket.'))
        self.assertEqual(spoken, 'Which hospital are you at?')
        self.assertEqual(''.join(heard), 'Which hospital are you at?')  # the caller hears it once
        self.assertIn(('suppressed', {'text': 'Which hospital are you at, Jo?'}), events)
        self.assertEqual(convo.history[-1]['content'], 'Which hospital are you at?')
        result = json.loads(fake.requests[1]['messages'][-1]['content'][0]['content'])
        self.assertEqual(result['already_asked_caller'], 'Which hospital are you at?')
        self.assertIn('wait for the caller', result['say_next'])

    def test_unheard_read_back_cannot_be_confirmed(self):
        session = {'caller_turns': 3, 'flow': {'id': 'log_ticket', 'values': {}, 'confirmed': False,
                                               'readback': 'Just to confirm…', 'readback_turn': 4, 'result': None}}
        fake = FakeClient([FakeStream('Is that your mobile?', content=[tool_use('lookup_device', {'hospital': 'RAH', 'bed_id': '1', 'device_id': ''})]),
                           FakeStream('Just to confirm, Sam, Royal Adelaide…')])
        async def on_text(_): pass
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()),                 patch.object(app, 'lookup', return_value={'device_found': True}):
            convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-s', session, on_text)
            run(convo.respond('0400 123 456'))
        self.assertIsNone(session['flow']['readback'])  # muted read-back is discarded, so a yes can't confirm it

    def test_no_guard_when_no_question_was_asked(self):
        fake = FakeClient([FakeStream('Please leave the arm as it is.', content=[tool_use('start_flow', {'flow_id': 'log_ticket'})]),
                           FakeStream('Which hospital are you at?')])
        async def on_text(_): pass
        with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=fake), patch('sys.stderr', io.StringIO()):
            convo = app.build_conversation({**CLAUDE_CFG, 'tickets': 'dry_run'}, 'CA-r', {}, on_text)
            kind, spoken = run(convo.respond('The arm keeps dropping.'))
        self.assertEqual(spoken, 'Please leave the arm as it is. Which hospital are you at?')
        self.assertNotIn('already_asked_caller', json.loads(fake.requests[1]['messages'][-1]['content'][0]['content']))


class TicketFlowConversationTests(unittest.TestCase):
    """A four-turn call through the real engine with a fake model, including a too-early confirm."""

    def test_full_ticket_flow(self):
        def use(name, args, n):
            return FakeStream(content=[tool_use(name, args, f'toolu_{n}')])
        record = lambda pairs: {'values': [{'field': k, 'value': v} for k, v in pairs]}
        fake = FakeClient([
            use('start_flow', {'flow_id': 'log_ticket'}, 1),
            use('flow_record', record([('issue_summary', 'Arm keeps dropping'), ('steps_tried', '')]), 2),
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
        self.assertFalse(early['confirmed']); self.assertIn('wait for the caller', early['reason'])
        self.assertTrue(final['confirmed']); self.assertTrue(final['result']['ticket_logged'])
        self.assertEqual(final['result']['args']['callback_number'], '+61400123456')
        self.assertEqual(session['ticket']['status'], 'logged'); self.assertIsNone(session['flow'])
        # The step note is sent as a system message while the flow is active, but never stored in history.
        notes = [m for r in fake.requests for m in r['messages'] if m['role'] == 'system']
        self.assertTrue(any('ACTIVE FLOW' in m['content'] for m in notes))
        self.assertTrue(any('Read-back to say' in m['content'] for m in notes))
        self.assertFalse(any(m['role'] == 'system' for m in convo.history))
        self.assertEqual(session['caller_turns'], 4)
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
