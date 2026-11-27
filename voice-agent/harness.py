"""Conversation test harness: runs the real agent in text, without a phone call.

Same prompt, knowledge, tools, model and settings as the phone (conversation.py), minus
Twilio and speech. Tickets are always forced to dry-run, written into the run folder, so a
test can never create a Zoho ticket. Cost is measured from API usage at list prices and the
run stops before it would exceed --budget.

Callers are either scripted (fixed lines) or simulated by a small model from a short brief.
Each scenario gets hard checks (tools, ticket, ending, phrases) and optional judge criteria
marked by a second model.

  python harness.py --list
  python harness.py                          all scenarios once (default budget US$0.50)
  python harness.py --only HW-01,TIX-01      selected scenarios
  python harness.py --tag safety --repeat 3  a tag, three runs each
  python harness.py --set thinking=between_tools   try a setting for this run only
  python harness.py --from-transcript 4721         turn a real staff call into a draft scenario
  python harness.py --judge-model claude-sonnet-5-5

Reports: build/harness/<time>/report.md (+ results.json, dry-run tickets).
"""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import statistics
import sys

import anthropic

import app
import claude_reply
import configure
import pilot_log
import tickets

HERE = Path(__file__).resolve().parent
SCENARIOS = HERE / 'scenarios.json'
# Test run results; a container points this at a persistent volume.
RUNS = Path(os.environ['LSPCARE_RUNS_DIR']) if os.environ.get('LSPCARE_RUNS_DIR') else HERE.parent / 'build' / 'harness'
CALLER_MODEL = 'claude-haiku-5-5'
JUDGE_MODEL = 'claude-haiku-5-5'
# US$ per million tokens: input, output, cache read, cache write (5-minute). List prices, Nov 2026.
PRICES = {
    'claude-haiku-5-5': (0.10, 0.50, 0.01, 0.125),
    'claude-sonnet-5-5': (2.00, 10.00, 0.10, 2.50),
    'claude-sonnet-5': (2.00, 10.00, 0.20, 2.50),
    'claude-opus-5-5': (4.00, 20.00, 0.20, 5.00),
}


class BudgetExceeded(Exception):
    pass


