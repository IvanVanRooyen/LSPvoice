"""Staged knowledge delivery for the pilot, built only from reviewed registry entries.

Each allowed source has one delivery mode (DELIVERY below):
  core   - included in every request (short, always-relevant rules: safety, buying, prices).
  guide  - fetched whole by exact ID with the get_guide tool, after the caller's hardware
           or situation is identified. The menu shown to the model is the entry's reviewed
           registry description, so the routing text is reviewed content too.
  search - split into sections and searched with the search_knowledge tool (SQLite FTS5,
           built in memory). For large reference material such as manuals and FAQs.

Every source is re-checked against its registry hash before use; a changed or unreviewed
file stops calls instead of being served. Sources not listed in DELIVERY are never loaded
(e.g. KB-STAFF-CONTACTS stays excluded until staff/patient routing is separated).
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / 'knowledge'

# To add a document: review it into knowledge/registry.json, then add its ID here.
DELIVERY = {
    'KB-PURCHASE': 'core',
    'KB-PRICES': 'core',
    'KB-UNKNOWN-HARDWARE': 'core',
    'KB-ARM-FAULTS': 'core',
    'KB-TERMINAL-LSPC15MF': 'guide',
    'KB-TERMINAL-TOUCH-KEYS': 'guide',
}
CORE_LIMIT = 40000      # characters sent on every request; above this, move sources to guide/search
GUIDE_LIMIT = 30000     # one guide returned by get_guide
SECTION_LIMIT = 4000    # one search result section
SEARCH_RESULTS = 3


class KnowledgeError(ValueError):
    pass


def _sections(text):
    """Split markdown at level-2 headings; the title + intro is the first section."""
    parts = re.split(r'(?m)^(?=## )', text)
    title = text.splitlines()[0].lstrip('# ').strip() if text.strip() else ''
    for part in parts:
        if not part.strip():
            continue
        first = part.splitlines()[0]
        heading = first[3:].strip() if first.startswith('## ') else title
        yield heading, part.strip()[:SECTION_LIMIT]


def _fts_query(query):
    words = re.findall(r'[\w-]+', str(query or '').casefold())[:12]
    return ' OR '.join('"' + w.replace('"', '') + '"' for w in words if len(w) > 1)


class Library:
    def __init__(self, library=LIBRARY, delivery=None):
        self.library = Path(library)
        self.delivery = dict(DELIVERY if delivery is None else delivery)
        self.lock = Lock()
        self.stamp = None

    def _stamp(self, entries):
        paths = [self.library / 'registry.json'] + [self.library / e['path'] for e in entries]
        return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)

    def _load(self):
        registry = json.loads((self.library / 'registry.json').read_text(encoding='utf-8'))
        entries = [e for e in registry['entries'] if e['id'] in self.delivery]
        stamp = self._stamp(entries)
        if stamp == self.stamp:
            return
        if {e['id'] for e in entries} != set(self.delivery) or len(entries) != len(self.delivery):
            raise KnowledgeError('A pilot source is missing from, or duplicated in, the registry')
        core, guides, sections = [], {}, []
        for item in entries:
            path = (self.library / item['path']).resolve()
            if (not path.is_relative_to(self.library.resolve()) or not item.get('export')
                    or item['readiness'] != 'development_ready'
                    or hashlib.sha256(path.read_bytes()).hexdigest() != item['content_sha256']):
                raise KnowledgeError('Knowledge source not reviewed or changed: ' + item['id'])
            text = path.read_text(encoding='utf-8')
            mode = self.delivery[item['id']]
            if mode == 'core':
                core.append('SOURCE ' + item['id'] + ' v' + item['version'] + '\n' + text)
            elif mode == 'guide':
                if len(text) > GUIDE_LIMIT:
                    raise KnowledgeError('Guide too long; split it or deliver it by search: ' + item['id'])
                guides[item['id']] = {'version': item['version'], 'use_when': item['description'], 'text': text}
            elif mode == 'search':
                sections += [(item['id'], item['version'], h, s) for h, s in _sections(text)]
            else:
                raise KnowledgeError('Unknown delivery mode for ' + item['id'])
        core_text = '\n\n'.join(core)
        if len(core_text) > CORE_LIMIT:
            raise KnowledgeError('Core knowledge exceeds the per-request limit; move sources to guide/search')
        index = None
        if sections:
            index = sqlite3.connect(':memory:', check_same_thread=False)
            index.execute('CREATE VIRTUAL TABLE s USING fts5(source, version UNINDEXED, heading, body)')
            index.executemany('INSERT INTO s VALUES (?, ?, ?, ?)', sections)
        self.core_text, self.guides, self.index, self.stamp = core_text, guides, index, stamp

    def check(self):
        with self.lock:
            self._load()

    def system_text(self):
        """Core sources plus the guide menu: the knowledge part of every request."""
        with self.lock:
            self._load()
            parts = [self.core_text]
            if self.guides:
                parts.append('GUIDES AVAILABLE THROUGH get_guide (fetch before using their steps):\n' + '\n'.join(
                    f'- {gid}: {g["use_when"]}' for gid, g in sorted(self.guides.items())))
            if self.index is not None:
                parts.append('Further reviewed reference material is available through search_knowledge.')
            return '\n\n'.join(parts)

    def guide_ids(self):
        with self.lock:
            self._load()
            return sorted(self.guides)

    def has_search(self):
        with self.lock:
            self._load()
            return self.index is not None

    def get_guide(self, guide_id):
        with self.lock:
            self._load()
            guide = self.guides.get(str(guide_id))
            if guide is None:
                return {'found': False, 'available_guides': sorted(self.guides)}
            return {'found': True, 'guide_id': guide_id, 'version': guide['version'], 'text': guide['text']}

    def search(self, query):
        with self.lock:
            self._load()
            match = _fts_query(query)
            if self.index is None or not match:
                return {'results': []}
            rows = self.index.execute(
                'SELECT source, version, heading, body FROM s WHERE s MATCH ? ORDER BY rank LIMIT ?',
                (match, SEARCH_RESULTS)).fetchall()
            return {'results': [{'source_id': s, 'version': v, 'section': h, 'text': b} for s, v, h, b in rows]}
