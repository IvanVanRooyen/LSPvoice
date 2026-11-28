"""Offline tests for flow edits and the stricter flow checks. No API calls."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import flow_edit
import flows


class FlowEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'flows'
        shutil.copytree(flows.FLOWS_DIR, self.dir, ignore=shutil.ignore_patterns('archive'))
        self.flow = json.loads((self.dir / 'log_ticket.json').read_text(encoding='utf-8'))

    def changed(self, edit):
        flow = json.loads(json.dumps(self.flow))
        edit(flow)
        return flow

    def test_stricter_checks_protect_the_ticket_action(self):
        cases = {
            'callback_number (au_phone) asked for': lambda f: f['fields']['callback_number'].update(optional=True),
            'needs hospital': lambda f: f['steps'][1]['fields'].remove('hospital'),
            'bed_id or device_id': lambda f: f['steps'][1].pop('one_of'),
            'own id': lambda f: f['steps'][1].update(id='problem'),
            'label and an "ask"': lambda f: f['fields']['bed_id'].update(ask=' '),
            'max must be': lambda f: f['fields']['caller_name'].update(max=0),
            'title and an instruction': lambda f: f['steps'][0].update(instruction=''),
            'at least one field': lambda f: f['steps'].insert(0, {'id': 'x', 'kind': 'collect', 'title': 'X', 'instruction': 'Y'}),
        }
        for message, edit in cases.items():
            with self.assertRaises(flows.FlowError, msg=message) as caught:
                flows.validate_definition(self.changed(edit))
            self.assertIn(message, str(caught.exception))
        flows.validate_definition(self.changed(lambda f: f['fields']['bed_id'].update(ask='the bed or room number')))

    def test_form_edit_saves_archived_versioned_lf(self):
        form = {'step.location.instruction': 'Ask for the bed number first,  then the hospital.',
                'field.bed_id.present': '1', 'field.bed_id.label': 'Bed or room', 'field.bed_id.ask': 'the bed or room number',
                'field.bed_id.optional': '1', 'field.bed_id.max': '50'}
        new = flow_edit.from_form(self.flow, form)
        self.assertEqual(new['steps'][1]['instruction'], 'Ask for the bed number first, then the hospital.')
        change = flow_edit.save('log_ticket', new, 'Product owner', 'Bed before hospital', self.dir, expected_version='1.1')
        raw = (self.dir / 'log_ticket.json').read_bytes()
        self.assertNotIn(b'\r', raw)
        saved = json.loads(raw)
        self.assertEqual((saved['version'], saved['fields']['bed_id']['label']), ('1.2', 'Bed or room'))
        self.assertEqual(saved['change_history'][-1]['from'], '1.1')
        self.assertTrue((self.dir / 'archive' / 'log_ticket-v1.1.json').is_file())
        self.assertEqual(list(flows.load_flows(self.dir)), ['log_ticket'])  # the archive folder is not loaded
        labels = [what for what, _, _ in change['changes']]
        self.assertIn('Field Bed: label', labels); self.assertIn('Step "Where is the screen": instruction', labels)
        with self.assertRaises(flow_edit.EditError):  # opened before that save
            flow_edit.save('log_ticket', new, 'Product owner', 'Again', self.dir, expected_version='1.1')

    def test_refusals_write_nothing(self):
        before = (self.dir / 'log_ticket.json').read_bytes()
        bad = [(self.flow, 'Product owner', 'Some note', 'Nothing has changed'),
               (self.changed(lambda f: f.update(title='New')), 'Jane Smith 0412', 'Some note', 'by role'),
               (self.changed(lambda f: f.update(title='New')), 'Product owner', '', 'Describe'),
               (self.changed(lambda f: f.update(id='other')), 'Product owner', 'Some note', 'id cannot be changed'),
               (self.changed(lambda f: f['fields']['callback_number'].update(type='text')), 'Product owner', 'Some note',
                'would not start'),
               (self.changed(lambda f: f.update(steps='nonsense')), 'Product owner', 'Some note', 'would not start')]
        for flow, role, note, message in bad:
            with self.assertRaises(flow_edit.EditError) as caught:
                flow_edit.save('log_ticket', flow, role, note, self.dir)
            self.assertIn(message, str(caught.exception))
        self.assertEqual((self.dir / 'log_ticket.json').read_bytes(), before)
        self.assertFalse((self.dir / 'archive').exists())
        with self.assertRaises(flow_edit.EditError):
            flow_edit.from_form(self.flow, {'field.caller_name.max': 'lots'})


if __name__ == '__main__':
    unittest.main()
