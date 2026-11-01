#!/usr/bin/python3
"""Receive one exact reviewed updater bundle over private standard input.

This does not execute or install bundle code and never accepts archive paths.
The command-line entry point is fixed to the Production root and lock paths.
"""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import uuid

ROOT = Path('/opt/teaching-ai-production')
SCOPE = 'teaching-production-api-drain-update-v1'
RECEIVER = 'operations/release/receiver.py'
FILES = {RECEIVER: 'receiver.py', 'operations/release/api_drain.py': 'api_drain.py',
         'operations/release/codex_configure.py': 'codex_configure.py'}
OPS = 'operations/startup/production-ops.py'
BLOCKERS = ('operations/release/state/codex-host-install.pending.json',
            'operations/release/state/codex-configuration.pending.json',
            'operations/release/state/api-drain.pending.json',
            'operations/release/state/release-operation.pending.json',
            'operations/release/state/api-drain-install.pending.json',
            'operations/release/state/api-drain-install.rollback-ready.json')
MEMBERS = {'manifest.json', 'install.py'} | {'files/' + value for value in FILES.values()}
LIMIT = 4 * 1024 * 1024


def check(value, code):
    if not value: raise ValueError(code)


def digest(value): return hashlib.sha256(value).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, 'DRAIN_RECEIVE_DUPLICATE_KEY'); result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


