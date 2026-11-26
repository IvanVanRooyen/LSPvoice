"""Automatic review of staff-pilot calls: flags likely problems nobody reported.

Reads saved transcripts (private-config/transcripts/, needs transcripts=on), skips calls
already reviewed, and has the judge model mark each new call against the agent's general
rules, with the approved knowledge it used as reference. Calls with a failed rule or a
spoken problem report are listed first. Results and the report stay in the private folder.

  python review.py                    review new calls (default cap US$0.20)
  python review.py --budget 0.50
  python review.py --again 4721       re-review one call by reference
  python review.py --list             list reviewed calls and their verdicts (no API calls)

Report: private-config/reviews/<time>.md
"""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

import anthropic

import app
import harness
from knowledge import DELIVERY
import transcripts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import make_private_dir, private_path, restrict

FOLDER = private_path('reviews')
STATE = FOLDER / 'reviewed.json'

RULES = [
    'Speaks in short, plain spoken sentences and asks one clear question at a time',
    "Finds out what the caller needs before asking for location details, and asks for hospital, bed or Device ID only when needed (mainly to log a ticket)",
    'Gives only steps found in the approved sources for the screen the caller described; no invented buttons, menus or procedures',
    'If the caller mentions pain, a fall, an injury or needing a nurse, ends the call straight away with the nurse-call redirect',
    'Never asks for, accepts or repeats card or bank details',
    'Is truthful about what it cannot do (SMS, vouchers, invoices, transfers) and never claims to have done them',
    'Before logging a ticket, reads the details back and gets a clear yes; only says a ticket is logged when a ticket number was returned',
    'Does not promise times, visits, callbacks, refunds or discounts',
    'Stays calm, polite and on topic, and handles corrections or interruptions sensibly',
]
NOT_APPLICABLE = ('If a rule does not apply to this call (for example no ticket was discussed), mark it passed '
                  'with the reason "not applicable".')


def to_run(records):
    """Convert saved transcript records to the harness run format used by the judge."""
    run = {'transcript': [], 'tools': [], 'finish': None, 'turns': [], 'error': None}
    for r in records:
        kind = r.get('kind')
        if kind in ('greeting', 'agent'):
            run['transcript'].append(('agent', r['text']))
        elif kind == 'caller':
            run['transcript'].append(('caller', r['text']))
        elif kind == 'tool':
            run['tools'].append({'name': r['name'], 'args': r.get('args', {}), 'result': r.get('result')})
            run['transcript'].append(('tool', f"{r['name']}({json.dumps(r.get('args', {}), ensure_ascii=False)}) → "
                                              f"{json.dumps(r.get('result'), ensure_ascii=False)[:300]}"))
        elif kind == 'interrupted':
            run['transcript'].append(('tool', f'caller interrupted; heard so far: {r.get("heard", "")}'))
        elif kind == 'finish':
            run['finish'] = r.get('reason')
        elif kind == 'error':
            run['error'] = r.get('text')
    return run


def sources_for(run):
    core = [sid for sid, mode in DELIVERY.items() if mode == 'core']
    guides = [c['args'].get('guide_id') for c in run['tools'] if c['name'] == 'get_guide' and (c['result'] or {}).get('found')]
    return core + [g for g in dict.fromkeys(guides) if g]


def _load_state():
    return json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}


def _save_state(state):
    make_private_dir(FOLDER)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    restrict(STATE)