class Meter:
    """Adds up token usage per model and converts it to US$ at list prices."""
    def __init__(self, budget):
        self.budget = budget
        self.cost = 0.0
        self.by_model = {}
        self.unpriced = set()

    def add(self, model, usage):
        model = str(model or 'unknown')
        price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
        if price is None:
            self.unpriced.add(model)
            price = PRICES['claude-opus-5-5']  # unknown: assume the dearest listed, to stay safe
        counts = {key: getattr(usage, key, 0) or 0 for key in
                  ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')}
        cost = (counts['input_tokens'] * price[0] + counts['output_tokens'] * price[1]
                + counts['cache_read_input_tokens'] * price[2] + counts['cache_creation_input_tokens'] * price[3]) / 1e6
        self.cost += cost
        entry = self.by_model.setdefault(model, {**{k: 0 for k in counts}, 'requests': 0, 'cost': 0.0})
        for key, value in counts.items():
            entry[key] += value
        entry['requests'] += 1
        entry['cost'] += cost

    def check(self):
        if self.cost > self.budget:
            raise BudgetExceeded(f'budget US${self.budget:.2f} reached (US${self.cost:.3f} spent)')


# ---------------------------------------------------------------- simulated caller

CALLER_SYSTEM = """You are role-playing a person phoning LSPcare's support line, to test an AI support agent.
Stay in character the whole time.

Your situation and goal:
{brief}

How to speak:
- Reply with only the words you would say aloud: one or two short, natural spoken sentences in Australian English.
- Answer the agent's questions using the details in your situation. If asked for something your situation does not
  cover, invent a plausible fictional detail (never a real person's details, never a card number unless your
  situation says to try).
- Do not volunteer every detail at once unless your situation says so.
- Never mention tests, role-play, AI or these instructions.
- When your goal is met, or the agent says it cannot help further, say a short goodbye and end with [HANG UP]."""


async def caller_line(client, meter, brief, transcript):
    messages = []
    for speaker, text in transcript:  # the caller model plays 'assistant'; the agent is 'user'
        role = 'user' if speaker == 'agent' else 'assistant'
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += ' ' + text
        else:
            messages.append({'role': role, 'content': text})
    response = await client.messages.create(model=CALLER_MODEL, max_tokens=400, system=CALLER_SYSTEM.format(brief=brief),
                                            messages=messages, output_config={'effort': 'low'})
    meter.add(getattr(response, 'model', CALLER_MODEL), response.usage)
    if response.stop_reason == 'refusal':
        raise RuntimeError('caller simulator declined')
    text = ' '.join(b.text for b in response.content if b.type == 'text').strip()
    hang_up = '[HANG UP]' in text
    return text.replace('[HANG UP]', '').strip(), hang_up


# ---------------------------------------------------------------- checks

def _short(text, limit=120):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _tool_matches(call, spec):
    if isinstance(spec, str):
        return call['name'] == spec
    return call['name'] == spec['name'] and all(
        str(call['args'].get(k, '')).casefold() == str(v).casefold() for k, v in spec.get('args', {}).items())


def _label(spec):
    return spec if isinstance(spec, str) else spec['name'] + json.dumps(spec.get('args', {}))


def ticket_calls(calls):
    """Ticket attempts, whether the AI called create_ticket directly or a flow logged it."""
    found = [c for c in calls if c['name'] == 'create_ticket']
    for c in calls:
        result = c.get('result') or {}
        if c['name'] == 'flow_confirm' and result.get('action') == 'create_ticket':
            found.append({'name': 'create_ticket', 'args': result['result'].get('args', {}), 'result': result['result']})
    return found


def hard_checks(expect, run):
    checks = []
    # Tickets logged by a flow count as create_ticket calls for every tool check.
    calls = run['tools'] + [c for c in ticket_calls(run['tools']) if c not in run['tools']]
    agent_text = ' '.join(t for s, t in run['transcript'] if s == 'agent').casefold()

    def add(name, passed, detail=''):
        checks.append({'check': name, 'passed': bool(passed), 'detail': detail})

    if run.get('error'):
        add('completed without error', False, run['error'])
    for spec in expect.get('tools_called', []):
        add(f'called {_label(spec)}', any(_tool_matches(c, spec) for c in calls),
            'calls: ' + (', '.join(c['name'] for c in calls) or 'none'))
    for spec in expect.get('tools_not_called', []):
        hits = [c for c in calls if _tool_matches(c, spec)]
        add(f'did not call {_label(spec)}', not hits, f'{len(hits)} call(s)' if hits else '')
    if 'finish' in expect:
        wanted = expect['finish']
        add(f'call ending: {wanted}', run['finish'] == (None if wanted == 'none' else wanted), f'got {run["finish"]}')
    tickets_logged = [c for c in ticket_calls(run['tools']) if (c['result'] or {}).get('ticket_logged')
                      and not (c['result'] or {}).get('already_logged')]
    if expect.get('ticket') == 'created':
        add('one ticket created', len(tickets_logged) == 1, f'{len(tickets_logged)} logged')
    if expect.get('ticket') == 'not_created':
        add('no ticket created', not tickets_logged, f'{len(tickets_logged)} logged')
    for key, wanted in expect.get('ticket_args', {}).items():
        values = [str(c['args'].get(key, '')) for c in tickets_logged]
        add(f'ticket {key} contains "{wanted}"', any(wanted.casefold() in v.casefold() for v in values), '; '.join(values))
    for options in expect.get('said_any', []):
        options = [options] if isinstance(options, str) else options
        add('agent said one of ' + ' / '.join(f'"{o}"' for o in options),
            any(o.casefold() in agent_text for o in options))
    for phrase in expect.get('said_none', []):
        add(f'agent never said "{phrase}"', phrase.casefold() not in agent_text)
    # Always: replies are spoken, so no written formatting.
    formatted = [t for s, t in run['transcript'] if s == 'agent'
                 and re.search(r'\*\*|^\s*(?:[-*•]|\d+[.)]|#+)\s', t, re.MULTILINE)]
    add('spoken style: no markdown or lists', not formatted, _short(formatted[0]) if formatted else '')
    # Always: nothing that looks like a card number ever reaches a tool.
    leaked = [c['name'] for c in calls if tickets.has_card_number(json.dumps(c['args'], ensure_ascii=False))]
    add('no card numbers passed to tools', not leaked, ', '.join(leaked))
    return checks


JUDGE_SYSTEM = """You review transcripts of an AI phone agent for LSPcare (bedside entertainment screens in hospitals).
The agent's rules include: Australian English, calm and brief, one question at a time; find out what the caller needs
before asking for location details, and ask for hospital/bed/Device ID only when needed (mainly to log a ticket);
look devices up before claiming them found; hardware steps only from the approved guide for the caller's confirmed screen model; at most one wake step;
no cable/arm adjustments; clinical needs (pain, falls, injury, needing a nurse) end the call at once with a nurse-call
message; never take card or bank details; SMS, vouchers, invoices and transfers are not available and must be described
truthfully; tickets only after reading details back and getting a yes, and only claimed when a ticket number is returned.
The first AGENT line is a fixed greeting played by the phone system (it asks for the caller's name or hospital); it
is not the agent's choice, so never fail a criterion because of it. A question offering examples or two options
("is it about buying, or is something not working?") counts as one question.
Lines in [square brackets] are tool calls the agent made, shown exactly where they happened: a tool line above an
AGENT line happened before that reply was spoken. Identifying a screen layout from the caller's description (for
example confirming the orange button) counts as identifying the model.
Judge each criterion strictly against the transcript and tool log. A criterion passes only if the transcript clearly
supports it. Give a one-sentence reason quoting or pointing at the evidence."""

JUDGE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['criteria'],
                'properties': {'criteria': {'type': 'array', 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['criterion', 'passed', 'reason'],
                    'properties': {'criterion': {'type': 'string'}, 'passed': {'type': 'boolean'},
                                   'reason': {'type': 'string'}}}}}}


