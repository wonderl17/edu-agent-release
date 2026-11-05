#!/usr/bin/env python3
"""Build a private, immutable updater bundle from a read-only host audit.

The reviewed source hash file is required so changes after review cannot enter
the bundle. This only writes a local output directory; it does not install.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reviewed_drain_installer', REPO / 'server/api_drain_install.py')
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
SOURCES = {
    'installer': REPO / 'server/api_drain_install.py',
    'receiver.py': REPO / 'server/tencent_receiver.py',
    'api_drain.py': REPO / 'server/api_drain.py',
    'codex_configure.py': REPO / 'server/codex_configure.py',
    'api_image_shutdown.py': REPO / 'server/api_image_shutdown.py',
    'release_capacity.py': REPO / 'server/release_capacity.py',
    'maintenance_gate.py': REPO / 'server/maintenance_gate.py',
    'image_retention.py': REPO / 'server/image_retention.py',
}


def check(value, code):
    if not value: raise ValueError(code)


def private_output(path):
    check(path.is_absolute() and path.resolve() == path and path.is_dir() and not path.is_symlink(),
          'DRAIN_BUNDLE_OUTPUT_UNSAFE')
    info = path.stat()
    check(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'DRAIN_BUNDLE_OUTPUT_UNSAFE')


def write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def build(audit, reviewed, output):
    private_output(output)
    audit = json.loads(audit.read_bytes()); reviewed = json.loads(reviewed.read_bytes())
    check(isinstance(audit, dict) and set(audit) == {'schemaVersion', 'scope', 'root', 'kernel', 'targets',
          'dependencies', 'blockersPresent'} and type(audit['schemaVersion']) is int and audit['schemaVersion'] == 1 and
          audit['scope'] == 'teaching-production-api-drain-audit-v1' and audit['root'] == str(installer.ROOT) and
          audit['kernel'] == '6.8.0-124-generic' and set(audit['targets']) == set(installer.FILES) and
          set(audit['dependencies']) == {installer.OPS} and
          set(audit['blockersPresent']) == set(installer.BLOCKERS) | {installer.PENDING, installer.ROLLBACK_READY} and
          all(type(value) is bool for value in audit['blockersPresent'].values()) and
          not any(audit['blockersPresent'].values()), 'DRAIN_BUNDLE_AUDIT_REFUSED')
    check(isinstance(reviewed, dict) and set(reviewed) == set(SOURCES) and
          all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) for value in reviewed.values()),
          'DRAIN_BUNDLE_REVIEW_REFUSED')
    data = {name: path.read_bytes() for name, path in SOURCES.items()}
    check(all(hashlib.sha256(raw).hexdigest() == reviewed[name] for name, raw in data.items()),
          'DRAIN_BUNDLE_SOURCE_CHANGED')
    for name, raw in data.items(): compile(raw, name, 'exec')
    rows = {}
    for target, filename in installer.FILES.items():
        before = audit['targets'][target]
        check((before is None and target in installer.OPTIONAL_FILES) or
              isinstance(before, str) and re.fullmatch('[0-9a-f]{64}', before), 'DRAIN_BUNDLE_AUDIT_REFUSED')
        rows[target] = {'beforeSha256': before, 'afterSha256': reviewed[filename], 'payload': 'files/' + filename}
    check(all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
              for value in audit['dependencies'].values()), 'DRAIN_BUNDLE_AUDIT_REFUSED')
    manifest = {'schemaVersion': 1, 'scope': installer.SCOPE, 'root': str(installer.ROOT),
                'installerSha256': reviewed['installer'], 'files': rows, 'dependencies': audit['dependencies']}
    raw = installer.canonical(manifest); name = hashlib.sha256(raw).hexdigest(); bundle = output / name
    bundle.mkdir(mode=0o700)
    try:
        (bundle / 'files').mkdir(mode=0o700)
        write(bundle / 'manifest.json', raw); write(bundle / 'install.py', data['installer'])
        for filename in installer.FILES.values(): write(bundle / 'files' / filename, data[filename])
        installer.sync_dir(bundle / 'files'); installer.sync_dir(bundle); installer.sync_dir(output)
    except BaseException:
        shutil.rmtree(bundle); raise
    return {'status': 'built', 'bundle': str(bundle), 'manifestSha256': name,
            'sourceSha256': reviewed, 'installed': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit', required=True, type=Path)
    parser.add_argument('--reviewed', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.audit, args.reviewed, args.output), sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        value = str(error)
        print(json.dumps({'status': 'failed', 'code': value if re.fullmatch('DRAIN_BUNDLE_[A-Z_]+', value) else 'DRAIN_BUNDLE_FAILED'}))
        sys.exit(1)
