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
import listening
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


# Holding acknowledgements (fixed wording, spoken by code): only when the model's first response in a
# turn is a silent call to a tool that records or looks up what the caller just said. Other tool turns
# (opening a guide, starting a flow) and answers without tools get none: a filler there is just padding.
ACKS = ['Got it.', 'Thanks.', 'Okay.']
ACK_TOOLS = {'flow_record', 'lookup_device'}
# After a holding acknowledgement, a reply opening with a bare one of its own ("Thanks. What's your bed
# number?") would be heard as "Thanks. Thanks. …": that opening sentence is left out.
_BARE_ACK = re.compile(r"\s*(?:thanks|thank you|got it|okay|ok|great|alright|all right|sure)\s*[.!]\s*", re.IGNORECASE)
FLOW_ENDED_NOTE = "Flow status from LSPcare's own system: the flow has ended; earlier flow status notes no longer apply."


def _has_read_back(readback, text, run=5):
    """True when text contains part of the read-back: some run of its words in order."""
    said, back = listening.words(text), listening.words(readback)
    runs = {tuple(back[i:i + run]) for i in range(max(1, len(back) - run + 1))}
    return any(tuple(said[i:i + run]) in runs for i in range(max(1, len(said) - run + 1)))


class Conversation:
    def __init__(self, cfg, system, specs, run_tool, on_text, on_event=None, on_usage=None, session=None, note=None,
                 context=None):
        """system and specs are callables, rebuilt each turn so knowledge checks run every time.

        session (the call's state dict) gets a caller-turn counter used by the flow engine;
        note() returns the active flow's step note. When it changes, it is sent at the start of the
        next turn as a system message and kept in history (append-only: earlier notes are never removed).
        context: call details for the model (for example the call reference), sent once with the
        first turn as a system message and kept.
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
        self.context = context
        self._context_stored = False
        self._last_note = None
        self._ack_turns = 0
        self.history = []
        self.partial = ''
        self.last_assistant = None
        self._timing = None

    def _emit(self, kind, **data):
        if self.on_event:
            self.on_event(kind, data)

    def _turn_system(self):
        """System text for this turn: call details (first turn) and the flow note when it changed."""
        parts = [self.context] if self.context and not self._context_stored else []
        note = self.note() if self.note else None
        if note and note != self._last_note:
            parts.append(note)
        elif not note and self._last_note and self._last_note != FLOW_ENDED_NOTE:
            note = FLOW_ENDED_NOTE  # so the stored notes of a finished flow are not followed
            parts.append(note)
        else:
            note = self._last_note
        return ('\n\n'.join(parts) or None), note

    # Spoken text is held until the model's round ends. If the round ends in a tool call the text was
    # the model describing its own work ("The value sounds like a bed ID, so I'll check it first") and
    # is dropped; otherwise it is the answer and is spoken. Narration can't be recognised by its
    # wording, so nothing from a round is spoken before its outcome is known. Tool turns get a holding
    # line instead; replies without tools wait for the whole round (about 0.2-0.4 s on the server).
    async def _speak(self, delta):
        self._held += self._spoken.feed(delta)

    async def _round_end(self, tool_names):
        """After each model response, before its tools run. tool_names: the tools it called."""
        tool_used = any(n != 'finish_call' for n in tool_names)  # words said with finish_call are the goodbye
        held, self._held = self._held + self._spoken.flush(), ''
        if tool_used:
            if held.strip():
                self._emit('suppressed', text=held.strip(), reason='narration before a tool call')
                self._dropped += ' ' + held
        elif held.strip():
            if self._acked:  # the caller has already heard an acknowledgement this turn
                bare = _BARE_ACK.match(held)
                if bare:
                    held = held[bare.end():]
            if held.strip():
                await self._say(held)
        if tool_used and self._rounds == 0 and not self.partial and set(tool_names) <= ACK_TOOLS:
            # The caller has just given a detail and the model called a tool: the answer needs a
            # second model response (~1.5 s), so acknowledge now rather than leave a silence.
            await self.hold(self._next_ack())
            self._acked = True
        self._rounds += 1

    def _next_ack(self):
        """Cycles through the acknowledgements, so the same one is never said twice in a row."""
        ack = ACKS[self._ack_turns % len(ACKS)]
        self._ack_turns += 1
        return ack

    async def hold(self, text):
        """Speak a fixed holding line (code, not the model) while the reply waits on something slow.

        Part of the turn's spoken text (so it is in history and interruptions keep only what was
        heard), and never held back or dropped."""
        if self.partial and not self.partial[-1].isspace():
            text = ' ' + text
        await self._out(text)
        self._after_tool = True  # keep a word gap before whatever the model says next

    async def _say(self, delta):
        if not delta:
            return
        if self._after_tool and self.partial and not self.partial[-1].isspace() and not delta[0].isspace():
            delta = ' ' + delta  # text spoken before and after a tool call: keep a word gap
        await self._out(delta)

    async def _out(self, delta):
        self._after_tool = False
        if self._timing['first'] is None:
            self._timing['first'] = time.monotonic() - self._timing['started']
        self.partial += delta
        await self.on_text(delta)

    async def _tool(self, name, args):
        self._after_tool = True
        self._timing['tools'].append(name + (':' + args['guide_id'] if name == 'get_guide' else ''))
        result = await self.run_tool(name, args)
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
        self._held = self._dropped = ''
        self._rounds, self._acked = 0, False
        self._timing = {'started': time.monotonic(), 'first': None, 'tools': []}
        self._emit('caller', text=text)
        system, specs = self.system(), self.specs()
        turn_system, note = self._turn_system()
        exchange = []  # this turn's system note, tool calls and results, kept so later turns can see them
        if self.cfg.get('provider') == 'anthropic':
            kind, value = await claude_reply.reply(self.cfg, system, self.history, specs, self._tool,
                                                   self._speak, on_usage=self.on_usage, exchange=exchange,
                                                   note=turn_system, on_round_end=self._round_end)
        else:
            kind, value = await self._openai(system, specs, turn_system)
            if turn_system:
                exchange.append({'role': 'system', 'content': turn_system})
        await self._round_end([])  # anything still held (OpenAI path) is the answer
        flow = self.session.get('flow')
        made_now = flow and flow.get('readback') and flow.get('readback_turn') == self.session.get('caller_turns')
        if made_now and _has_read_back(flow['readback'], self._dropped) and not _has_read_back(flow['readback'], self.partial):
            flow['readback'] = None  # said before a tool call and dropped: unheard, so a yes can't confirm it
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
        # Stored only for a completed turn: a system message must be followed by an assistant turn.
        self.history.extend(exchange)
        self._context_stored = True
        self._last_note = note
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

    def reprompt(self, text):
        """The agent asks again after a silence (said by code): kept as its own assistant message, so the
        model knows it was said and an interruption trims only this, not the reply before it."""
        if self.history and self.history[-1]['role'] == 'user':
            return False  # a caller turn is under way: nothing to re-ask
        if self.history:  # after the greeting there is no history yet (the greeting isn't in it either)
            self.history.append({'role': 'assistant', 'content': text})
            self.last_assistant = len(self.history) - 1
        self._emit('reprompt', text=text)
        return True

    def discard_partial(self):
        self.partial = ''

    async def _openai(self, system, specs, turn_system=None):
        cfg = self.cfg
        inputs = [{'role': 'developer' if m['role'] == 'system' else m['role'], 'content': m['content']}
                  for m in self.history if isinstance(m.get('content'), str)]
        if turn_system:
            inputs.append({'role': 'developer', 'content': turn_system})
        async with AsyncOpenAI(api_key=cfg['openai_api_key'], max_retries=0, timeout=25) as client:
            for round_number in range(4):
                options = {'reasoning': {'effort': 'none'}} if cfg['model'] == 'gpt-6.1-terra' else {}
                async with client.responses.stream(model=cfg['model'], instructions=system, input=inputs,
                                                   tools=tools.openai_tools(specs), store=False,
                                                   max_output_tokens=900, **options) as stream:
                    async for event in stream:
                        if event.type == 'response.output_text.delta':
                            await self._speak(event.delta)
                    result = await stream.get_final_response()
                calls = [x for x in result.output if x.type == 'function_call']
                await self._round_end([c.name for c in calls])
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
