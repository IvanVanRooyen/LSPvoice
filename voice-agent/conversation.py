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


_SENTENCE_END = '.!?'
# A turn that opens like this is the model describing its next step, not answering: hold it like
# later text (dropped if a tool call follows, spoken if not).
_NARRATION_START = re.compile(
    r"(?:(?:ok(?:ay)?|sure|alright|right|great|thanks|thank you)[,.!]?\s+)?"
    r"(?:let me|let's|i'll|i will|i'm going to|i am going to|i'm just|i'm now|now i'll|one moment|just a moment|"
    r"give me a moment|bear with me)\b", re.IGNORECASE)
_DECIDE_AFTER = 25  # characters of the opening looked at before the first word is spoken (~50 ms)
# Holding acknowledgements (fixed wording, spoken by code): only when the model's first response in a
# turn is a silent call to a tool that records or looks up what the caller just said. Other tool turns
# (opening a guide, starting a flow) and answers without tools get none: a filler there is just padding.
ACKS = ['Got it.', 'Thanks.', 'Okay.']
ACK_TOOLS = {'flow_record', 'lookup_device'}
# After a holding acknowledgement, a reply opening with a bare one of its own ("Thanks. What's your bed
# number?") would be heard as "Thanks. Thanks. …": that opening sentence is left out.
_BARE_ACK = re.compile(r"\s*(?:thanks|thank you|got it|okay|ok|great|alright|all right|sure)\s*[.!]\s*", re.IGNORECASE)
FLOW_ENDED_NOTE = "Flow status from LSPcare's own system: the flow has ended; earlier flow status notes no longer apply."


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

    # Spoken text passes a gate: the first sentence of a turn is spoken as it streams; after it,
    # text is held until the model's round ends. If the round ends in a tool call the held text was
    # narration ("I'll record that now") and is dropped; otherwise it is the answer and is spoken.
    # This keeps the first word as quick as before while narration between tool calls is never heard.
    def _gate(self, text):
        if self._holding:
            self._held += text
            return ''
        if self._deciding:  # the turn's opening words decide whether its first sentence may stream
            self._opening += text
            if len(self._opening) < _DECIDE_AFTER and not any(c in _SENTENCE_END for c in self._opening):
                return ''
            if self._acked and _BARE_ACK.fullmatch(self._opening):
                return ''  # "Thanks." so far: see whether more follows before deciding
            if self._acked and _BARE_ACK.match(self._opening) and not self._opening[_BARE_ACK.match(self._opening).end():].strip(' '):
                return ''
            text = self._decide()
            if self._holding:
                return ''
        out = []
        for i, ch in enumerate(text):
            if ch.isspace() and self._last_char in _SENTENCE_END:
                self._holding = True
                self._held += text[i:]
                break
            out.append(ch)
            self._last_char = ch
        return ''.join(out)

    def _decide(self):
        """Called once per turn with the opening words: narration is held, anything else streams."""
        text, self._opening, self._deciding = self._opening, '', False
        if self._acked:  # the caller has already heard an acknowledgement this turn
            bare = _BARE_ACK.match(text)
            if bare:
                text = text[bare.end():]
        if _NARRATION_START.match(text.lstrip()):
            self._holding = True
            self._held += text
            return ''
        return text

    async def _speak(self, delta):
        await self._say(self._gate(self._spoken.feed(delta)))

    async def _round_end(self, tool_names):
        """After each model response, before its tools run. tool_names: the tools it called."""
        tool_used = any(n != 'finish_call' for n in tool_names)
        tail = self._spoken.flush()
        if tail:
            await self._say(self._gate(tail))
        if self._deciding and self._opening:  # a short round: decide on what there is
            opening = self._decide()
            if opening:
                await self._say(self._gate(opening))
        held, self._held = self._held, ''
        if tool_used:
            if held.strip():
                self._emit('suppressed', text=held.strip(), reason='narration before a tool call')
        elif held:
            await self._say(held)
        if tool_used and self._rounds == 0 and not self.partial and set(tool_names) <= ACK_TOOLS:
            # The caller has just given a detail and the model called a tool without a word: the answer
            # needs a second model response (~1.5 s), so acknowledge now rather than leave a silence.
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
        heard), never held back, dropped or muted, and not the turn's question."""
        if self.partial and not self.partial[-1].isspace():
            text = ' ' + text
        await self._out(text)
        self._after_tool = True  # keep a word gap before whatever the model says next

    async def _say(self, delta):
        if not delta:
            return
        if self._question_pending:
            # A question was asked before a tool ran: the caller hears it once, then we wait.
            self._suppressed += delta
            return
        if self._after_tool and self.partial and not self.partial[-1].isspace() and not delta[0].isspace():
            delta = ' ' + delta  # text spoken before and after a tool call: keep a word gap
        self._model_said += delta
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
        spoken = self._model_said.rstrip()  # the model's own words: a holding line is never its question
        if isinstance(result, dict) and spoken.endswith('?'):
            # The reply already ended with a question before this tool ran (the model spoke, then
            # used a tool). Tell it, as data, and drop anything more it says in this reply (enforced
            # in _say). No instruction goes in a tool result: those can look like a prompt injection.
            question = re.split(r'(?<=[.!?])\s+', spoken)[-1]
            result = {**result, 'already_asked_caller': question}
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
        self._holding, self._held, self._last_char = False, '', ''
        self._deciding, self._opening = True, ''
        self._rounds, self._model_said, self._acked = 0, '', False
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
        if self._suppressed.strip():
            self._emit('suppressed', text=self._suppressed.strip(), reason='question already asked')
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
