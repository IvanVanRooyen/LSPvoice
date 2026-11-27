"""Safe edits to conversation flows (used by the dashboard).

A changed flow is validated exactly as the voice agent validates it at start-up (flows.py),
on a scratch copy of the flows folder together with the other flows, before anything is written.
Saving keeps the previous file in flows/archive/, and records a version and change history in
the flow file. The voice agent reads flows when it starts, so an edit reaches callers after the
agent is restarted (run.ps1).
"""
from datetime import date
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import flows

ROLE = re.compile(r'[A-Za-z][A-Za-z /&().,-]{1,79}')
# Wording and options that can be edited in the form (the structure is edited as JSON).
FLOW_TEXT = ('title', 'use_when', 'on_success', 'on_failure')
FIELD_TEXT = ('label', 'ask')
STEP_TEXT = ('title', 'instruction')


class EditError(ValueError):
    pass


def dumps(flow):
    return json.dumps(flow, indent=2, ensure_ascii=False) + '\n'


def bump(version):
    head, _, last = str(version).rpartition('.')
    return f'{head}.{int(last) + 1}' if head and last.isdigit() else f'{version}.1'


def flow_path(folder, flow_id):
    """File holding a flow, found by its id (file names need not match ids)."""
    for path in sorted(Path(folder).glob('*.json')):
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('id') == flow_id:
                return path
        except ValueError:
            continue
    raise EditError('Unknown flow.')


def from_form(current, form):
    """Apply the dashboard form (flat 'flow.title', 'field.<name>.ask', 'step.<id>.instruction' ...) to a copy."""
    flow = json.loads(json.dumps(current))
    for key in FLOW_TEXT:
        if f'flow.{key}' in form:
            flow[key] = ' '.join(str(form[f'flow.{key}']).split())
    for name, spec in flow['fields'].items():
        for key in FIELD_TEXT:
            if f'field.{name}.{key}' in form:
                spec[key] = ' '.join(str(form[f'field.{name}.{key}']).split())
        if f'field.{name}.present' in form:  # checkboxes are only sent when ticked
            if form.get(f'field.{name}.optional'):
                spec['optional'] = True
            else:
                spec.pop('optional', None)
        if spec['type'] == 'text' and str(form.get(f'field.{name}.max', '')).strip():
            try:
                spec['max'] = int(form[f'field.{name}.max'])
            except ValueError:
                raise EditError(f'{spec["label"]}: the length limit must be a whole number.')
    for step in flow['steps']:
        for key in STEP_TEXT:
            if f'step.{step["id"]}.{key}' in form:
                step[key] = ' '.join(str(form[f'step.{step["id"]}.{key}']).split())
    return flow


def describe_changes(old, new):
    """Plain-language list of what changed, for the preview."""
    out = []
    for key in FLOW_TEXT:
        if old.get(key) != new.get(key):
            out.append((f'Flow {key.replace("_", " ")}', old.get(key), new.get(key)))
    old_fields, new_fields = old.get('fields', {}), new.get('fields', {})
    for name in sorted(set(old_fields) | set(new_fields)):
        a, b = old_fields.get(name), new_fields.get(name)
        if a is None or b is None:
            out.append((f'Field {name}', 'absent' if a is None else 'present', 'absent' if b is None else 'present'))
            continue
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                out.append((f'Field {a.get("label", name)}: {key}', a.get(key), b.get(key)))
    old_steps = [s['id'] for s in old.get('steps', [])]
    new_steps = [s['id'] for s in new.get('steps', [])]
    if old_steps != new_steps:
        out.append(('Step order', ' → '.join(old_steps), ' → '.join(new_steps)))
    by_id = {s['id']: s for s in old.get('steps', [])}
    for step in new.get('steps', []):
        before = by_id.get(step['id'])
        if before:
            for key in sorted(set(before) | set(step)):
                if before.get(key) != step.get(key):
                    out.append((f'Step "{before.get("title")}": {key}', before.get(key), step.get(key)))
    return out


def plan(flow_id, new_flow, role, note, folder=flows.FLOWS_DIR, today=None):
    """Check an edit on a scratch copy of the flows folder. Returns what would change; writes nothing."""
    folder = Path(folder)
    path = flow_path(folder, flow_id)
    old = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(new_flow, dict):
        raise EditError('The flow must be a JSON object.')
    if new_flow.get('id') != flow_id:
        raise EditError('The flow id cannot be changed here (the agent and tests refer to it).')
    compare = lambda f: {k: v for k, v in f.items() if k not in ('version', 'change_history')}
    if compare(old) == compare(new_flow):
        raise EditError('Nothing has changed.')
    if not ROLE.fullmatch(str(role).strip()):
        raise EditError('Say who approved the change by role (for example "Product owner"), not by name.')
    if len(str(note).strip()) < 5:
        raise EditError('Describe what changed and why (a short note).')
    today = (today or date.today()).isoformat()
    version = bump(old.get('version', '1.0'))
    saved = dict(new_flow)
    saved['version'] = version
    saved['change_history'] = list(old.get('change_history', [])) + [
        {'version': version, 'from': old.get('version', '1.0'), 'date': today, 'approved_by': str(role).strip(),
         'note': ' '.join(str(note).split())}]
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / 'flows'
        shutil.copytree(folder, copy, ignore=shutil.ignore_patterns('archive'))
        (copy / path.name).write_text(dumps(saved), encoding='utf-8')
        try:
            flows.load_flows(copy)
        except (flows.FlowError, KeyError, TypeError, AttributeError) as error:
            raise EditError(f'Not saved: the voice agent would not start with this flow ({error}).')
    changes = describe_changes(old, new_flow)  # only once the flow is known to be well formed
    diff = list(difflib.unified_diff(dumps(old).splitlines(), dumps(saved).splitlines(), f'{path.name} (current)',
                                     f'{path.name} (edited)', lineterm='', n=1))
    return {'id': flow_id, 'path': path, 'old': old, 'new': saved, 'old_version': old.get('version', '1.0'),
            'version': version, 'changes': changes, 'diff': diff,
            'archive': folder / 'archive' / f'{flow_id}-v{old.get("version", "1.0")}.json'}


def save(flow_id, new_flow, role, note, folder=flows.FLOWS_DIR, expected_version=None):
    folder = Path(folder)
    current = json.loads(flow_path(folder, flow_id).read_text(encoding='utf-8'))
    if expected_version is not None and current.get('version', '1.0') != expected_version:
        raise EditError('This flow was changed since you opened it; reload and edit again.')
    change = plan(flow_id, new_flow, role, note, folder)
    change['archive'].parent.mkdir(parents=True, exist_ok=True)
    if not change['archive'].exists():
        change['archive'].write_bytes(dumps(change['old']).encode('utf-8'))
    temp = change['path'].with_name(change['path'].name + '.new')
    temp.write_bytes(dumps(change['new']).encode('utf-8'))  # bytes: LF line endings on Windows too
    os.replace(temp, change['path'])
    return change