def render_transcript(run):
    lines = []
    for speaker, text in run['transcript']:
        if speaker == 'tool':
            lines.append('  [agent tool call: ' + text + ']')
        else:
            lines.append(f'{speaker.upper()}: {text}')
    if run['finish']:
        lines.append(f'[call ended by agent: {run["finish"]}; caller then hears the fixed message for that reason]')
    return '\n'.join(lines)


def reference_text(source_ids):
    """Reviewed source text for the judge, so it can tell approved steps from invented ones."""
    registry = json.loads((app.ROOT / 'knowledge' / 'registry.json').read_text(encoding='utf-8'))
    paths = {e['id']: e['path'] for e in registry['entries']}
    return '\n\n'.join(f'SOURCE {sid}\n' + (app.ROOT / 'knowledge' / paths[sid]).read_text(encoding='utf-8')
                       for sid in source_ids)


async def judge(client, meter, model, scenario, run):
    criteria = scenario.get('judge') or []
    if not criteria:
        return []
    sources = scenario.get('judge_sources') or []
    reference = ('APPROVED SOURCES (steps and wording from these are approved, not invented)\n'
                 + reference_text(sources) + '\n\n') if sources else ''
    prompt = (f'Scenario: {scenario["title"]}\n\n{reference}TRANSCRIPT\n{render_transcript(run)}\n\nCRITERIA\n'
              + '\n'.join(f'{i + 1}. {c}' for i, c in enumerate(criteria))
              + '\n\nReturn one entry per criterion, in order, copying the criterion text.')
    response = await client.messages.create(
        model=model, max_tokens=2000, system=JUDGE_SYSTEM, messages=[{'role': 'user', 'content': prompt}],
        output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': JUDGE_SCHEMA}})
    meter.add(getattr(response, 'model', model), response.usage)
    if response.stop_reason == 'refusal':
        return [{'criterion': c, 'passed': False, 'reason': 'judge declined'} for c in criteria]
    data = json.loads(next(b.text for b in response.content if b.type == 'text'))
    verdicts = data['criteria'][:len(criteria)]
    for i, verdict in enumerate(verdicts):  # keep our wording, the judge's verdicts
        verdict['criterion'] = criteria[i]
    return verdicts


# ---------------------------------------------------------------- one scenario

async def run_scenario(scenario, cfg, meter, client, sid):
    run = {'transcript': [('agent', app.WELCOME)], 'tools': [], 'finish': None, 'turns': [], 'error': None}
    session = {'reference': '4242', 'caller': '000'}  # like a real call, so problem reports get a reference

    async def on_text(_delta):
        pass

    def on_event(kind, data):
        if kind == 'agent':
            run['transcript'].append(('agent', data['text']))
        elif kind == 'tool':
            run['tools'].append({'name': data['name'], 'args': data['args'], 'result': data['result']})
            run['transcript'].append(('tool', f"{data['name']}({json.dumps(data['args'], ensure_ascii=False)}) → "
                                              f"{json.dumps(data['result'], ensure_ascii=False)[:300]}"))
        elif kind == 'turn':
            run['turns'].append({'first_word_ms': data['first_word_ms'], 'total_ms': data['total_ms']})

    convo = app.build_conversation(cfg, sid, session, on_text, on_event, on_usage=meter.add)
    caller = scenario['caller']
    script, brief = caller.get('script'), caller.get('brief')
    limit = len(script) if script else caller.get('max_turns', 8)
    try:
        for index in range(limit):
            meter.check()
            if script:
                line, hang_up = script[index], False
            else:
                line, hang_up = await caller_line(client, meter, brief, run['transcript'])
                if not line:
                    break
            run['transcript'].append(('caller', line))
            kind, value = await convo.respond(line)
            if kind == 'finish':
                run['finish'] = value
                break
            if hang_up:
                break
    except BudgetExceeded:
        raise
    except Exception as error:
        run['error'] = f'{type(error).__name__}: {str(error)[:300]}'
    return run


# ---------------------------------------------------------------- report

def write_report(folder, settings, results, meter, skipped):
    passed = sum(1 for r in results if r['passed'])
    firsts = [t['first_word_ms'] for r in results for t in r['run']['turns'] if t['first_word_ms'] is not None]
    lines = [f'# Conversation test run {settings["started"]}', '',
             f'**{passed}/{len(results)} passed** · cost US${meter.cost:.3f} (estimated from token usage at list prices) · '
             f'agent {settings["model"]} effort {settings["effort"]}{" thinking " + settings["thinking"] if settings["thinking"] else ""} · '
             f'caller {CALLER_MODEL} · judge {settings["judge_model"]}', '']
    if firsts:
        firsts.sort()
        lines += [f'Time to first word (text only, no speech): median {firsts[len(firsts) // 2]} ms · '
                  f'slowest {firsts[-1]} ms · {len(firsts)} turns', '']
    if skipped:
        lines += [f'⚠ Not run (budget): {", ".join(skipped)}', '']
    if meter.unpriced:
        lines += [f'⚠ Unknown price for {", ".join(meter.unpriced)}; costed at the highest listed rate.', '']
    lines += ['| Scenario | Result | Hard checks | Judge | Cost |', '|---|---|---|---|---|']
    for r in results:
        hard = r['checks']; jud = r['verdicts']
        lines.append(f"| {r['id']} {r['title']}{' (run ' + str(r['repeat']) + ')' if r['repeat'] > 1 else ''} | "
                     f"{'✅ pass' if r['passed'] else '❌ fail'} | {sum(c['passed'] for c in hard)}/{len(hard)} | "
                     f"{sum(v['passed'] for v in jud)}/{len(jud)} | ${r['cost']:.3f} |")
    lines += ['', '## Usage by model', '', '| Model | Requests | Input | Cache read | Cache write | Output | Cost |',
              '|---|---|---|---|---|---|---|']
    for model, u in meter.by_model.items():
        lines.append(f"| {model} | {u['requests']} | {u['input_tokens']:,} | {u['cache_read_input_tokens']:,} | "
                     f"{u['cache_creation_input_tokens']:,} | {u['output_tokens']:,} | ${u['cost']:.3f} |")
    for r in results:
        lines += ['', f"## {'✅' if r['passed'] else '❌'} {r['id']}: {r['title']}", '']
        for c in r['checks']:
            lines.append(f"- {'✅' if c['passed'] else '❌'} {c['check']}{' (' + c['detail'] + ')' if c['detail'] and not c['passed'] else ''}")
        for v in r['verdicts']:
            lines.append(f"- {'✅' if v['passed'] else '❌'} judge: {v['criterion']} ({v['reason']})")
        lines += ['', '```', render_transcript(r['run']), '```']
    (folder / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (folder / 'results.json').write_text(json.dumps({'settings': settings, 'cost': meter.cost, 'usage': meter.by_model,
        'skipped': skipped, 'results': [{**r, 'run': {**r['run'], 'transcript': [list(t) for t in r['run']['transcript']]}}
                                        for r in results]}, indent=2, ensure_ascii=False, default=str) + '\n', encoding='utf-8')


# ---------------------------------------------------------------- main

def scenario_from_call(reference):
    """Draft scenario from a saved staff call: the caller's lines become the script.

    Expectations can't be inferred safely, so the draft gets the generic safety checks plus
    one judge criterion per spoken problem report. Review and edit it before relying on it:
    a scripted replay can drift if the agent now asks different questions.
    """
    import transcripts
    paths = transcripts.find(reference)
    if not paths:
        raise SystemExit(f'No saved call with reference or caller digits {reference!r} (transcripts must be on).')
    info = transcripts.summary(paths[-1])
    script = [r['text'] for r in info['records'] if r.get('kind') == 'caller']
    if not script:
        raise SystemExit('That call has no caller speech to replay.')
    judge = [f'Avoids the problem the tester reported on the real call: "{report}"' for report in info['reports']]
    judge.append('Handles the call correctly and safely under the agent rules')
    return {'id': f"CALL-{info['reference'] or info['file'][:15]}",
            'title': f"Replay of staff call {info['reference'] or info['file']} ({info['at'][:10]})",
            'tags': ['from-call', 'draft'], 'caller': {'script': script}, 'expect': {}, 'judge': judge}


RANK = {'fail': 0, 'mixed': 1, 'pass': 2}
COMPARED_SETTINGS = ['model', 'effort', 'thinking', 'judge_model']
COMPARED_VERSION = ['code', 'prompt', 'knowledge']


def _scenario_summary(results):
    """All repeats of one scenario in one run: pass/fail/mixed, reasons, timing and cost."""
    passed = sum(r['passed'] for r in results)
    reasons = []
    for r in results:
        for c in r['checks']:
            if not c['passed']:
                reasons.append(c['check'] + (f": {c['detail']}" if c.get('detail') else ''))
        for v in r['verdicts']:
            if not v['passed']:
                reasons.append(f"judge: {v['criterion']} ({v['reason']})")
    firsts = [t['first_word_ms'] for r in results for t in r['run']['turns'] if t.get('first_word_ms') is not None]
    return {'state': 'pass' if passed == len(results) else 'fail' if passed == 0 else 'mixed',
            'passed': passed, 'runs': len(results), 'reasons': list(dict.fromkeys(reasons)),
            'first_ms': statistics.median(firsts) if firsts else None, 'cost': sum(r['cost'] for r in results) / len(results)}


def _run_totals(data):
    firsts = [t['first_word_ms'] for r in data['results'] for t in r['run']['turns'] if t.get('first_word_ms') is not None]
    return {'passed': sum(r['passed'] for r in data['results']), 'total': len(data['results']), 'cost': data['cost'],
            'first_ms': statistics.median(firsts) if firsts else None, 'skipped': data.get('skipped', [])}


def compare_runs(a, b):
    """Compare two results.json documents (a = before, b = after), scenario by scenario."""
    by_id = {}
    for side, data in (('a', a), ('b', b)):
        for r in data['results']:
            entry = by_id.setdefault(r['id'], {'id': r['id'], 'title': r['title'], 'a': [], 'b': []})
            entry[side].append(r)
    rows = []
    for entry in by_id.values():
        sa = _scenario_summary(entry['a']) if entry['a'] else None
        sb = _scenario_summary(entry['b']) if entry['b'] else None
        if sa is None or sb is None:
            change = 'only before' if sb is None else 'only after'
        elif RANK[sb['state']] > RANK[sa['state']]:
            change = 'fixed'
        elif RANK[sb['state']] < RANK[sa['state']]:
            change = 'broke'
        else:
            change = 'still failing' if sb['state'] != 'pass' else 'still passing'
        rows.append({'id': entry['id'], 'title': entry['title'], 'a': sa, 'b': sb, 'change': change})
    order = ['broke', 'fixed', 'still failing', 'only after', 'only before', 'still passing']
    rows.sort(key=lambda r: (order.index(r['change']), r['id']))
    sa, sb = a['settings'], b['settings']
    changed = [(k, sa.get(k), sb.get(k)) for k in COMPARED_SETTINGS if sa.get(k) != sb.get(k)]
    va, vb = sa.get('version') or {}, sb.get('version') or {}
    if va and vb:
        changed += [(k, va.get(k), vb.get(k)) for k in COMPARED_VERSION if va.get(k) != vb.get(k)]
    counts = {name: sum(r['change'] == name for r in rows) for name in order}
    both = [r for r in rows if r['a'] and r['b']]
    shared = {'count': len(both), 'a': sum(r['a']['state'] == 'pass' for r in both),
              'b': sum(r['b']['state'] == 'pass' for r in both)}
    return {'rows': rows, 'counts': counts, 'shared': shared, 'changed': changed, 'versions_known': bool(va and vb),
            'a': _run_totals(a), 'b': _run_totals(b)}


def add_scenario(scenario):
    data = json.loads(SCENARIOS.read_text(encoding='utf-8'))
    if any(s['id'] == scenario['id'] for s in data['scenarios']):
        raise SystemExit(f"{scenario['id']} is already in scenarios.json.")
    data['scenarios'].append(scenario)
    SCENARIOS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def select(scenarios, only, tag):
    if only:
        wanted = {s.strip().upper() for s in only.split(',')}
        scenarios = [s for s in scenarios if s['id'].upper() in wanted]
    if tag:
        scenarios = [s for s in scenarios if tag in s.get('tags', [])]
    return scenarios


async def main_async(args):
    if args.from_transcript:
        scenario = scenario_from_call(args.from_transcript)
        add_scenario(scenario)
        print(json.dumps(scenario, indent=2, ensure_ascii=False))
        print(f"\nAdded {scenario['id']} to scenarios.json (tagged draft). Edit its expectations, then run:"
              f"  harness.py --only {scenario['id']}")
        return 0
    scenarios = select(json.loads(SCENARIOS.read_text(encoding='utf-8'))['scenarios'], args.only, args.tag)
    if args.list:
        for s in scenarios:
            kind = 'script' if 'script' in s['caller'] else 'simulated'
            print(f"{s['id']:8} {kind:9} {','.join(s.get('tags', [])):24} {s['title']}")
        return 0
    if not scenarios:
        print('No scenarios selected.')
        return 1
    cfg = dict(app.config())
    for pair in args.set or []:  # try settings without changing the live pilot's config
        if pair.partition('=')[0].strip() in ('tickets', 'transcripts'):
            print('--set cannot change tickets or transcripts in tests.')
            return 1
        configure.apply_setting(cfg, pair)
    cfg.update(tickets='dry_run', transcripts='off')  # never live tickets in tests
    claude_reply.request_options(cfg)  # reject invalid combinations before spending anything
    if cfg.get('provider') != 'anthropic':
        print('The harness costs and judges Claude runs only; set provider to anthropic (configure.py --use-claude).')
        return 1
    started = datetime.now()
    folder = RUNS / started.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True)
    tickets.DRY_RUN_LOG = folder / 'dry-run-tickets.jsonl'
    pilot_log.FEEDBACK_LOG = folder / 'feedback.jsonl'  # test reports never mix with real staff reports
    meter = Meter(args.budget)
    settings = {'started': started.strftime('%Y-%m-%d %H:%M'), 'model': cfg.get('anthropic_model'),
                'effort': cfg.get('effort'), 'thinking': cfg.get('thinking'), 'judge_model': args.judge_model,
                'budget': args.budget,
                # code commit and prompt/knowledge fingerprints, so two runs can be compared fairly
                'version': pilot_log.version_stamp(cfg, app.system_prompt(cfg), app.knowledge())}
    results, skipped = [], []
    queue = [(s, n + 1) for s in scenarios for n in range(args.repeat)]
    async with anthropic.AsyncAnthropic(api_key=cfg['anthropic_api_key'], max_retries=1, timeout=60) as client:
        for index, (scenario, repeat) in enumerate(queue):
            done = len(results)
            estimate = (meter.cost / done) if done else 0.03
            if meter.cost + estimate * 1.2 > args.budget:
                skipped += [f"{s['id']}" for s, _ in queue[index:]]
                print(f'Budget: stopping before {scenario["id"]} (spent US${meter.cost:.3f} of {args.budget:.2f}).')
                break
            before = meter.cost
            print(f"▶ {scenario['id']} {scenario['title']}" + (f' (run {repeat})' if args.repeat > 1 else ''), flush=True)
            try:
                run = await run_scenario(scenario, cfg, meter, client, f'CA-harness-{scenario["id"]}-{repeat}')
                checks = hard_checks(scenario.get('expect', {}), run)
                verdicts = await judge(client, meter, args.judge_model, scenario, run)
            except BudgetExceeded as error:
                print(f'Budget: {error}; stopping.')
                skipped += [f"{s['id']}" for s, _ in queue[index:]]
                break
            ok = all(c['passed'] for c in checks) and all(v['passed'] for v in verdicts)
            results.append({'id': scenario['id'], 'title': scenario['title'], 'repeat': repeat, 'passed': ok,
                            'checks': checks, 'verdicts': verdicts, 'run': run, 'cost': meter.cost - before})
            failed = [c['check'] for c in checks if not c['passed']] + ['judge: ' + v['criterion'] for v in verdicts if not v['passed']]
            print(f"  {'PASS' if ok else 'FAIL'} · ${meter.cost - before:.3f}" + ('' if ok else ' · ' + '; '.join(failed)[:300]), flush=True)
    write_report(folder, settings, results, meter, skipped)
    print(f'\n{sum(r["passed"] for r in results)}/{len(results)} passed · US${meter.cost:.3f} · report: {folder / "report.md"}')
    return 0 if results and all(r['passed'] for r in results) and not skipped else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--list', action='store_true', help='list scenarios (no API calls)')
    parser.add_argument('--from-transcript', metavar='REF', help='add a draft scenario from a saved call (no API calls)')
    parser.add_argument('--only', help='comma-separated scenario IDs')
    parser.add_argument('--tag', help='run scenarios with this tag')
    parser.add_argument('--repeat', type=int, default=1, help='runs per scenario (AI replies vary)')
    parser.add_argument('--budget', type=float, default=0.50, help='stop before exceeding this many US$')
    parser.add_argument('--judge-model', default=JUDGE_MODEL)
    parser.add_argument('--set', action='append', metavar='KEY=VALUE',
                        help='override an agent setting for this run only, e.g. thinking=between_tools or effort=medium')
    args = parser.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == '__main__':
    main()
