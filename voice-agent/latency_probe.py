"""Offline time-to-first-word for typical turns, with a stand-in model. No network, no API cost.

The stand-in takes 1.4 s to its first token, then streams one word every 16 ms (about 80 tokens a
second); a ticket write takes 5 s. Numbers are medians of three runs. Use it to check that a
change to the speaking path (conversation.py, claude_reply.py) does not slow the caller's first
word, and compare with an earlier commit:

    python latency_probe.py                                  # this checkout
    git worktree add /tmp/before <commit>                    # then, from /tmp/before/voice-agent:
    PYTHONPATH=. python /path/to/this/latency_probe.py       # the same probe against the old code
    git worktree remove /tmp/before

Compare runs on the same machine: timer resolution (about 15 ms on Windows) moves the streaming
cases by a few tenths of a second. Real calls add speech-to-text, the network and text-to-speech;
see `turn` events in transcripts.
"""
import asyncio
import io
import statistics
import time
from types import SimpleNamespace
from unittest.mock import patch

import app
import claude_reply
import tickets

MODEL_SECONDS, TICKET_SECONDS, WORD_SECONDS = 1.4, 5.0, 0.016


class Stream:
    def __init__(self, text='', tools=()):
        self.text, self.tools = text, list(tools)
    async def __aenter__(self):
        await asyncio.sleep(MODEL_SECONDS)
        return self
    async def __aexit__(self, *a): pass
    def __aiter__(self):
        async def gen():
            for i, word in enumerate(self.text.split(' ') if self.text else []):
                if i:
                    await asyncio.sleep(WORD_SECONDS)
                yield SimpleNamespace(type='text', text=(' ' if i else '') + word)
        return gen()
    async def get_final_message(self):
        content = ([SimpleNamespace(type='text', text=self.text)] if self.text else []) + [
            SimpleNamespace(type='tool_use', id=f'toolu_{i}', name=n, input=a) for i, (n, a) in enumerate(self.tools)]
        return SimpleNamespace(stop_reason='tool_use' if self.tools else 'end_turn', content=content, usage=None,
                               model='claude-sonnet-5-5', stop_details=None)


class Client:
    def __init__(self, streams):
        self.streams = list(streams); self.beta = self; self.messages = self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    def stream(self, **kw): return self.streams.pop(0)


def flow(values, readback_turn=None):
    return {'id': 'log_ticket', 'values': values, 'confirmed': False, 'readback': 'Just to confirm…' if readback_turn else None,
            'readback_turn': readback_turn, 'result': None}


def slow_write(_record):
    time.sleep(TICKET_SECONDS)


async def first_word(streams, line, session):
    timing = {}
    async def on_text(_): pass
    def on_event(kind, data):
        if kind == 'turn':
            timing.update(data)
    cfg = {'provider': 'anthropic', 'anthropic_api_key': 'x', 'anthropic_model': 'claude-sonnet-5-5', 'effort': 'low',
           'tickets': 'dry_run', 'auth_token': 'x', 'account_sid': 'AC' + '0' * 32, 'public_base': 'https://x.invalid'}
    with patch.object(claude_reply.anthropic, 'AsyncAnthropic', return_value=Client(streams)), \
            patch.object(app, 'lookup', return_value={'device_found': True}), \
            patch.object(tickets, '_write_dry_run', slow_write), patch('sys.stderr', io.StringIO()):
        convo = app.build_conversation(cfg, 'CA-probe', session, on_text, on_event)
        await convo.respond(line)
    return timing['first_word_ms'], timing['total_ms']


async def main():
    problem = {'issue_summary': 'Black screen on the LSPC15mf', 'problem_spoken': 'your screen is black'}
    full = {**problem, 'hospital': 'Royal Adelaide', 'bed_id': '7G13101', 'caller_name': 'Sam', 'callback_number': '+61400123456'}
    answer = ("Press the orange button on the right side of the screen once. Wait about ten seconds for the picture "
              "to come back. Is the screen showing anything now?")
    cases = [
        ('no tool, one sentence (10 words)', lambda: [Stream("Sure, I can help with that. What's the problem?")], 'Hi.', lambda: {}),
        ('no tool, three sentences (27 words)', lambda: [Stream(answer)], 'My screen is black.', lambda: {}),
        ('narration + lookup_device (ref 6081)',
         lambda: [Stream("The value sounds like a bed ID, so I'll check it first.",
                         tools=[('lookup_device', {'hospital': 'RAH', 'bed_id': '7G13101', 'device_id': ''})]),
                  Stream("That's the LSPC15mf in bed 7G13101. What's happening with it?")],
         '7G13101.', lambda: {}),
        ('flow_record (silent first response)',
         lambda: [Stream(tools=[('flow_record', {'values': [{'field': 'hospital', 'value': 'Royal Adelaide'}]})]),
                  Stream("What's your bed number?")], 'Royal Adelaide.', lambda: {'caller_turns': 1, 'flow': flow(dict(problem))}),
        ('flow_confirm -> ticket (5 s write)',
         lambda: [Stream(tools=[('flow_confirm', {'answer': 'yes'})]), Stream('Your ticket number is 1 0 4 2.')],
         "Yes, that's right.", lambda: {'caller_turns': 3, 'flow': flow(dict(full), readback_turn=3)}),
    ]
    for name, streams, line, session in cases:
        runs = [await first_word(streams(), line, session()) for _ in range(3)]
        first = statistics.median(r[0] for r in runs); total = statistics.median(r[1] for r in runs)
        print(f'{name:40s} first word {first / 1000:5.2f} s   reply done {total / 1000:5.2f} s')


asyncio.run(main())
