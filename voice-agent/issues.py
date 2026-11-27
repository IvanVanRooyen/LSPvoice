"""Issue tracker for the staff pilot: one list of problems found in calls, from finding to fix.

Issues come from three places:
  review   a finding from the automatic call review (review.py)
  report   a problem a tester reported by voice during a call
  manual   added on the dashboard (for example a report posted in Teams)

Each issue moves new -> open (confirmed) -> fixed, or is dismissed (not a problem, duplicate).
A fixed issue linked to a test scenario counts as verified once that scenario passes in a test
run started after the fix. The list is kept in private-config/issues.json (it quotes calls).
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import make_private_dir, private_path, restrict

import pilot_log
import review

STORE = private_path('issues.json')
STATUSES = {'new': 'New', 'open': 'Open', 'fixed': 'Fixed', 'dismissed': 'Dismissed'}
ACTIVE = ('new', 'open')
CATEGORIES = ['unsorted', 'knowledge gap', 'rule or prompt', 'tool or code', 'speech recognition', 'voice',
              'speed', 'caller expectation']
SOURCES = {'review': 'Automatic review', 'report': 'Reported by voice', 'manual': 'Added by hand'}


def now():
    return datetime.now().isoformat(timespec='seconds')


def load():
    if not STORE.is_file():
        return {'next': 1, 'issues': []}
    return json.loads(STORE.read_text(encoding='utf-8'))


def save(data):
    make_private_dir(STORE.parent)
    temp = STORE.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    restrict(temp)
    os.replace(temp, STORE)
    restrict(STORE)


def _title(text, limit=90):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _new(data, *, title, detail, source, key=None, call_file=None, reference='', category='unsorted', at=None):
    issue = {'id': f"ISS-{data['next']:03d}", 'title': _title(title), 'detail': str(detail), 'source': source,
             'source_key': key, 'call_file': call_file, 'reference': reference or '', 'category': category,
             'status': 'new', 'scenario': '', 'fixed_in': '', 'fixed_at': '', 'created': at or now(), 'updated': now(),
             'history': [{'at': now(), 'change': f'created from {SOURCES[source].lower()}'}]}
    data['next'] += 1
    data['issues'].append(issue)
    return issue


def _report_files():
    """Call reference -> saved call file name, for linking spoken reports to their call."""
    import transcripts
    found = {}
    for path in transcripts._calls():
        parts = path.stem.split('-ref')
        if len(parts) == 2:
            found[parts[1][:4]] = path.name
    return found


def sync():
    """Add an issue for every review finding and spoken report not yet in the list. Returns how many were added."""
    data = load()
    known = {i['source_key'] for i in data['issues'] if i.get('source_key')}
    added = 0
    for file, entry in sorted(review._load_state().items()):
        for text in entry.get('issues', []):
            key = 'review:' + file + ':' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]
            if key in known:
                continue
            rule, _, reason = text.partition(': ')
            _new(data, title=rule, detail=reason or text, source='review', key=key, call_file=file,
                 reference=entry.get('reference', ''), at=entry.get('reviewed'))
            known.add(key); added += 1
    reports = pilot_log.read_reports()
    files = _report_files() if reports else {}
    for r in reports:
        key = f"report:{r.get('at')}:{r.get('call')}"
        if key in known:
            continue
        _new(data, title=r.get('description', ''), detail=r.get('description', ''), source='report', key=key,
             call_file=files.get(r.get('reference')), reference=r.get('reference', ''), at=r.get('at'))
        known.add(key); added += 1
    if added:
        save(data)
    return added


def get(data, issue_id):
    return next((i for i in data['issues'] if i['id'] == issue_id), None)


def add(title, detail='', reference='', category='unsorted'):
    data = load()
    if not str(title).strip():
        raise ValueError('An issue needs a title.')
    if category not in CATEGORIES:
        raise ValueError('Unknown category.')
    import transcripts
    file = next((p.name for p in transcripts._calls() if reference and f'-ref{reference}-' in p.name), None)
    issue = _new(data, title=title, detail=detail, source='manual', call_file=file, reference=reference, category=category)
    save(data)
    return issue


def update(issue_ids, status=None, category=None, scenario=None, note='', fixed_in=None):
    """Change one or more issues; every change is written to the issue's history."""
    data = load()
    changed = []
    for issue_id in issue_ids:
        issue = get(data, issue_id)
        if issue is None:
            raise KeyError(issue_id)
        steps = []
        if status is not None and status != issue['status']:
            if status not in STATUSES:
                raise ValueError('Unknown status.')
            steps.append(f"status {issue['status']} → {status}")
            issue['status'] = status
            if status == 'fixed':
                issue['fixed_in'] = fixed_in or pilot_log.CODE_VERSION
                issue['fixed_at'] = now()
                steps.append(f"fixed in {issue['fixed_in']}")
        if category is not None and category != issue['category']:
            if category not in CATEGORIES:
                raise ValueError('Unknown category.')
            steps.append(f"category → {category}")
            issue['category'] = category
        if scenario is not None and scenario != issue['scenario']:
            steps.append(f"test scenario → {scenario or 'none'}")
            issue['scenario'] = scenario
        if note.strip():
            steps.append('note: ' + note.strip()[:2000])
        if steps:
            issue['history'].append({'at': now(), 'change': '; '.join(steps)})
            issue['updated'] = now()
            changed.append(issue)
    if changed:
        save(data)
    return changed


def verification(issue, results):
    """What the tests say about an issue. results: scenario id -> list of (passed, started) newest first."""
    if not issue['scenario']:
        return None
    runs = results.get(issue['scenario'], [])
    if not runs:
        return {'state': 'not run', 'text': 'scenario not run yet'}
    passed, started = runs[0]
    if issue['status'] == 'fixed':
        after = [(p, s) for p, s in runs if s and s >= issue['fixed_at'][:16].replace('T', ' ')]
        if not after:
            return {'state': 'waiting', 'text': 'fixed: run the scenario to verify'}
        return ({'state': 'verified', 'text': 'verified: passed after the fix'} if after[0][0]
                else {'state': 'failing', 'text': 'still failing after the fix'})
    return ({'state': 'passing', 'text': 'scenario passes'} if passed else {'state': 'failing', 'text': 'scenario fails (reproduces it)'})
