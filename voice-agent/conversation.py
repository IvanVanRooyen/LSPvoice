"""One call's conversation: history, the turn loop for Claude or OpenAI, interruption repair.

Shared by the phone relay (app.py) and the test harness (harness.py), so tests exercise
exactly the code that answers real calls. Twilio, speech and session bookkeeping stay outside.

Events reported through on_event(kind, data), used for transcripts and test traces:
  caller {text} · agent {text} · tool {name, args, result} · interrupted {heard}
  finish {reason} · turn {first_word_ms, total_ms, tools}
"""
import json
import re
import time

from openai import AsyncOpenAI

import claude_reply
import tools

_MARKER = re.compile(r'[ \t]*(?:[-•]|\d{1,2}[.)])[ \t]')        # a list marker at the start of a line
_MAYBE_MARKER = re.compile(r'[ \t]*(?:[-•]|\d{1,2}[.)]?)?')      # could still turn into one


class SpokenText:
    """Removes written formatting from streamed reply text before it is spoken.

    Drops markdown symbols (* # `) anywhere and list markers ("- ", "• ", "1. ") at the start
    of a line. Works token by token: a possible marker at a line start is held back only until
    it is clearly one or not. Ordinary text ("3 days", "$27.50", "1.5 hours") passes unchanged.
    """
    def __init__(self):
        self.pending = ''
        self.line_start = True

    def feed(self, text):
        out = []
        for ch in text:
            if ch in '*#`':
                continue
            if ch == '\n':
                out.append(self.pending)
                self.pending = ''
                self.line_start = True
                out.append(ch)
                continue
            if not self.line_start:
                out.append(ch)
                continue
            self.pending += ch
            if _MARKER.fullmatch(self.pending):
                self.pending, self.line_start = '', False
            elif not _MAYBE_MARKER.fullmatch(self.pending):
                out.append(self.pending.lstrip(' \t'))  # spacing between replies is handled by Conversation
                self.pending, self.line_start = '', False
        return ''.join(out)

    def flush(self):
        rest, self.pending, self.line_start = self.pending, '', True
        return rest


