"""Live call transcripts for the staff pilot (fictional details only).

Turn on with:  configure.py --set transcripts=on   (off by default)
While on, each call prints a readable transcript in the pilot window as it happens and is
saved to private-config/transcripts/<time>-<call>.jsonl (readable by your user only).

  python transcripts.py --follow     follow the newest call live (e.g. in a second terminal)
  python transcripts.py --show [N]   print the newest (or Nth newest) saved call
  python transcripts.py --list       list saved calls (reference, caller digits, flags)
  python transcripts.py --find 4721  print the call with this reference (or caller's last digits)
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import make_private_dir, private_path, restrict

FOLDER = private_path('transcripts')


def _short(value, limit=160):
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _tool_summary(name, args, result):
    result = result or {}
    if name == 'lookup_device':
        found = (f"found: {result.get('hospital_code')} ward {result.get('ward')}, bed {result.get('bed_id')}"
                 if result.get('device_found') else 'not found (' + str(result.get('lookup_reason') or result.get('reason')) + ')')
        return f"lookup_device({args.get('hospital')}, bed={args.get('bed_id') or '-'}, id={args.get('device_id') or '-'}) → {found}"
    if name == 'get_guide':
        return f"get_guide({args.get('guide_id')}) → {'v' + str(result.get('version')) if result.get('found') else 'not found'}"
    if name == 'search_knowledge':
        return f"search_knowledge({args.get('query')!r}) → {len(result.get('results', []))} sections"
    if name == 'start_flow':
        return (f"flow started: {args.get('flow_id')} → step {result.get('step')}" if result.get('started')
                else f"start_flow({args.get('flow_id')}) refused: {result.get('reason')}")
    if name == 'flow_record':
        recorded = '; '.join(f'{k} = {v}' for k, v in (result.get('recorded') or {}).items()) or 'nothing new'
        problems = '; '.join(f'{k}: {v}' for k, v in (result.get('problems') or {}).items())
        return f"flow: recorded {recorded}" + (f" · refused {problems}" if problems else '') + f" → next: {result.get('step')}"
    if name == 'flow_confirm':
        if not result.get('confirmed'):
            return f"flow_confirm({args.get('answer')}) not accepted: {result.get('reason')}"
        inner = result.get('result') or {}
        if inner.get('ticket_logged'):
            return f"flow confirmed → ticket #{inner.get('ticket_number')}" + (' (dry run)' if inner.get('dry_run') else '')
        return f"flow confirmed → {result.get('action')} failed: {inner.get('reason')}"
    if name == 'flow_cancel':
        return f"flow cancelled: {args.get('reason')}"
    if name == 'report_problem':
        return f"⚑ PROBLEM REPORTED: {args.get('description')}"
    if name == 'create_ticket':
        if result.get('ticket_logged'):
            return f"create_ticket → #{result.get('ticket_number')}" + (' (dry run)' if result.get('dry_run') else '') \
                   + (' (already logged)' if result.get('already_logged') else '')
        return f"create_ticket → not logged: {result.get('reason')}"
    return f'{name} → {_short(json.dumps(result), 80)}'


USAGE_KEYS = {'input_tokens': 'input', 'output_tokens': 'output',
              'cache_read_input_tokens': 'cache_read', 'cache_creation_input_tokens': 'cache_write'}


def usage_record(model, usage):
    """Token counts from one API response, as saved in a 'usage' transcript event."""
    return {'model': str(model or 'unknown'),
            **{short: int(getattr(usage, key, 0) or 0) for key, short in USAGE_KEYS.items()}}


def format_event(record):
    """One readable line for a transcript record, or None for records not shown in the terminal."""
    kind, at = record.get('kind'), record.get('at', '')[11:19]
    if kind == 'usage':
        return None
    if kind == 'greeting':
        return f'{at}  AGENT   │ {record["text"]}  (greeting)'
    if kind == 'caller':
        return f'{at}  CALLER  │ {record["text"]}'
    if kind == 'agent':
        return f'{at}  AGENT   │ {record["text"]}'
    if kind == 'tool':
        return f'{at}  tool    │ {_tool_summary(record["name"], record.get("args", {}), record.get("result"))}'
    if kind == 'interrupted':
        return f'{at}  ↯       │ caller interrupted; heard so far: "{_short(record.get("heard", ""), 100)}"'
    if kind == 'finish':
        return f'{at}  END     │ {record.get("reason")}'
    if kind == 'turn':
        first = record.get('first_word_ms')
        return f'{at}  ·       │ first word {first if first is not None else "-"} ms · turn {record.get("total_ms")} ms'
    if kind == 'suppressed':
        return f'{at}  (muted) │ not spoken, question already asked: "{_short(record.get("text", ""), 120)}"'
    if kind == 'error':
        return f'{at}  ERROR   │ {record.get("text")}'
    if kind == 'started':
        version = record.get('version') or {}
        ref = f' · ref {record["reference"]}' if record.get('reference') else ''
        caller = f' · caller …{record["caller"]}' if record.get('caller') else ''
        return (f'──── call …{record.get("call", "")}{ref}{caller} started {record.get("at", "")[:19].replace("T", " ")} ────'
                + (f'\n          version {version.get("code")} · {version.get("model")} effort {version.get("effort")} '
                   f'thinking {version.get("thinking")} · prompt {version.get("prompt")} · knowledge {version.get("knowledge")}'
                   if version else ''))
    if kind == 'closed':
        return f'──── call ended ({record.get("reason") or "hang-up"}){" · ⚑ FLAGGED by caller" if record.get("flagged") else ""} ────'
    return f'{at}  {kind}'


class CallTranscript:
    """Per-call transcript writer. Does nothing when disabled."""
    def __init__(self, sid, enabled, echo=True, folder=None, details=None):
        self.enabled, self.echo = enabled, echo
        self.path = None
        if not enabled:
            return
        folder = Path(folder or FOLDER)
        make_private_dir(folder)
        call = str(sid or 'unknown')[-6:]
        details = {k: v for k, v in (details or {}).items() if v}
        ref = f"-ref{details['reference']}" if details.get('reference') else ''
        self.path = folder / f'{datetime.now():%Y%m%d-%H%M%S}{ref}-{call}.jsonl'
        self.path.touch()
        restrict(self.path)
        self.event('started', {'call': call, **details})

    def event(self, kind, data):
        if not self.enabled:
            return
        record = {'at': datetime.now().isoformat(timespec='seconds'), 'kind': kind, **data}
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        line = format_event(record) if self.echo else None
        if line:
            print(line, file=sys.stderr, flush=True)


def _calls():
    return sorted(FOLDER.glob('*.jsonl')) if FOLDER.is_dir() else []


def summary(path):
    """Reference, caller digits, version, flags and length of one saved call."""
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    started = next((r for r in records if r.get('kind') == 'started'), {})
    closed = next((r for r in records if r.get('kind') == 'closed'), {})
    reports = [r['args'].get('description') for r in records if r.get('kind') == 'tool' and r.get('name') == 'report_problem']
    return {'file': path.name, 'at': started.get('at', ''), 'reference': started.get('reference', ''),
            'caller': started.get('caller', ''), 'version': started.get('version', {}),
            'turns': sum(r.get('kind') == 'caller' for r in records), 'ended': closed.get('reason'),
            'flagged': bool(reports) or bool(closed.get('flagged')), 'reports': reports, 'records': records}


def _seconds(a, b):
    try:
        return max(0, round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()))
    except (TypeError, ValueError):
        return None


def stats(records):
    """Call figures for the dashboard: duration, reply timing, tools, ticket and token totals."""
    started = next((r for r in records if r.get('kind') == 'started'), {})
    closed = next((r for r in records if r.get('kind') == 'closed'), None)
    turns = [r for r in records if r.get('kind') == 'turn']
    firsts = [t['first_word_ms'] for t in turns if t.get('first_word_ms') is not None]
    totals = [t['total_ms'] for t in turns if t.get('total_ms') is not None]
    tools = [r for r in records if r.get('kind') == 'tool']
    ticket = None
    for r in tools:
        result = r.get('result') or {}
        inner = result.get('result') if r.get('name') == 'flow_confirm' else result
        if isinstance(inner, dict) and inner.get('ticket_logged'):
            ticket = {'number': inner.get('ticket_number'), 'dry_run': bool(inner.get('dry_run'))}
    usages = [r for r in records if r.get('kind') == 'usage']
    by_model = {}
    for u in usages:
        entry = by_model.setdefault(u.get('model', 'unknown'), {**{k: 0 for k in USAGE_KEYS.values()}, 'requests': 0})
        entry['requests'] += 1
        for short in USAGE_KEYS.values():
            entry[short] += u.get(short, 0)
    tokens = {short: sum(u.get(short, 0) for u in usages) for short in USAGE_KEYS.values()} if usages else None
    last = records[-1].get('at') if records else None
    return {'duration_s': _seconds(started.get('at'), (closed or {}).get('at') or last),
            'open': closed is None,
            'caller_turns': sum(r.get('kind') == 'caller' for r in records),
            'replies': len(turns),
            'first_word_avg_ms': round(sum(firsts) / len(firsts)) if firsts else None,
            'first_word_max_ms': max(firsts) if firsts else None,
            'turn_avg_ms': round(sum(totals) / len(totals)) if totals else None,
            'first_words_ms': firsts,
            'tool_calls': len(tools),
            'tools': sorted({r.get('name') for r in tools}),
            'interruptions': sum(r.get('kind') == 'interrupted' for r in records),
            'errors': sum(r.get('kind') == 'error' for r in records),
            'ticket': ticket, 'requests': len(usages), 'tokens': tokens, 'by_model': by_model}


def find(text):
    text = str(text).strip()
    return [p for p in _calls() if (lambda s: text in (s['reference'], s['caller']))(summary(p))]


def _print_file(path):
    for line in path.read_text(encoding='utf-8').splitlines():
        text = format_event(json.loads(line)) if line.strip() else None
        if text:
            print(text)


def read_new(path, position):
    """Complete records added since byte offset position, and the new offset.

    Reads bytes so the offset is exact; a line still being written (no newline yet) is
    left for the next poll.
    """
    records = []
    with path.open('rb') as handle:
        handle.seek(position)
        while True:
            line = handle.readline()
            if not line.endswith(b'\n'):
                break
            position += len(line)
            if line.strip():
                records.append(json.loads(line.decode('utf-8')))
    return records, position


def follow():
    print(f'Following calls in {FOLDER}. Ctrl+C to stop.', flush=True)
    current, position = None, 0
    while True:
        calls = _calls()
        if calls and calls[-1] != current:
            current, position = calls[-1], 0
        if current:
            records, position = read_new(current, position)
            for record in records:
                line = format_event(record)
                if line:
                    print(line, flush=True)
        time.sleep(0.4)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--follow', action='store_true')
    group.add_argument('--show', nargs='?', const=1, type=int, metavar='N')
    group.add_argument('--list', action='store_true')
    group.add_argument('--find', metavar='REF')
    args = parser.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if args.follow:
        try:
            follow()
        except KeyboardInterrupt:
            pass
    elif args.list:
        for path in _calls():
            s = summary(path)
            print(f"{s['at'][:16].replace('T', ' ')}  ref {s['reference'] or '----'}  caller …{s['caller'] or '???'}  "
                  f"{s['turns']:>2} turns  ended {s['ended'] or '?':8} {'⚑ ' + '; '.join(s['reports']) if s['flagged'] else ''}")
    elif args.find:
        matches = find(args.find)
        if not matches:
            print('No saved call with that reference or caller digits.')
        for path in matches:
            _print_file(path)
            print()
    else:
        calls = _calls()
        if len(calls) < args.show:
            print('No saved transcript there. Turn transcripts on: configure.py --set transcripts=on')
            return
        _print_file(calls[-args.show])


if __name__ == '__main__':
    main()
