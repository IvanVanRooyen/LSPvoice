"""Staff-pilot call records: reference codes, version stamps and problem reports.

Every call gets a 4-digit reference, spoken at the end of the call, so a staff report can be
matched to its transcript. Each transcript starts with a version stamp (code commit, model
settings, prompt and knowledge fingerprints) so we know which version handled the call.
Problem reports made by voice (report_problem tool) go to private-config/feedback.jsonl.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mock-backend'))
from private_files import private_path, restrict

import claude_reply

REPO = Path(__file__).resolve().parents[1]
FEEDBACK_LOG = private_path('feedback.jsonl')


def _git_version():
    """Short commit of the running code, with '+changes' if tracked files differ from it."""
    try:
        commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=REPO, capture_output=True,
                                text=True, timeout=5, check=True).stdout.strip()
        changed = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=REPO,
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        return commit + ('+changes' if changed else '')
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


# Taken when the server starts, i.e. the code actually loaded. A container image has no git
# history: its build passes the commit in LSPCARE_VERSION instead.
CODE_VERSION = os.environ.get('LSPCARE_VERSION', '').strip() or _git_version()


def _fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]


def new_reference(in_use):
    while True:
        reference = f'{secrets.randbelow(10000):04d}'
        if reference not in in_use:
            return reference


def spoken(reference):
    return ' '.join(str(reference))


def caller_tail(number):
    digits = re.sub(r'\D', '', str(number or ''))
    return digits[-3:] if len(digits) >= 3 else ''


def version_stamp(cfg, system_text, knowledge_text):
    if cfg.get('provider') == 'anthropic':
        options = claude_reply.request_options(cfg)
        model, effort = options['model'], options['output_config']['effort']
        thinking = options.get('thinking', {}).get('type', 'adaptive')
    else:
        model, effort, thinking = cfg.get('model'), None, None
    return {'code': CODE_VERSION, 'model': model, 'effort': effort, 'thinking': thinking,
            'voice': cfg.get('voice'), 'prompt': _fingerprint(system_text), 'knowledge': _fingerprint(knowledge_text)}


def record_report(session, description, sid):
    """report_problem tool: save a staff tester's spoken problem report and flag the call."""
    reference = session.get('reference', '----')
    session['flagged'] = True
    record = {'at': datetime.now().isoformat(timespec='seconds'), 'reference': reference,
              'caller': session.get('caller', ''), 'call': str(sid)[-6:], 'description': str(description)[:1000]}
    FEEDBACK_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    new = not FEEDBACK_LOG.exists()
    with FEEDBACK_LOG.open('a', encoding='utf-8') as handle:
        if new:
            restrict(FEEDBACK_LOG)
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    return {'recorded': True, 'call_reference': spoken(reference)}


def read_reports(path=None):
    path = Path(path or FEEDBACK_LOG)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
