#!/usr/bin/env python3
"""Send one verified private updater bundle to the fixed Production host.

No bundle contents or credentials enter the remote command line or output.
An uncertain SSH result is reported for read-only inspection, never retried.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
RECEIVER = REPO / 'server/api_drain_bundle_receive.py'
IDENTITY = Path('/Users/wonderl/.ssh/teaching-ai-tencent-validation')
KNOWN_HOSTS = Path('/Users/wonderl/Backups/tencent-production-cutover-20261024/pipeline/server-known-hosts')
HOST = 'ubuntu@49.233.166.202'


def check(value, code):
    if not value: raise ValueError(code)


def payload(bundle, receiver):
    check(bundle.is_absolute() and bundle.resolve() == bundle and not bundle.is_symlink() and
          re.fullmatch('[0-9a-f]{64}', bundle.name), 'DRAIN_SEND_BUNDLE_UNSAFE')
    for directory in (bundle, bundle / 'files'):
        info = directory.lstat()
        check(stat.S_ISDIR(info.st_mode) and not directory.is_symlink() and info.st_uid == os.geteuid() and
              stat.S_IMODE(info.st_mode) == 0o700, 'DRAIN_SEND_BUNDLE_UNSAFE')
    actual = {str(path.relative_to(bundle)) for path in bundle.iterdir() if path.name != 'files'}
    actual |= {'files/' + path.name for path in (bundle / 'files').iterdir()}
    check(actual == receiver.MEMBERS, 'DRAIN_SEND_MEMBERS_REFUSED')
    files = {}
    for name in receiver.MEMBERS:
        path = bundle / name; info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_uid == os.geteuid() and
              info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 524288,
              'DRAIN_SEND_FILE_UNSAFE')
        files[name] = base64.b64encode(path.read_bytes()).decode('ascii')
    raw = json.dumps({'schemaVersion': 1, 'files': files}, sort_keys=True, separators=(',', ':')).encode()
    receiver.Receiver().unpack(raw, bundle.name)
    return raw


def send(bundle, expected_receiver_sha256):
    check(isinstance(expected_receiver_sha256, str) and re.fullmatch('[0-9a-f]{64}', expected_receiver_sha256),
          'DRAIN_SEND_RECEIVER_HASH_REQUIRED')
    source_bytes = RECEIVER.read_bytes()
    check(hashlib.sha256(source_bytes).hexdigest() == expected_receiver_sha256, 'DRAIN_SEND_RECEIVER_CHANGED')
    spec = importlib.util.spec_from_file_location('bundle_receiver', RECEIVER)
    receiver = importlib.util.module_from_spec(spec)
    exec(compile(source_bytes, str(RECEIVER), 'exec'), receiver.__dict__)
    raw = payload(bundle, receiver)
    check(IDENTITY.is_file() and KNOWN_HOSTS.is_file(), 'DRAIN_SEND_SSH_UNAVAILABLE')
    source = source_bytes.decode('utf-8', 'strict')
    remote = shlex.join(['sudo', '-n', '/usr/bin/python3', '-c', source, bundle.name])
    args = ['ssh', '-F', '/dev/null', '-T', '-i', str(IDENTITY), '-o', 'IdentitiesOnly=yes',
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=20',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-o', 'UserKnownHostsFile=' + str(KNOWN_HOSTS), HOST, remote]
    try:
        result = subprocess.run(args, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    except subprocess.TimeoutExpired:
        return {'status': 'unknown', 'code': 'DRAIN_SEND_TIMEOUT', 'bundle': bundle.name, 'installed': False}
    try: value = json.loads(result.stdout)
    except Exception:
        return {'status': 'unknown', 'code': 'DRAIN_SEND_RESPONSE_INVALID', 'bundle': bundle.name, 'installed': False}
    if result.returncode != 0:
        code = value.get('code') if isinstance(value, dict) else None
        return {'status': 'failed', 'code': code if isinstance(code, str) and re.fullmatch('DRAIN_RECEIVE_[A-Z_]+', code)
                else 'DRAIN_SEND_FAILED', 'bundle': bundle.name, 'installed': False}
    check(isinstance(value, dict) and set(value) == {'status', 'bundle', 'installed'} and
          value['status'] in ('received', 'present') and value['bundle'] == bundle.name and
          value['installed'] is False, 'DRAIN_SEND_RESPONSE_INVALID')
    return value


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--receiver-sha256', required=True)
    args = parser.parse_args(); result = send(args.bundle, args.receiver_sha256)
    print(json.dumps(result, sort_keys=True)); return 0 if result['status'] in ('received', 'present') else 1


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as error:
        code = str(error) if re.fullmatch('DRAIN_SEND_[A-Z_]+', str(error)) else 'DRAIN_SEND_FAILED'
        print(json.dumps({'status': 'failed', 'code': code, 'installed': False})); raise SystemExit(1)
