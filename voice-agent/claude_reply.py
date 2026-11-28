"""Claude (Anthropic API) reply loop for the pilot, used when config provider is 'anthropic'.

Same contract as the OpenAI loop in app.py: stream spoken text through on_text, run tools
through run_tool, and report finish_call. Completed turns are kept by app.py as plain
text, so no thinking block is ever replayed outside the turn that produced it; within a
turn the assistant content (thinking included) is appended unchanged.

Model default Claude Sonnet 5.5 at low effort with thinking off ("between_tools", Sonnet 5.5
only, effort high or below) for the lowest latency. Config "thinking": "adaptive" lets the
model think before replying instead.
"""
import json

import anthropic

import tools

DEFAULT_MODEL = 'claude-sonnet-5-5'
DEFAULT_EFFORT = 'low'  # short or no thinking keeps time-to-first-word acceptable on a phone call
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
ROUNDS = 4  # same bound as the OpenAI loop


def _between_tools_allowed(model, effort):
    return model == 'claude-sonnet-5-5' and effort not in ('xhigh', 'max')


def request_options(cfg):
    """Thinking defaults to off (between_tools) where the model allows it: in testing on
    26 Nov it cut the slowest replies by about a second with similar quality. Set
    thinking=adaptive to let the model think before replying."""
    model = cfg.get('anthropic_model') or DEFAULT_MODEL
    effort = cfg.get('effort') or DEFAULT_EFFORT
    thinking = cfg.get('thinking') or ('between_tools' if _between_tools_allowed(model, effort) else 'adaptive')
    options = {'model': model, 'output_config': {'effort': effort}}
    if thinking == 'between_tools':
        if not _between_tools_allowed(model, effort):
            raise ValueError('thinking between_tools needs claude-sonnet-5-5 at effort high or below')
        options['thinking'] = {'type': 'between_tools'}
    return options


def request_messages(history, note=None):
    """The messages for a request: history (unchanged) plus this turn's note, in an order the API accepts.

    A mid-conversation system message must be followed by an assistant message or end the array.
    History stores a completed turn as user -> system -> assistant. When the caller talked over a
    reply before hearing any of it, that reply is stored empty and an empty assistant turn can't be
    sent, which would leave system -> user (a 400 for the rest of the call). So a system message is
    placed just before the next assistant message, or at the end: after an unheard reply it follows
    the caller's next words instead. No note is ever dropped, nothing the caller didn't hear is
    invented, and adjacent system messages are merged into one.
    """
    out, pending = [], []
    for m in list(history) + ([{'role': 'system', 'content': note}] if note else []):
        if not m['content']:
            continue  # an interruption before any word was heard: the API rejects empty turns
        if m['role'] == 'system':
            pending.append(m['content'])
            continue
        if m['role'] == 'assistant' and pending:
            out.append({'role': 'system', 'content': '\n\n'.join(pending)})
            pending = []
        out.append({'role': m['role'], 'content': m['content']})
    if pending:
        out.append({'role': 'system', 'content': '\n\n'.join(pending)})
    return out


async def reply(cfg, system, history, tool_specs, run_tool, on_text, on_usage=None, exchange=None, note=None,
                on_round_end=None):
    """Return ('text', spoken) or ('finish', reason).

    history holds earlier turns: text turns, the tool calls and results of earlier turns, and the
    system messages (flow notes, call details) sent in earlier turns.
    exchange (a list) receives this turn's system note, tool calls and results, so the caller can
    keep them in history: without them the model forgets it already looked a bed up or opened a
    guide, and sees its own remarks about notes that are no longer there. History is append-only.
    Thinking blocks are not kept across turns (allowed by the API; avoids history-edit checks).
    on_usage(model, usage) is called after every API response (used by the test harness for cost).
    note (text or None) is sent once, at the start of the turn, as a mid-conversation system
    message after the caller's words: never after a tool result, where the model can mistake
    instructions for a prompt injection.
    on_round_end(tool_used) is awaited after each response, before any tool runs, so the caller can
    decide whether text held back from that round is spoken (an answer) or dropped (narration).
    """
    messages = request_messages(history, note)
    if note and exchange is not None:
        exchange.append({'role': 'system', 'content': note})
    definitions = tools.anthropic_tools(tool_specs)
    options = request_options(cfg)
    spoken = ''
    json_retries = 0
    async with anthropic.AsyncAnthropic(api_key=cfg['anthropic_api_key'], max_retries=0, timeout=25) as client:
        rounds = 0
        while rounds < ROUNDS:
            said_this_round = ''
            try:
                async with client.beta.messages.stream(
                        max_tokens=16000, system=system, messages=messages, tools=definitions,
                        cache_control={'type': 'ephemeral'},  # system + knowledge are re-sent every turn
                        betas=[FALLBACK_BETA], fallbacks='default', **options) as stream:
                    async for event in stream:
                        if event.type == 'text':
                            said_this_round += event.text
                            spoken += event.text
                            await on_text(event.text)
                    message = await stream.get_final_message()
                if on_usage and getattr(message, 'usage', None) is not None:
                    on_usage(getattr(message, 'model', None) or options['model'], message.usage)
            except ValueError:
                # Tool-input JSON the SDK could not parse. Re-issue only if nothing was said
                # yet, so the caller never hears the same words twice.
                json_retries += 1
                if said_this_round or json_retries > 2:
                    raise
                continue
            json_retries = 0
            rounds += 1
            if message.stop_reason == 'refusal':
                category = getattr(getattr(message, 'stop_details', None), 'category', None)
                raise RuntimeError(f'Claude declined the turn (category {category})')
            uses = [block for block in message.content if block.type == 'tool_use']
            if on_round_end:  # words said together with finish_call are the goodbye, not narration
                await on_round_end(any(block.name != 'finish_call' for block in uses))
            if not uses:
                return 'text', spoken
            if message.stop_reason == 'max_tokens':
                raise RuntimeError('Tool input truncated at max_tokens')
            messages.append({'role': 'assistant', 'content': message.content})
            results = []
            for block in uses:
                if not tools.valid_input(tool_specs, block.name, block.input):
                    results.append({'type': 'tool_result', 'tool_use_id': block.id, 'is_error': True,
                                    'content': json.dumps({'INVALID_JSON': json.dumps(block.input)})})
                    continue
                if block.name == 'finish_call':
                    return 'finish', block.input['reason']
                results.append({'type': 'tool_result', 'tool_use_id': block.id,
                                'content': json.dumps(await run_tool(block.name, block.input))})
            messages.append({'role': 'user', 'content': results})
            if exchange is not None:
                exchange.append({'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': b.id, 'name': b.name, 'input': b.input} for b in uses]})
                exchange.append({'role': 'user', 'content': results})
    raise ValueError('Pilot tool limit')
