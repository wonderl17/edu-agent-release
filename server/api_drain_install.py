#!/usr/bin/python3
"""Install a reviewed drain-controller update without restarting services.

Only a root-owned, hash-named bundle at the fixed production path is accepted
by the command-line entry point. Alternate roots are for in-process tests.
"""
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

ROOT = Path('/opt/teaching-ai-production')
SCOPE = 'teaching-production-api-drain-update-v1'
RECEIVER = 'operations/release/receiver.py'
FILES = {
    RECEIVER: 'receiver.py',
    'operations/release/api_drain.py': 'api_drain.py',
    'operations/release/codex_configure.py': 'codex_configure.py',
    'operations/release/codex_runtime.py': 'codex_runtime.py',
    'operations/release/api_image_shutdown.py': 'api_image_shutdown.py',
    'operations/release/release_capacity.py': 'release_capacity.py',
    'operations/release/maintenance_gate.py': 'maintenance_gate.py',
    'operations/release/image_retention.py': 'image_retention.py',
}
OPTIONAL_FILES = frozenset('operations/release/' + name for name in
    ('api_drain.py','api_image_shutdown.py','release_capacity.py','maintenance_gate.py','image_retention.py'))
PENDING = 'operations/release/state/api-drain-install.pending.json'
ROLLBACK_READY = 'operations/release/state/api-drain-install.rollback-ready.json'
BLOCKERS = (
    'operations/release/state/codex-host-install.pending.json',
    'operations/release/state/codex-configuration.pending.json',
    'operations/release/state/api-drain.pending.json',
    'operations/release/state/release-operation.pending.json',
    'operations/release/state/release-admission.pending.json',
    'operations/release/state/maintenance-gate.pending.json',
)
OPS = 'operations/startup/production-ops.py'
GATE = (b'#!/usr/bin/python3\nimport json,sys\n'
        b'print(json.dumps({"status":"failed","code":"API_DRAIN_INSTALLATION_RECOVERY_REQUIRED",'
        b'"modelCalls":0,"externalNotifications":0}))\nsys.exit(1)\n')


def require(value, code):
    if not value: raise ValueError(code)


def digest(data): return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'DRAIN_INSTALL_DUPLICATE_KEY')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


