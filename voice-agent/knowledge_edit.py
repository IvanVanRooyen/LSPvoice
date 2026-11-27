"""Safe edits to reviewed knowledge documents (used by the dashboard).

Follows docs/knowledge.md: the new text is saved in knowledge/content/, the previous version is
kept in knowledge/content/archive/, and the registry entry gets a bumped version, the new sha256
fingerprint, the review date and the approval. The whole change is first applied to a scratch
copy of the library and checked exactly as the agent checks it (fingerprints, readiness, size
limits), so a change that would make calls fail is refused before anything is written.

The running agent re-reads the library when files change, so a saved edit is used from its next
reply. The content file and the registry are each replaced atomically, one straight after the
other.
"""
from datetime import date
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from knowledge import CORE_LIMIT, DELIVERY, GUIDE_LIMIT, LIBRARY, KnowledgeError, Library

MAX_DOCUMENT = 60000
ROLE = re.compile(r'[A-Za-z][A-Za-z /&().,-]{1,79}')


class EditError(ValueError):
    pass


def normalise(text):
    """LF line endings and one final newline, as the fingerprints expect."""
    text = str(text).replace('\r\n', '\n').replace('\r', '\n')
    return text.rstrip('\n') + '\n'


def bump(version):
    """'1.1' -> '1.2'; anything else gets '.1' added."""
    head, _, last = str(version).rpartition('.')
    return f'{head}.{int(last) + 1}' if head and last.isdigit() else f'{version}.1'


def load_registry(library=LIBRARY):
    return json.loads((Path(library) / 'registry.json').read_text(encoding='utf-8'))


def entry(registry, source_id):
    found = next((e for e in registry['entries'] if e['id'] == source_id), None)
    if found is None:
        raise EditError('Unknown knowledge source.')
    return found


def content_path(library, item):
    path = (Path(library) / item['path']).resolve()
    if not path.is_relative_to((Path(library) / 'content').resolve()) or path.suffix != '.md':
        raise EditError('Only Markdown documents in knowledge/content can be edited here.')
    return path


def diff_lines(old, new, name='document'):
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), f'{name} (current)', f'{name} (edited)', lineterm='', n=2))


def plan(source_id, text, description, role, note, library=LIBRARY, delivery=None, today=None):
    """Work out an edit and check it on a scratch copy. Returns what would change; writes nothing real."""
    library = Path(library)
    registry = load_registry(library)
    item = entry(registry, source_id)
    path = content_path(library, item)
    old_text = path.read_text(encoding='utf-8')
    new_text = normalise(text)
    new_description = ' '.join(str(description).split()) or item.get('description', '')
    if len(new_text) > MAX_DOCUMENT:
        raise EditError(f'The document is too long for one source ({len(new_text):,} characters); split it.')
    if new_text == old_text and new_description == item.get('description', ''):
        raise EditError('Nothing has changed.')
    if not ROLE.fullmatch(str(role).strip()):
        raise EditError('Say who approved the change by role (for example "Product owner"), not by name.')
    if len(str(note).strip()) < 5:
        raise EditError('Describe what changed and why (a short note).')
    today = (today or date.today()).isoformat()
    version = bump(item['version'])
    updated = dict(item)
    updated.update(version=version, content_sha256=hashlib.sha256(new_text.encode('utf-8')).hexdigest(),
                   description=new_description, reviewed_date=today,
                   approval=f'{str(role).strip()} {today}: {" ".join(str(note).split())}')
    updated['change_history'] = list(item.get('change_history', [])) + [
        {'version': version, 'from': item['version'], 'date': today, 'approved_by': str(role).strip(),
         'note': ' '.join(str(note).split())}]
    new_registry = dict(registry)
    new_registry['entries'] = [updated if e['id'] == source_id else e for e in registry['entries']]
    new_registry['updated'] = today
    archive = path.parent / 'archive' / f'{path.stem}-v{item["version"]}.md'

    # Check on a scratch copy, exactly as the agent will load it.
    delivery = dict(DELIVERY if delivery is None else delivery)
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / 'knowledge'
        shutil.copytree(library, copy, ignore=shutil.ignore_patterns('archive'))
        (copy / item['path']).write_bytes(new_text.encode('utf-8'))
        (copy / 'registry.json').write_text(json.dumps(new_registry, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        mode = delivery.get(source_id)
        try:
            before = len(Library(library, delivery).system_text()) if mode else None
            after = len(Library(copy, delivery).system_text()) if mode else None
        except KnowledgeError as error:
            raise EditError(f'Not saved: the agent would refuse calls with this change ({error}).')
    return {'id': source_id, 'path': path, 'archive': archive, 'old_text': old_text, 'new_text': new_text,
            'old_version': item['version'], 'version': version, 'registry': new_registry, 'mode': mode,
            'description_changed': new_description != item.get('description', ''), 'old_description': item.get('description', ''),
            'description': new_description, 'diff': diff_lines(old_text, new_text, Path(item['path']).name),
            'knowledge_before': before, 'knowledge_after': after,
            'limit': CORE_LIMIT if mode == 'core' else GUIDE_LIMIT if mode == 'guide' else None}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.new')
    temp.write_bytes(data)
    return temp


def save(source_id, text, description, role, note, library=LIBRARY, delivery=None, expected_version=None):
    """Apply an edit after the same checks as plan(). expected_version guards against saving over a newer edit."""
    library = Path(library)
    if expected_version is not None and entry(load_registry(library), source_id)['version'] != expected_version:
        raise EditError('This document was changed since you opened it; reload and edit again.')
    change = plan(source_id, text, description, role, note, library, delivery)
    old_registry = (library / 'registry.json').read_bytes()
    archive = change['archive']
    if not archive.exists():
        _write(archive, change['old_text'].encode('utf-8')).replace(archive)
    content = _write(change['path'], change['new_text'].encode('utf-8'))
    registry = _write(library / 'registry.json', (json.dumps(change['registry'], indent=2, ensure_ascii=False) + '\n').encode('utf-8'))
    os.replace(content, change['path'])
    os.replace(registry, library / 'registry.json')
    try:
        Library(library, delivery).check()
    except KnowledgeError:  # should not happen after plan(); put the previous version of both back
        os.replace(_write(change['path'], change['old_text'].encode('utf-8')), change['path'])
        os.replace(_write(library / 'registry.json', old_registry), library / 'registry.json')
        raise
    return change
