"""Where private files live (outside Git) and how they are kept owner-only.

The private folder defaults to "private-config" next to the repository folder
(e.g. ...\\Twilio AI\\private-config when the repo is ...\\Twilio AI\\<repo>).
Set LSPCARE_PRIVATE_DIR to use a different folder.

POSIX uses mode 0600/0700. Windows ignores those modes, so the same protection
is applied and checked with an owner-only ACL through icacls.
"""
from functools import cache
import os
from pathlib import Path
import subprocess

AGENT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIR = Path(os.environ.get('LSPCARE_PRIVATE_DIR') or AGENT_ROOT.parent / 'private-config')
WINDOWS = os.name == 'nt'
# Root-equivalent principals, plus OWNER RIGHTS (the owner), which Python 3.13+
# itself adds when creating a mode-0700 folder on Windows.
_WINDOWS_TRUSTED = {'nt authority\\system', 'builtin\\administrators', 'owner rights'}


def private_path(name):
    return PRIVATE_DIR / name


@cache
def _windows_user():
    return subprocess.run(['whoami'], capture_output=True, text=True, check=True).stdout.strip().casefold()


def restrict(path):
    """Make a file (0600) or folder (0700) readable by the current user only."""
    path = Path(path)
    if not WINDOWS:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    grant = _windows_user() + (':(OI)(CI)F' if path.is_dir() else ':F')
    subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', grant],
                   capture_output=True, check=True)


def make_private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Refusing a symlink for a private folder')
    restrict(path)


def is_private(path):
    """True if no one but the owner (and system/administrators on Windows) has access."""
    path = Path(path)
    if path.is_symlink():
        return False
    if not WINDOWS:
        return not path.stat().st_mode & 0o077
    listing = subprocess.run(['icacls', str(path)], capture_output=True, text=True,
                             errors='replace', check=True).stdout.replace(str(path), '', 1)
    principals = {line.strip().split(':(', 1)[0].casefold()
                  for line in listing.splitlines() if ':(' in line}
    return bool(principals) and principals <= _WINDOWS_TRUSTED | {_windows_user()}
