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


async def reply(cfg, system, history, tool_specs, run_tool, on_text, on_usage=None, exchange=None):
    """Return ('text', spoken) or ('finish', reason).

    history holds earlier turns: text turns, plus the tool calls and results of earlier turns.
    exchange (a list) receives this turn's tool calls and results, so the caller can keep them
    in history: without them the model forgets it already looked a bed up or opened a guide.
    Thinking blocks are not kept across turns (allowed by the API; avoids history-edit checks).
    on_usage(model, usage) is called after every API response (used by the test harness for cost).
    """
    # An interruption before any word was heard leaves an empty assistant turn; the API rejects those.
    messages = [{'role': m['role'], 'content': m['content']} for m in history if m['content']]
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