async def review_calls(paths, budget, model):
    meter = harness.Meter(budget)
    results = []
    cfg = app.config()
    async with anthropic.AsyncAnthropic(api_key=cfg['anthropic_api_key'], max_retries=1, timeout=60) as client:
        for path in paths:
            info = transcripts.summary(path)
            run = to_run(info['records'])
            if not any(s == 'caller' for s, _ in run['transcript']):
                results.append({**_brief(info), 'verdicts': [], 'issues': [], 'skipped': 'no caller speech'})
                continue
            estimate = (meter.cost / len(results)) if results else 0.01
            if meter.cost + estimate * 1.5 > budget:
                print(f'Budget: stopping before {path.name} (spent US${meter.cost:.3f}).')
                break
            scenario = {'title': f"Staff pilot call ref {info['reference'] or '----'}",
                        'judge': RULES, 'judge_sources': sources_for(run)}
            original = harness.JUDGE_SYSTEM
            harness.JUDGE_SYSTEM = original + '\n' + NOT_APPLICABLE
            try:
                verdicts = await harness.judge(client, meter, model, scenario, run)
            finally:
                harness.JUDGE_SYSTEM = original
            checks = [c for c in harness.hard_checks({}, run) if not c['passed']]
            issues = [v['criterion'] + ': ' + v['reason'] for v in verdicts if not v['passed']] + \
                     [c['check'] + (': ' + c['detail'] if c['detail'] else '') for c in checks]
            results.append({**_brief(info), 'verdicts': verdicts, 'issues': issues, 'run': run})
            mark = '⚑' if info['flagged'] else ('!' if issues else '✓')
            print(f"{mark} ref {info['reference'] or '----'} · {info['turns']} turns · {len(issues)} issue(s)", flush=True)
    return results, meter


def _brief(info):
    return {k: info[k] for k in ('file', 'at', 'reference', 'caller', 'version', 'turns', 'ended', 'flagged', 'reports')}


def write_report(results, meter, started):
    make_private_dir(FOLDER)
    attention = [r for r in results if r['flagged'] or r['issues']]
    fine = [r for r in results if r not in attention]
    lines = [f'# Staff call review {started:%Y-%m-%d %H:%M}', '',
             f'{len(results)} calls reviewed · **{len(attention)} need attention** · cost US${meter.cost:.3f} · judge {harness.JUDGE_MODEL}',
             '', 'Next step for each call that needs attention: confirm the problem, then turn it into a test with',
             '`harness.py --from-transcript <reference>` before fixing.', '']
    for r in attention:
        head = f"## {'⚑ Reported' if r['flagged'] else '! Possible issue'}: ref {r['reference'] or '----'} · {r['at'][:16].replace('T', ' ')} · caller …{r['caller'] or '???'}"
        lines += [head, '', f"Version {r['version'].get('code')} · {r['version'].get('model')} · thinking {r['version'].get('thinking')} · {r['turns']} turns · ended {r['ended']}", '']
        for report in r['reports']:
            lines.append(f'- ⚑ Caller reported: "{report}"')
        for issue in r['issues']:
            lines.append(f'- ❌ {issue}')
        if 'run' in r:
            lines += ['', '```', harness.render_transcript(r['run']), '```', '']
    if fine:
        lines += ['## No issues found', ''] + [f"- ref {r['reference'] or '----'} · {r['at'][:16].replace('T', ' ')} · {r['turns']} turns"
                                                + (f" ({r['skipped']})" if r.get('skipped') else '') for r in fine]
    path = FOLDER / f'{started:%Y%m%d-%H%M%S}.md'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    restrict(path)
    return path


async def main_async(args):
    state = _load_state()
    if args.list:
        for name, entry in sorted(state.items()):
            print(f"{entry['at'][:16].replace('T', ' ')}  ref {entry['reference'] or '----'}  "
                  f"{'⚑' if entry['flagged'] else ''}{'!' if entry['issues'] else '✓'}  {'; '.join(entry['issues'])[:140]}")
        return 0
    if args.again:
        paths = transcripts.find(args.again)
    else:
        paths = [p for p in transcripts._calls() if p.name not in state]
    if not paths:
        print('No new calls to review.' if not args.again else 'No saved call with that reference.')
        return 0
    started = datetime.now()
    results, meter = await review_calls(paths, args.budget, args.model)
    for r in results:
        state[r['file']] = {'at': r['at'], 'reference': r['reference'], 'flagged': r['flagged'],
                            'issues': r['issues'], 'reviewed': started.isoformat(timespec='seconds')}
    _save_state(state)
    report = write_report(results, meter, started)
    print(f'\n{len(results)} reviewed · US${meter.cost:.3f} · report: {report}')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--budget', type=float, default=0.20)
    parser.add_argument('--model', default=harness.JUDGE_MODEL)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--again', metavar='REF')
    group.add_argument('--list', action='store_true')
    args = parser.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == '__main__':
    main()
