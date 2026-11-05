#!/usr/bin/python3
"""Read-only, non-secret host file hash audit for the drain updater bundle."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path('/opt/teaching-ai-production')
TARGETS = ('operations/release/receiver.py', 'operations/release/api_drain.py',
           'operations/release/codex_configure.py', 'operations/release/api_image_shutdown.py',
           'operations/release/release_capacity.py','operations/release/maintenance_gate.py','operations/release/image_retention.py')
OPTIONAL_FILES = frozenset('operations/release/' + name for name in
    ('api_drain.py','api_image_shutdown.py','release_capacity.py','maintenance_gate.py','image_retention.py'))
DEPENDENCIES = ('operations/startup/production-ops.py',)
BLOCKERS = ('operations/release/state/codex-host-install.pending.json',
            'operations/release/state/codex-configuration.pending.json',
            'operations/release/state/api-drain.pending.json',
            'operations/release/state/release-operation.pending.json',
            'operations/release/state/release-admission.pending.json',
            'operations/release/state/maintenance-gate.pending.json',
            'operations/release/state/api-drain-install.pending.json',
            'operations/release/state/api-drain-install.rollback-ready.json')


def private_hash(name, missing=False):
    path = ROOT / name
    if path.resolve() != path: raise ValueError('DRAIN_AUDIT_PATH_UNSAFE')
    parent = path.parent
    while True:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('DRAIN_AUDIT_DIRECTORY_UNSAFE')
        if parent == ROOT: break
        if not parent.is_relative_to(ROOT): raise ValueError('DRAIN_AUDIT_PATH_UNSAFE')
        parent = parent.parent
    if not path.exists():
        if missing and not path.is_symlink(): return None
        raise ValueError('DRAIN_AUDIT_FILE_MISSING')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 524288):
        raise ValueError('DRAIN_AUDIT_FILE_UNSAFE')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1 or ROOT.resolve() != ROOT:
        raise ValueError('DRAIN_AUDIT_SCOPE_REFUSED')
    targets = {name: private_hash(name, missing=name in OPTIONAL_FILES) for name in TARGETS}
    deps = {name: private_hash(name) for name in DEPENDENCIES}
    blockers = {name: (ROOT / name).exists() or (ROOT / name).is_symlink() for name in BLOCKERS}
    print(json.dumps({'schemaVersion': 1, 'scope': 'teaching-production-api-drain-audit-v1',
                      'root': str(ROOT), 'kernel': os.uname().release, 'targets': targets,
                      'dependencies': deps, 'blockersPresent': blockers}, sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        code = str(error) if str(error).startswith('DRAIN_AUDIT_') else 'DRAIN_AUDIT_FAILED'
        print(json.dumps({'status': 'failed', 'code': code})); sys.exit(1)
