"""Flow engine: code-enforced step order for multi-step processes.

A flow is a JSON file in flows/ (see flows/log_ticket.json): fields with types, and steps of
three kinds:
  collect  gather fields; each value is validated in code when the AI records it
  confirm  the system writes the read-back; a "yes" only counts if the caller gave it in a
           later turn than the read-back was produced
  action   the system runs the action (for example create_ticket) after the confirmation;
           the AI cannot run it directly

The AI moves through a flow with four tools: start_flow, flow_record, flow_confirm and
flow_cancel. The current step is shown to it as a short note at the end of each request
(conversation.py), so the cached rules and tool list never change. The step pointer is
derived from what has been recorded, so it can't skip ahead. Flow state lives in the call's
session and ends with the call.

  python flows.py --check        validate every flow file
  python flows.py --diagram ID   print a Mermaid diagram of one flow
"""
import argparse
import json
from pathlib import Path
import sys

import tickets

FLOWS_DIR = Path(__file__).resolve().parent / 'flows'
KINDS = {'collect', 'confirm', 'action'}
TYPES = {'text', 'au_phone', 'boolean'}
ACTION_NAMES = {'create_ticket'}


class FlowError(ValueError):
    pass


# ------------------------------------------------------------------ definitions

def validate_definition(flow, actions=ACTION_NAMES):
    fid = flow.get('id')
    if not fid or not isinstance(flow.get('steps'), list) or not flow['steps']:
        raise FlowError(f'{fid}: needs an id and at least one step')
    fields = flow.get('fields', {})
    for name, spec in fields.items():
        if spec.get('type') not in TYPES:
            raise FlowError(f'{fid}: field {name} has unknown type {spec.get("type")}')
    seen_confirm = False
    for step in flow['steps']:
        if step.get('kind') not in KINDS:
            raise FlowError(f'{fid}: step {step.get("id")} has unknown kind')
        for name in step.get('fields', []) + step.get('also', []) + step.get('one_of', []):
            if name not in fields:
                raise FlowError(f'{fid}: step {step["id"]} uses unknown field {name}')
        if step['kind'] == 'confirm':
            seen_confirm = True
        if step['kind'] == 'action':
            if step.get('action') not in actions:
                raise FlowError(f'{fid}: unknown action {step.get("action")}')
            if not seen_confirm:
                raise FlowError(f'{fid}: an action step must come after a confirm step')
    for key in ('title', 'use_when', 'on_success', 'on_failure'):
        if not flow.get(key):
            raise FlowError(f'{fid}: missing {key}')
    return flow


def load_flows(folder=FLOWS_DIR):
    flows = {}
    for path in sorted(Path(folder).glob('*.json')):
        flow = validate_definition(json.loads(path.read_text(encoding='utf-8')))
        if flow['id'] in flows:
            raise FlowError(f'duplicate flow id {flow["id"]}')
        flows[flow['id']] = flow
    return flows


# ------------------------------------------------------------------ values

def spoken_phone(number):
    """+61412555019 -> 0412 555 019; +61298765432 -> 02 9876 5432 (for read-backs)."""
    national = '0' + number[3:] if number.startswith('+61') else number
    if national.startswith('04') and len(national) == 10:
        return f'{national[:4]} {national[4:7]} {national[7:]}'
    if len(national) == 10:
        return f'{national[:2]} {national[2:6]} {national[6:]}'
    return national


def clean_value(spec, raw):
    """Return (value, problem). Card-like numbers are never stored, in any field."""
    text = ' '.join(str(raw if raw is not None else '').split())
    if tickets.has_card_number(text):
        return None, 'that looks like a card number: never record card or bank details'
    kind = spec['type']
    if kind == 'boolean':
        lowered = text.casefold()
        if lowered in ('true', 'yes', '1'):
            return True, None
        if lowered in ('false', 'no', '0'):
            return False, None
        return None, 'use true or false'
    if not text:
        return None, 'empty value'
    if kind == 'au_phone':
        phone = tickets.au_phone(text)
        return (phone, None) if phone else (None, 'not an Australian mobile or landline number; ask again')
    return text[:spec.get('max', 600)], None