class Receiver:
    # Alternate roots/owners are for in-process isolated tests only.
    def __init__(self, root=ROOT, uid=0, gid=0, lock_dir=Path('/run/lock')):
        self.root, self.uid, self.gid, self.lock_dir = root, uid, gid, lock_dir
        self.parent = root / 'operations/release/drain-update-bundles'

    def directory(self, path, mode=None):
        check(path.resolve() == path and not path.is_symlink(), 'DRAIN_RECEIVE_PATH_UNSAFE')
        info = path.lstat()
        check(stat.S_ISDIR(info.st_mode) and info.st_uid == self.uid and info.st_gid == self.gid and
              not stat.S_IMODE(info.st_mode) & 0o022 and
              (mode is None or stat.S_IMODE(info.st_mode) == mode), 'DRAIN_RECEIVE_DIRECTORY_UNSAFE')

    def private(self, path, missing=False):
        check(path.resolve() == path and not path.is_symlink(), 'DRAIN_RECEIVE_PATH_UNSAFE')
        if not path.exists():
            check(missing, 'DRAIN_RECEIVE_SOURCE_MISSING'); return None
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_gid == self.gid and
              info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 524288,
              'DRAIN_RECEIVE_FILE_UNSAFE')
        return path.read_bytes()

    @contextlib.contextmanager
    def locks(self):
        handles = []
        try:
            for name in ('teaching-production-release.lock', 'teaching-production-maintenance.lock'):
                fd = os.open(self.lock_dir / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600); handles.append(fd)
                info = os.fstat(fd)
                check(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_gid == self.gid and info.st_nlink == 1 and
                      stat.S_IMODE(info.st_mode) == 0o600, 'DRAIN_RECEIVE_LOCK_UNSAFE')
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise ValueError('DRAIN_RECEIVE_CONTROL_BUSY') from None
            yield
        finally:
            for fd in reversed(handles): os.close(fd)

    def unpack(self, raw, expected):
        check(len(raw) <= LIMIT and re.fullmatch('[0-9a-f]{64}', expected), 'DRAIN_RECEIVE_INPUT_REFUSED')
        value = decode(raw)
        check(isinstance(value, dict) and set(value) == {'schemaVersion', 'files'} and
              type(value['schemaVersion']) is int and value['schemaVersion'] == 1 and isinstance(value['files'], dict) and
              set(value['files']) == MEMBERS, 'DRAIN_RECEIVE_MEMBERS_REFUSED')
        files = {}
        for name, encoded in value['files'].items():
            check(isinstance(encoded, str) and len(encoded) <= 700000, 'DRAIN_RECEIVE_SIZE_REFUSED')
            try: data = base64.b64decode(encoded, validate=True)
            except Exception: raise ValueError('DRAIN_RECEIVE_ENCODING_REFUSED') from None
            check(len(data) <= 524288, 'DRAIN_RECEIVE_SIZE_REFUSED'); files[name] = data
        check(digest(files['manifest.json']) == expected, 'DRAIN_RECEIVE_MANIFEST_MISMATCH')
        manifest = decode(files['manifest.json'])
        check(isinstance(manifest, dict) and set(manifest) == {'schemaVersion', 'scope', 'root', 'installerSha256', 'files', 'dependencies'} and
              type(manifest['schemaVersion']) is int and manifest['schemaVersion'] == 1 and manifest['scope'] == SCOPE and manifest['root'] == str(ROOT) and
              isinstance(manifest['files'], dict) and set(manifest['files']) == set(FILES) and
              isinstance(manifest['dependencies'], dict) and set(manifest['dependencies']) == {OPS} and
              re.fullmatch('[0-9a-f]{64}', manifest['dependencies'][OPS]) and
              digest(files['install.py']) == manifest['installerSha256'], 'DRAIN_RECEIVE_MANIFEST_REFUSED')
        compile(files['install.py'], 'install.py', 'exec')
        for name, filename in FILES.items():
            row = manifest['files'][name]
            check(isinstance(row, dict) and set(row) == {'beforeSha256', 'afterSha256', 'payload'} and
                  row['payload'] == 'files/' + filename and
                  ((row['beforeSha256'] is None and name.endswith('/api_drain.py')) or
                   isinstance(row['beforeSha256'], str) and re.fullmatch('[0-9a-f]{64}', row['beforeSha256'])) and
                  digest(files[row['payload']]) == row['afterSha256'], 'DRAIN_RECEIVE_MANIFEST_REFUSED')
            compile(files[row['payload']], filename, 'exec')
        return manifest, files

    def expected(self, manifest):
        for path in (self.root, self.root / 'operations', self.root / 'operations/release',
                     self.root / 'operations/release/state', self.root / 'operations/startup'):
            self.directory(path)
        for name in BLOCKERS:
            path = self.root / name
            check(not (path.exists() or path.is_symlink()), 'DRAIN_RECEIVE_CONTROL_PENDING')
        for name, row in manifest['files'].items():
            raw = self.private(self.root / name, missing=row['beforeSha256'] is None)
            check((digest(raw) if raw is not None else None) == row['beforeSha256'], 'DRAIN_RECEIVE_STALE_SOURCE')
        check(digest(self.private(self.root / OPS)) == manifest['dependencies'][OPS], 'DRAIN_RECEIVE_DEPENDENCY_CHANGED')

    def verify_existing(self, bundle, files):
        self.directory(bundle, 0o700); self.directory(bundle / 'files', 0o700)
        actual = {str(path.relative_to(bundle)) for path in bundle.iterdir() if path.name != 'files'}
        actual |= {'files/' + path.name for path in (bundle / 'files').iterdir()}
        check(actual == MEMBERS and all(self.private(bundle / name) == data for name, data in files.items()),
              'DRAIN_RECEIVE_EXISTING_MISMATCH')

    def receive(self, raw, expected):
        manifest, files = self.unpack(raw, expected)
        with self.locks():
            self.expected(manifest)
            if not self.parent.exists(): self.parent.mkdir(mode=0o700); sync_dir(self.parent.parent)
            self.directory(self.parent, 0o700)
            target = self.parent / expected
            if target.exists() or target.is_symlink():
                self.verify_existing(target, files)
                return {'status': 'present', 'bundle': expected, 'installed': False}
            temp = self.parent / ('.incoming-' + uuid.uuid4().hex)
            temp.mkdir(mode=0o700)
            try:
                (temp / 'files').mkdir(mode=0o700)
                for name, data in files.items():
                    fd = os.open(temp / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data); stream.flush(); os.fsync(stream.fileno())
                sync_dir(temp / 'files'); sync_dir(temp)
                check(not (target.exists() or target.is_symlink()), 'DRAIN_RECEIVE_TARGET_EXISTS')
                os.rename(temp, target); sync_dir(self.parent)
                self.verify_existing(target, files)
            finally:
                if temp.exists(): shutil.rmtree(temp)
        return {'status': 'received', 'bundle': expected, 'installed': False}


def main():
    check(os.geteuid() == 0 and len(sys.argv) == 2, 'DRAIN_RECEIVE_OPERATOR_SCOPE')
    def timeout(_signal, _frame): raise TimeoutError('DRAIN_RECEIVE_TIMEOUT')
    signal.signal(signal.SIGALRM, timeout); signal.alarm(120)
    raw = sys.stdin.buffer.read(LIMIT + 1)
    result = Receiver().receive(raw, sys.argv[1]); signal.alarm(0)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        code = str(error) if re.fullmatch('DRAIN_RECEIVE_[A-Z_]+', str(error)) else 'DRAIN_RECEIVE_FAILED'
        print(json.dumps({'status': 'failed', 'code': code, 'installed': False})); sys.exit(1)