def load(path):
    spec = importlib.util.spec_from_file_location('drain_install_' + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


class Installer:
    def __init__(self, bundle, root=ROOT, uid=0, gid=0, lock_dir=Path('/run/lock')):
        self.bundle, self.root, self.uid, self.gid, self.lock_dir = bundle, root, uid, gid, lock_dir
        self.fault = None

    def private(self, path, *, missing=False, limit=524288):
        require(path.resolve() == path, 'DRAIN_INSTALL_SYMLINK_REFUSED')
        parent = path.parent
        while True:
            info = parent.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.uid and
                    not stat.S_IMODE(info.st_mode) & 0o022, 'DRAIN_INSTALL_DIRECTORY_UNSAFE')
            if parent == self.root: break
            require(parent.is_relative_to(self.root), 'DRAIN_INSTALL_PATH_SCOPE')
            parent = parent.parent
        if not path.exists():
            require(missing and not path.is_symlink(), 'DRAIN_INSTALL_FILE_MISSING')
            return None
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_gid == self.gid and
                info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= limit,
                'DRAIN_INSTALL_FILE_UNSAFE')
        return path.read_bytes()

    def read(self, name, *, missing=False, limit=524288):
        require(name in set(FILES) | set(BLOCKERS) | {PENDING, ROLLBACK_READY, OPS}, 'DRAIN_INSTALL_PATH_SCOPE')
        return self.private(self.root / name, missing=missing, limit=limit)

    def write(self, name, data):
        require(name in set(FILES) | {PENDING, ROLLBACK_READY}, 'DRAIN_INSTALL_WRITE_SCOPE')
        path = self.root / name
        self.private(path, missing=True, limit=4194304)
        if data is None:
            path.unlink(missing_ok=True); sync_dir(path.parent)
        else:
            temp = path.with_name(path.name + '.drain-install-' + uuid.uuid4().hex)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, path); sync_dir(path.parent)
            finally: temp.unlink(missing_ok=True)
        if self.fault: self.fault(name, data)

    def manifest(self):
        require(self.bundle.parent == self.root / 'operations/release/drain-update-bundles' and
                re.fullmatch('[0-9a-f]{64}', self.bundle.name), 'DRAIN_INSTALL_BUNDLE_SCOPE')
        raw = self.private(self.bundle / 'manifest.json', limit=20000)
        require(digest(raw) == self.bundle.name, 'DRAIN_INSTALL_MANIFEST_HASH')
        value = decode(raw)
        require(isinstance(value, dict) and set(value) == {'schemaVersion', 'scope', 'root', 'installerSha256', 'files', 'dependencies'} and
                type(value['schemaVersion']) is int and value['schemaVersion'] == 1 and value['scope'] == SCOPE and value['root'] == str(ROOT) and
                set(value['files']) == set(FILES) and isinstance(value['dependencies'], dict) and
                set(value['dependencies']) == {OPS} and
                re.fullmatch('[0-9a-f]{64}', value['dependencies'][OPS]), 'DRAIN_INSTALL_MANIFEST')
        program = self.private(self.bundle / 'install.py')
        require(digest(program) == value['installerSha256'] and program == Path(__file__).read_bytes(),
                'DRAIN_INSTALL_PROGRAM_CHANGED')
        for name, filename in FILES.items():
            row = value['files'][name]
            require(isinstance(row, dict) and set(row) == {'beforeSha256', 'afterSha256', 'payload'} and
                    row['payload'] == 'files/' + filename and
                    ((row['beforeSha256'] is None and name in OPTIONAL_FILES) or
                     isinstance(row['beforeSha256'],str) and re.fullmatch('[0-9a-f]{64}', row['beforeSha256'])) and
                    re.fullmatch('[0-9a-f]{64}', row['afterSha256']), 'DRAIN_INSTALL_FILE_CONTRACT')
            if name == RECEIVER:
                require(row['beforeSha256'] is not None, 'DRAIN_INSTALL_FILE_CONTRACT')
            payload = self.private(self.bundle / row['payload'])
            require(digest(payload) == row['afterSha256'], 'DRAIN_INSTALL_PAYLOAD_CHANGED')
            compile(payload, name, 'exec')
        return value

    def expected(self, manifest):
        require(self.read(PENDING, missing=True, limit=4194304) is None, 'DRAIN_INSTALL_RECOVERY_REQUIRED')
        require(self.read(ROLLBACK_READY, missing=True, limit=4194304) is None, 'DRAIN_INSTALL_RECOVERY_REQUIRED')
        for name in BLOCKERS:
            require(self.read(name, missing=True, limit=4194304) is None, 'DRAIN_INSTALL_CONTROL_PENDING')
        for name, row in manifest['files'].items():
            data = self.read(name, missing=True)
            require((digest(data) if data is not None else None) == row['beforeSha256'], 'DRAIN_INSTALL_STALE_SOURCE')
        self.dependencies(manifest)

    def dependencies(self, manifest):
        require(digest(self.read(OPS)) == manifest['dependencies'][OPS], 'DRAIN_INSTALL_DEPENDENCY_CHANGED')

    @contextlib.contextmanager
    def locks(self):
        handles = []
        try:
            for name in ('teaching-production-release.lock', 'teaching-production-maintenance.lock'):
                path = self.lock_dir / name
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600); handles.append(fd)
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_nlink == 1 and
                        stat.S_IMODE(info.st_mode) == 0o600, 'DRAIN_INSTALL_LOCK_UNSAFE')
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise ValueError('DRAIN_INSTALL_CONTROL_BUSY') from None
            yield
        finally:
            for fd in reversed(handles): os.close(fd)

    def runtime(self):
        self.read(OPS)
        ops = load(self.root / OPS); ops.preflight(); containers = ops.inspect()
        require(ops.snapshot().get('healthy') is True and isinstance(containers, dict) and containers,
                'DRAIN_INSTALL_RUNTIME_UNHEALTHY')
        return {'configSha256': digest(canonical(ops.config())),
                'containers': {name: {'id': item['Id'], 'image': item['Image'], 'startedAt': item['State']['StartedAt']}
                               for name, item in containers.items()}}

    def control_command(self, args, runtime):
        sensitive = (*FILES, OPS)
        if any(str(self.root / name) in value for name in sensitive for value in args): return True
        if not any(Path(value).name in ('docker', 'docker-compose') for value in args[:2]): return False
        containers = runtime['containers']
        identities = {item['id'] for item in containers.values()}
        identities |= {'teaching-production-' + name + '-1' for name in containers}
        if any(value in identities for value in args): return True
        if any(re.fullmatch('[0-9a-f]{12,63}', value) and any(item['id'].startswith(value) for item in containers.values())
               for value in args): return True
        return any((args[i] in ('--project-name', '-p') and args[i + 1] == 'teaching-production') or
                   (args[i] == '--project-directory' and args[i + 1] == str(self.root))
                   for i in range(len(args) - 1))

    def fence(self, runtime):
        deadline = time.monotonic() + 150
        while True:
            active = False
            for path in Path('/proc').glob('[0-9]*/cmdline'):
                if int(path.parent.name) == os.getpid(): continue
                try: args = [x.decode(errors='replace') for x in path.read_bytes().split(b'\0') if x]
                except (FileNotFoundError, ProcessLookupError): continue
                if self.control_command(args, runtime): active = True; break
            if not active: return
            require(time.monotonic() < deadline, 'DRAIN_INSTALL_OLD_CONTROL_BUSY')
            time.sleep(1)

    def journal(self, manifest, name=PENDING):
        require(name in (PENDING, ROLLBACK_READY), 'DRAIN_INSTALL_PATH_SCOPE')
        data = decode(self.read(name, limit=4194304))
        require(isinstance(data, dict) and set(data) == {'schemaVersion', 'bundle', 'operationId', 'phase', 'files', 'runtime'} and
                data['schemaVersion'] == 1 and data['bundle'] == self.bundle.name and
                re.fullmatch('[0-9a-f]{32}', data['operationId']) and
                data['phase'] in ('prepared', 'committed', 'rolling_back') and set(data['files']) == set(FILES) and
                isinstance(data['runtime'], dict) and data['runtime'], 'DRAIN_INSTALL_JOURNAL_INVALID')
        for name, row in data['files'].items():
            require(isinstance(row, dict) and set(row) == {'before'}, 'DRAIN_INSTALL_JOURNAL_INVALID')
            raw = row['before']
            if raw is not None:
                require(isinstance(raw, str) and len(raw) <= 700000, 'DRAIN_INSTALL_JOURNAL_INVALID')
                try: raw = base64.b64decode(raw, validate=True)
                except Exception: raise ValueError('DRAIN_INSTALL_JOURNAL_INVALID') from None
            expected = manifest['files'][name]['beforeSha256']
            require((digest(raw) if raw is not None else None) == expected, 'DRAIN_INSTALL_JOURNAL_INVALID')
        return data

    def known_files(self, manifest):
        for name, row in manifest['files'].items():
            data = self.read(name, missing=True)
            actual = digest(data) if data is not None else None
            choices = {row['beforeSha256'], row['afterSha256']}
            if name == RECEIVER: choices.add(digest(GATE))
            require(actual in choices, 'DRAIN_INSTALL_UNKNOWN_FILE')

    def finish_restore(self, manifest, journal):
        require(self.read(PENDING, missing=True, limit=4194304) is None, 'DRAIN_INSTALL_RECOVERY_REQUIRED')
        self.dependencies(manifest)
        for name in FILES:
            if name == RECEIVER: continue
            actual = self.read(name, missing=True)
            expected = manifest['files'][name]['beforeSha256']
            require((digest(actual) if actual is not None else None) == expected, 'DRAIN_INSTALL_RESTORE_MISMATCH')
        current = self.read(RECEIVER)
        previous = base64.b64decode(journal['files'][RECEIVER]['before'], validate=True)
        require(current in (GATE, previous) and self.runtime() == journal['runtime'], 'DRAIN_INSTALL_RESTORE_MISMATCH')
        if current == GATE: self.write(RECEIVER, previous)
        require(self.read(RECEIVER) == previous, 'DRAIN_INSTALL_RESTORE_MISMATCH')
        self.write(ROLLBACK_READY, None)
        return {'status': 'restored', 'modelCalls': 0, 'servicesRestarted': 0}

    def finish_commit(self, manifest, journal):
        self.dependencies(manifest)
        require(journal['phase'] == 'committed' and
                all(digest(self.read(name)) == row['afterSha256'] for name, row in manifest['files'].items()) and
                self.runtime() == journal['runtime'], 'DRAIN_INSTALL_COMMIT_UNVERIFIED')
        # Persist the commit decision independently before removing the marker
        # recognized by the new receiver. A crash never rolls back a commit.
        self.write(ROLLBACK_READY, canonical(journal))
        self.write(PENDING, None)
        self.write(ROLLBACK_READY, None)
        return {'status': 'installed', 'modelCalls': 0, 'servicesRestarted': 0}

    def restore(self, manifest, journal):
        self.dependencies(manifest)
        self.known_files(manifest)
        journal['phase'] = 'rolling_back'; self.write(PENDING, canonical(journal))
        self.write(RECEIVER, GATE); self.fence(journal['runtime'])
        for name in [x for x in FILES if x != RECEIVER]:
            raw = journal['files'][name]['before']
            self.write(name, None if raw is None else base64.b64decode(raw, validate=True))
        require(self.runtime() == journal['runtime'], 'DRAIN_INSTALL_RUNTIME_CHANGED')
        self.write(ROLLBACK_READY, canonical(journal))
        self.write(PENDING, None)
        return self.finish_restore(manifest, journal)

    def check(self):
        manifest = self.manifest(); self.expected(manifest); self.runtime()
        return {'status': 'verified', 'modelCalls': 0, 'servicesRestarted': 0}

    def apply(self):
        manifest = self.manifest()
        with self.locks():
            self.expected(manifest); runtime = self.runtime()
            journal = {'schemaVersion': 1, 'bundle': self.bundle.name, 'operationId': uuid.uuid4().hex,
                       'phase': 'prepared', 'runtime': runtime, 'files': {}}
            for name in FILES:
                raw = self.read(name, missing=True)
                journal['files'][name] = {'before': None if raw is None else base64.b64encode(raw).decode()}
            # Before touching the old entry point, persist all rollback bytes.
            # If killed here, old files remain consistent. After the gate is
            # installed, this checkpoint recovers even before PENDING exists.
            self.write(ROLLBACK_READY, canonical(journal))
            try:
                self.write(RECEIVER, GATE); self.fence(runtime)
                self.write(PENDING, canonical(journal))
                for name in [x for x in FILES if x != RECEIVER] + [RECEIVER]:
                    self.write(name, self.private(self.bundle / manifest['files'][name]['payload']))
                require(all(digest(self.read(name)) == row['afterSha256'] for name, row in manifest['files'].items()),
                        'DRAIN_INSTALL_WRITE_MISMATCH')
                self.dependencies(manifest)
                require(self.runtime() == runtime, 'DRAIN_INSTALL_RUNTIME_CHANGED')
                journal['phase'] = 'committed'; self.write(PENDING, canonical(journal))
                return self.finish_commit(manifest, journal)
            except Exception:
                pending = self.read(PENDING, missing=True, limit=4194304)
                if pending is not None:
                    stored = self.journal(manifest)
                    if stored['phase'] == 'committed':
                        # Preserve the durable commit for explicit recovery.
                        raise
                    self.restore(manifest, stored)
                else:
                    checkpoint = self.journal(manifest, ROLLBACK_READY)
                    if checkpoint['phase'] == 'committed': raise
                    self.finish_restore(manifest, checkpoint)
                raise

    def recover(self):
        manifest = self.manifest()
        with self.locks():
            for name in BLOCKERS:
                require(self.read(name, missing=True, limit=4194304) is None, 'DRAIN_INSTALL_CONTROL_PENDING')
            if self.read(PENDING, missing=True, limit=4194304) is None:
                if self.read(ROLLBACK_READY, missing=True, limit=4194304) is not None:
                    checkpoint = self.journal(manifest, ROLLBACK_READY)
                    if checkpoint['phase'] == 'committed': return self.finish_commit(manifest, checkpoint)
                    return self.finish_restore(manifest, checkpoint)
                return {'status': 'not_pending', 'modelCalls': 0, 'servicesRestarted': 0}
            journal = self.journal(manifest); self.known_files(manifest)
            if journal['phase'] == 'committed':
                return self.finish_commit(manifest, journal)
            return self.restore(manifest, journal)


def main():
    require(os.geteuid() == 0 and len(sys.argv) == 2 and sys.argv[1] in ('check', 'apply', 'recover'),
            'DRAIN_INSTALL_OPERATOR_SCOPE')
    installer = Installer(Path(__file__).resolve().parent)
    result = getattr(installer, sys.argv[1])()
    print(json.dumps(result)); return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as error:
        code = str(error) if re.fullmatch('[A-Z][A-Z0-9_]{1,95}', str(error)) else 'DRAIN_INSTALL_FAILED'
        print(json.dumps({'status': 'failed', 'code': code, 'modelCalls': 0, 'servicesRestarted': 0}))
        raise SystemExit(1)