def display(spec, value):
    if spec['type'] == 'au_phone':
        return spoken_phone(value)
    if spec['type'] == 'boolean':
        return 'yes' if value else 'no'
    return value


# ------------------------------------------------------------------ engine

class FlowEngine:
    """One call's flow state, kept in the call session (session['flow'])."""

    def __init__(self, session, flows, actions):
        self.session = session
        self.flows = flows
        self.actions = actions  # name -> async callable(values) -> {'ok': bool, ...}

    @property
    def state(self):
        return self.session.get('flow')

    def _turn(self):
        return self.session.get('caller_turns', 0)

    def _definition(self):
        return self.flows[self.state['id']]

    def _step(self):
        """First step not yet satisfied: derived from what has been recorded, never stored."""
        st, flow = self.state, self._definition()
        values = st['values']
        for index, step in enumerate(flow['steps']):
            if step['kind'] == 'collect':
                missing = [f for f in step.get('fields', []) if f not in values]
                if step.get('one_of') and not any(f in values for f in step['one_of']):
                    missing.append(' or '.join(step['one_of']))
                if missing:
                    return index, step, missing
            elif step['kind'] == 'confirm' and not st['confirmed']:
                return index, step, []
            elif step['kind'] == 'action' and st.get('result') is None:
                return index, step, []
        return len(flow['steps']), None, []

    def _readback(self):
        flow, values = self._definition(), self.state['values']
        parts = [f'{spec["label"]}: {display(spec, values[name])}' for name, spec in flow['fields'].items()
                 if name in values and not (spec['type'] == 'boolean' and not values[name])]
        return 'Just to confirm. ' + '. '.join(parts) + '. Is that all correct?'

    def status(self, **extra):
        if not self.state:
            return {'active': False, **extra}
        flow = self._definition()
        index, step, missing = self._step()
        if step and step['kind'] == 'confirm' and not self.state['readback']:
            self.state['readback'] = self._readback()
            self.state['readback_turn'] = self._turn()  # the caller must answer in a later turn
        result = {'active': True, 'flow': flow['id'], 'step': step['id'] if step else 'done',
                  'step_number': f'{min(index + 1, len(flow["steps"]))} of {len(flow["steps"])}',
                  'instruction': step['instruction'] if step else '',
                  'still_needed': [flow['fields'][m]['ask'] if m in flow['fields'] else m for m in missing],
                  'recorded': {flow['fields'][k]['label']: display(flow['fields'][k], v) for k, v in self.state['values'].items()}}
        if step and step['kind'] == 'confirm':
            result['readback'] = self.state['readback']
        return {**result, **extra}

    def note(self):
        """Short note appended to each model request while a flow is active."""
        if not self.state:
            return None
        s = self.status()
        lines = [f"ACTIVE FLOW: {self._definition()['title']} (step {s['step_number']}).",
                 f"Do now: {s['instruction']}"]
        if s['still_needed']:
            lines.append('Still needed: ' + '; '.join(s['still_needed']) + '.')
        if s['recorded']:
            lines.append('Recorded: ' + '; '.join(f'{k} = {v}' for k, v in s['recorded'].items()) + '.')
        if s.get('readback'):
            lines.append(f"Read-back to say: {s['readback']}")
        lines.append('Record each value with flow_record as soon as the caller gives it. Safety and the '
                     "caller's questions come first; the flow waits. Use flow_cancel if they no longer want this.")
        return '\n'.join(lines)

    def start(self, flow_id):
        if flow_id not in self.flows:
            return {'started': False, 'reason': 'unknown flow'}
        if self.state:
            if self.state['id'] == flow_id:
                return self.status(started=False, reason='already in this flow; carry on from the current step')
            return {'started': False, 'reason': f'finish or cancel the {self.state["id"]} flow first'}
        self.session['flow'] = {'id': flow_id, 'values': {}, 'confirmed': False, 'readback': None,
                                'readback_turn': None, 'result': None}
        return self.status(started=True)

    def record(self, items):
        if not self.state:
            return {'recorded': False, 'reason': 'no flow is active; call start_flow first'}
        fields = self._definition()['fields']
        accepted, problems = {}, {}
        for item in items:
            name, raw = item.get('field'), item.get('value')
            if name not in fields:
                problems[str(name)] = 'not a field of this flow'
                continue
            value, problem = clean_value(fields[name], raw)
            if problem:
                problems[name] = problem
                continue
            if self.state['values'].get(name) != value:
                self.state['values'][name] = value
                accepted[name] = display(fields[name], value)
        if accepted:  # any change means the details must be read back again
            self.state['confirmed'] = False
            self.state['readback'] = None
        return self.status(recorded=accepted, problems=problems)

    async def confirm(self, answer):
        if not self.state:
            return {'confirmed': False, 'reason': 'no flow is active'}
        index, step, missing = self._step()
        if not step or step['kind'] != 'confirm':
            return self.status(confirmed=False, reason='not at the read-back step yet; collect what is still needed first')
        self.status()  # makes sure a read-back exists
        if self._turn() <= self.state['readback_turn']:
            return self.status(confirmed=False, reason='read the details back and wait for the caller to answer before confirming')
        if answer != 'yes':
            self.state['readback'] = None
            return self.status(confirmed=False, reason='ask what needs changing and record the corrected value; a new read-back will follow')
        self.state['confirmed'] = True
        flow = self._definition()
        outcome = {'confirmed': True}
        while True:
            index, step, _ = self._step()
            if not step or step['kind'] != 'action':
                break
            result = await self.actions[step['action']](dict(self.state['values']))
            self.state['result'] = result
            outcome.update(action=step['action'], result=result)
            if not result.get('ok'):
                break
        ok = bool(outcome.get('result', {}).get('ok'))
        outcome['next'] = flow['on_success'] if ok else flow['on_failure']
        self.session.setdefault('flows_done', []).append({'id': flow['id'], 'ok': ok})
        self.session['flow'] = None
        return outcome

    def cancel(self, reason):
        if not self.state:
            return {'cancelled': False, 'reason': 'no flow is active'}
        self.session.setdefault('flows_done', []).append({'id': self.state['id'], 'ok': False, 'cancelled': str(reason)[:200]})
        self.session['flow'] = None
        return {'cancelled': True}