class Conversation:
    def __init__(self, cfg, system, specs, run_tool, on_text, on_event=None, on_usage=None, session=None, note=None):
        """system and specs are callables, rebuilt each turn so knowledge checks run every time.

        session (the call's state dict) gets a caller-turn counter used by the flow engine;
        note() returns the active flow's step note, appended to each request while a flow runs.
        """
        self.cfg = cfg
        self.system = system
        self.specs = specs
        self.run_tool = run_tool
        self.on_text = on_text
        self.on_event = on_event
        self.on_usage = on_usage
        self.session = session if session is not None else {}
        self.note = note
        self.history = []
        self.partial = ''
        self.last_assistant = None
        self._timing = None

    def _emit(self, kind, **data):
        if self.on_event:
            self.on_event(kind, data)

    async def _speak(self, delta):
        await self._say(self._spoken.feed(delta))

    async def _say(self, delta):
        if not delta:
            return
        if self._question_pending:
            # A question was asked before a tool ran: the caller hears it once, then we wait.
            self._suppressed += delta
            return
        if self._after_tool and self.partial and not self.partial[-1].isspace() and not delta[0].isspace():
            delta = ' ' + delta  # text spoken before and after a tool call: keep a word gap
        self._after_tool = False
        if self._timing['first'] is None:
            self._timing['first'] = time.monotonic() - self._timing['started']
        self.partial += delta
        await self.on_text(delta)

    async def _tool(self, name, args):
        tail = self._spoken.flush()
        if tail:
            await self._say(tail)
        self._after_tool = True
        self._timing['tools'].append(name + (':' + args['guide_id'] if name == 'get_guide' else ''))
        result = await self.run_tool(name, args)
        spoken = self.partial.rstrip()
        if isinstance(result, dict) and spoken.endswith('?'):
            # The reply already ended with a question before this tool ran (the model spoke, then
            # used a tool). The tool's next-step instruction would make it ask again, so tell it,
            # and drop anything more it says in this reply (enforced in _say).
            question = re.split(r'(?<=[.!?])\s+', spoken)[-1]
            result = {**result, 'already_asked_caller': question,
                      'say_next': 'Nothing more in this reply: wait for the caller to answer that question.'}
            self._question_pending = True
        self._emit('tool', name=name, args=args, result=result)
        return result

    async def respond(self, text):
        """Run one caller turn. Returns ('text', spoken) or ('finish', reason); raises on failure."""
        self.history.append({'role': 'user', 'content': text[:4000]})
        self.session['caller_turns'] = self.session.get('caller_turns', 0) + 1
        self.last_assistant = None
        self.partial = ''
        self._spoken = SpokenText()
        self._after_tool = False
        self._question_pending = False
        self._suppressed = ''
        self._timing = {'started': time.monotonic(), 'first': None, 'tools': []}
        self._emit('caller', text=text)
        system, specs = self.system(), self.specs()
        exchange = []  # this turn's tool calls/results (Claude path), kept so later turns can see them
        if self.cfg.get('provider') == 'anthropic':
            kind, value = await claude_reply.reply(self.cfg, system, self.history, specs, self._tool,
                                                   self._speak, on_usage=self.on_usage, exchange=exchange,
                                                   note=self.note)
        else:
            kind, value = await self._openai(system, specs)
        tail = self._spoken.flush()
        if tail:
            await self._say(tail)
        if self._suppressed.strip():
            self._emit('suppressed', text=self._suppressed.strip())
            flow = self.session.get('flow')
            if flow and flow.get('readback') and flow.get('readback_turn') == self.session.get('caller_turns'):
                flow['readback'] = None  # the caller never heard it: a new read-back must be said first
        first = self._timing['first']
        self._emit('turn', first_word_ms=None if first is None else round(first * 1000),
                   total_ms=round((time.monotonic() - self._timing['started']) * 1000), tools=list(self._timing['tools']))
        if self.partial:
            self._emit('agent', text=self.partial)
        if kind == 'finish':
            self._emit('finish', reason=value)
            return kind, value
        if not self.partial:
            raise ValueError('No complete answer returned')
        self.history.extend(exchange)
        self.history.append({'role': 'assistant', 'content': self.partial})
        self.last_assistant = len(self.history) - 1
        return 'text', self.partial

    def interrupted(self, heard):
        """The caller spoke over the reply: keep only the words they actually heard."""
        if self.last_assistant is not None and self.last_assistant == len(self.history) - 1:
            self.history[self.last_assistant]['content'] = heard
        elif heard:
            self.history.append({'role': 'assistant', 'content': heard})
            self.last_assistant = len(self.history) - 1
        self.partial = ''
        self._emit('interrupted', heard=heard)

    def discard_partial(self):
        self.partial = ''

    async def _openai(self, system, specs):
        cfg = self.cfg
        inputs = list(self.history)
        last_note = None
        async with AsyncOpenAI(api_key=cfg['openai_api_key'], max_retries=0, timeout=25) as client:
            for round_number in range(4):
                current_note = self.note() if self.note and round_number == 0 else None
                if current_note and current_note != last_note:
                    inputs.append({'role': 'developer', 'content': current_note})
                    last_note = current_note
                options = {'reasoning': {'effort': 'none'}} if cfg['model'] == 'gpt-6.1-terra' else {}
                async with client.responses.stream(model=cfg['model'], instructions=system, input=inputs,
                                                   tools=tools.openai_tools(specs), store=False,
                                                   max_output_tokens=900, **options) as stream:
                    async for event in stream:
                        if event.type == 'response.output_text.delta':
                            await self._speak(event.delta)
                    result = await stream.get_final_response()
                calls = [x for x in result.output if x.type == 'function_call']
                if not calls:
                    return 'text', self.partial
                inputs.extend(result.output)
                for call in calls:
                    args = json.loads(call.arguments)
                    if not tools.valid_input(specs, call.name, args):
                        raise ValueError('Invalid tool input')
                    if call.name == 'finish_call':
                        return 'finish', args['reason']
                    answer = await self._tool(call.name, args)
                    inputs.append({'type': 'function_call_output', 'call_id': call.call_id, 'output': json.dumps(answer)})
            raise ValueError('Pilot tool limit')
