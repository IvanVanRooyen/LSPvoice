"""Offline tests for knowledge edits: checked on a copy first, archived, registered, never left broken."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import knowledge
import knowledge_edit


class KnowledgeEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.lib = Path(self.tmp.name) / 'knowledge'
        shutil.copytree(knowledge.LIBRARY, self.lib, ignore=shutil.ignore_patterns('archive'))
        self.item = lambda sid: knowledge_edit.entry(knowledge_edit.load_registry(self.lib), sid)
        self.path = lambda sid: self.lib / self.item(sid)['path']

    def edit(self, sid, text, role='Product owner', note='Clarified the wording', description='', **kw):
        return knowledge_edit.save(sid, text, description, role, note, self.lib, **kw)

    def test_bump_and_normalise(self):
        self.assertEqual(knowledge_edit.bump('1.1'), '1.2'); self.assertEqual(knowledge_edit.bump('2.9'), '2.10')
        self.assertEqual(knowledge_edit.bump('draft'), 'draft.1')
        self.assertEqual(knowledge_edit.normalise('a\r\nb\r\n\n\n'), 'a\nb\n')

    def test_save_archives_registers_and_stays_loadable(self):
        sid = 'KB-PRICES'
        before, old_text = self.item(sid), self.path(sid).read_text(encoding='utf-8')
        new_text = old_text.replace('\n', '\r\n') + '\r\nExtra line for the test.\r\n'
        change = self.edit(sid, new_text)
        after = self.item(sid)
        saved = self.path(sid).read_bytes()
        self.assertNotIn(b'\r', saved)
        self.assertEqual(after['content_sha256'], hashlib.sha256(saved).hexdigest())
        self.assertEqual(after['version'], knowledge_edit.bump(before['version']))
        self.assertTrue(after['approval'].startswith('Product owner '))
        self.assertEqual(after['change_history'][-1]['from'], before['version'])
        self.assertEqual(change['archive'].read_text(encoding='utf-8'), old_text)
        self.assertIn('Extra line for the test.', knowledge.Library(self.lib).system_text())
        # Other entries and the registry's layout are untouched apart from this entry and 'updated'.
        registry = knowledge_edit.load_registry(self.lib)
        self.assertEqual([e['id'] for e in registry['entries']],
                         [e['id'] for e in json.loads((knowledge.LIBRARY / 'registry.json').read_text(encoding='utf-8'))['entries']])

    def test_guide_description_can_change_alone(self):
        sid = 'KB-TERMINAL-LSPC15MF'
        text = self.path(sid).read_text(encoding='utf-8')
        self.edit(sid, text, description='Use only when the caller confirms an orange button below the screen.')
        self.assertIn('orange button below the screen', knowledge.Library(self.lib).system_text())

    def test_refusals_write_nothing(self):
        sid = 'KB-PRICES'
        registry_before = (self.lib / 'registry.json').read_bytes()
        text = self.path(sid).read_text(encoding='utf-8')
        cases = [(text, 'Product owner', 'Some note', 'Nothing has changed'),
                 (text + 'x\n', 'Jane Smith 0412 345 678', 'Some note', 'by role'),
                 (text + 'x\n', 'Product owner', '', 'Describe what changed'),
                 (text + ('Long filler line for the core limit. ' * 1200), 'Product owner', 'Too long', 'would refuse calls'),
                 ('x' * (knowledge_edit.MAX_DOCUMENT + 1), 'Product owner', 'Too long', 'too long')]
        for new_text, role, note, message in cases:
            with self.assertRaises(knowledge_edit.EditError) as caught:
                self.edit(sid, new_text, role=role, note=note)
            self.assertIn(message, str(caught.exception))
        self.assertEqual((self.lib / 'registry.json').read_bytes(), registry_before)
        self.assertEqual(self.path(sid).read_text(encoding='utf-8'), text)
        self.assertFalse((self.lib / 'content' / 'archive').exists())

    def test_stale_version_is_refused(self):
        sid = 'KB-PRICES'
        text = self.path(sid).read_text(encoding='utf-8')
        with self.assertRaises(knowledge_edit.EditError):
            self.edit(sid, text + 'x\n', expected_version='0.1')

    def test_unknown_and_outside_paths(self):
        with self.assertRaises(knowledge_edit.EditError):
            self.edit('KB-NOPE', 'x')
        registry = knowledge_edit.load_registry(self.lib)
        registry['entries'][0]['path'] = '../voice-agent/app.py'
        with self.assertRaises(knowledge_edit.EditError):
            knowledge_edit.content_path(self.lib, registry['entries'][0])


if __name__ == '__main__':
    unittest.main()