def mermaid(flow):
    """Diagram of a flow for docs and review (also drawn on the dashboard)."""
    lines = ['flowchart TD', f'  start(["{flow["title"]}"])']
    previous = 'start'
    for step in flow['steps']:
        sid = step['id'].replace('-', '_')
        label = step['title']
        if step['kind'] == 'collect':
            needed = step.get('fields', []) + ([' or '.join(step['one_of'])] if step.get('one_of') else [])
            label += '<br/>' + ', '.join(needed)
            lines.append(f'  {sid}["{label}"]')
        elif step['kind'] == 'confirm':
            lines.append(f'  {sid}{{"{label}<br/>caller says yes"}}')
            lines.append(f'  {sid} -- no: correct a detail --> {previous}')
        else:
            lines.append(f'  {sid}[["{label}"]]')
        lines.append(f'  {previous} --> {sid}')
        previous = sid
    lines += ['  done(["Tell the caller the outcome"])', f'  {previous} --> done']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--diagram', metavar='ID')
    args = parser.parse_args()
    flows = load_flows()
    if args.check:
        for flow in flows.values():
            print(f"OK  {flow['id']}: {len(flow['steps'])} steps, {len(flow['fields'])} fields")
    else:
        if args.diagram not in flows:
            raise SystemExit(f'No flow {args.diagram!r}. Flows: {", ".join(flows)}')
        print(mermaid(flows[args.diagram]))


if __name__ == '__main__':
    main()
