#!/usr/bin/python3
"""Root-only PG admission adapter. Protocol secrets are stdin/private receipts only."""
import importlib.util
import fcntl
import json
import os
import secrets
import signal
import uuid
from pathlib import Path
import subprocess
import stat
import sys
import time

ROOT = Path('/opt/teaching-ai-production')
APP_CONTROL = '/app/apps/api/dist/deployment-admission-control.mjs'
ACTIONS = {'acquire', 'begin_quiesce', 'await_quiescent', 'enter_critical', 'release_after_healthy', 'status', 'reconcile_owner', 'recover_pending'}

class GateRefused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def _require(condition, code):
    if not condition:
        raise GateRefused(code)

def _ops():
    _require(os.geteuid() == 0, 'GATE_ROOT_REQUIRED')
    path = ROOT / 'operations/startup/production-ops.py'
    for candidate in (ROOT, ROOT / 'operations', ROOT / 'operations/startup', path):
        info = candidate.lstat()
        _require(info.st_uid == 0 and not candidate.is_symlink() and not (stat.S_IMODE(info.st_mode) & 0o022), 'GATE_HELPER_PERMISSIONS_INVALID')
    _require(stat.S_ISREG(path.stat().st_mode), 'GATE_HELPER_PERMISSIONS_INVALID')
    spec = importlib.util.spec_from_file_location('maintenance_gate_production_ops', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _run(args, *, data=None, timeout=25, env=None):
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env=env)
    try:
        stdout, _ = process.communicate(input=data, timeout=timeout)
        return process.returncode, stdout
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            try: process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe: pipe.close()
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired: pass
        raise

def _runtime_coherence(ops, containers):
    names = ('api', 'worker-general', 'worker-ppt')
    flags = []
    for name in names:
        _require(name in containers, 'GATE_RUNTIME_CAPABILITY_MISMATCH')
        values = [entry.split('=', 1)[1] for entry in containers[name].get('Config', {}).get('Env', []) if entry.startswith('DEPLOYMENT_ADMISSION_ENABLED=')]
        _require(len(values) <= 1 and (not values or values[0] in ('true', 'false', '')), 'GATE_RUNTIME_CAPABILITY_MISMATCH')
        flags.append(values == ['true'])
    if not any(flags): return False
    _require(all(flags), 'GATE_RUNTIME_CAPABILITY_MISMATCH')
    script = "const fs=require('node:fs'),c=require('node:crypto');try{const role=process.argv[1],base='/app/apps/api/dist/',p=base+'deployment-admission-runtime.json',s=fs.lstatSync(p);if(!s.isFile()||s.isSymbolicLink()||s.size>4096)throw Error();const m=JSON.parse(fs.readFileSync(p,'utf8'));const hash=p=>{const s=fs.lstatSync(p);if(!s.isFile()||s.isSymbolicLink())throw Error();return c.createHash('sha256').update(fs.readFileSync(p)).digest('hex')};const ok=m.protocolVersion===2&&m.role===role&&m.entrySha256===hash(base+(role==='api'?'server':'ai-jobs-worker')+'.mjs')&&m.controlSha256===hash(base+'deployment-admission-control.mjs')&&process.env.DEPLOYMENT_ADMISSION_ENABLED==='true';console.log(JSON.stringify({supported:ok,protocolVersion:m.protocolVersion,role:m.role}))}catch{process.exit(2)}"
    for name in names:
        role = 'api' if name == 'api' else 'worker'
        code, output = _run(ops.DOCKER + ['exec', containers[name]['Id'], 'node', '-e', script, role], env=ops.fixed_env())
        try: proof = json.loads(output)
        except (ValueError, UnicodeError): raise GateRefused('GATE_RUNTIME_CAPABILITY_MISMATCH')
        _require(code == 0 and proof.get('supported') is True and proof.get('protocolVersion') == 2 and proof.get('role') == role, 'GATE_RUNTIME_CAPABILITY_MISMATCH')
    return True

def _bridge(request):
    ops = _ops()
    containers = ops.inspect()
    enhanced = _runtime_coherence(ops, containers)
    api = containers['api']
    probe_code, probe_stdout = _run(ops.DOCKER + ['exec', api['Id'], 'node', '-e', "const fs=require('node:fs');try{const s=fs.lstatSync(process.argv[1]);console.log(JSON.stringify({installed:s.isFile()&&!s.isSymbolicLink()}))}catch(e){if(e.code==='ENOENT')console.log(JSON.stringify({installed:false}));else process.exit(2)}", APP_CONTROL], timeout=15, env=ops.fixed_env())
    try:
        proof = json.loads(probe_stdout)
    except (ValueError, UnicodeError):
        raise GateRefused('GATE_APP_PROTOCOL_INVALID')
    _require(probe_code == 0 and type(proof.get('installed')) is bool, 'GATE_APP_CONTROL_FAILED')
    _require(proof['installed'], 'GATE_RUNTIME_CAPABILITY_MISMATCH' if enhanced else 'GATE_NOT_INSTALLED')
    code, stdout = _run(ops.DOCKER + ['exec', '-i', api['Id'], 'node', APP_CONTROL],
        data=json.dumps(request, separators=(',', ':')).encode(), env=ops.fixed_env())
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise GateRefused('GATE_APP_PROTOCOL_INVALID')
    if code or value.get('status') != 'ok':
        code = value.get('code')
        if enhanced and code in ('DEPLOYMENT_ADMISSION_DISABLED', 'GATE_NOT_INSTALLED'):
            raise GateRefused('GATE_RUNTIME_CAPABILITY_MISMATCH')
        allowed = {'DEPLOYMENT_ADMISSION_DISABLED','GATE_NOT_INSTALLED','GATE_BUSY','GATE_OPERATION_INVALID','GATE_OWNER_REQUIRED','GATE_OWNER_MISMATCH','GATE_PHASE_INVALID','GATE_WORK_NOT_DRAINED','GATE_HEALTH_PROOF_REQUIRED','GATE_ACTION_INVALID'}
        raise GateRefused(code if code in allowed else 'GATE_APP_CONTROL_FAILED')
    caps = value.get('capabilities', {})
    if not enhanced and caps.get('reversibleHttpAdmission') is True:
        raise GateRefused('GATE_RUNTIME_CAPABILITY_MISMATCH')
    if enhanced:
        _require(caps.get('protocolVersion') == 2 and caps.get('transactionalTaskAdmission') is True and caps.get('reversibleHttpAdmission') is True and caps.get('durableLineage') is True, 'GATE_RUNTIME_CAPABILITY_MISMATCH')
        identity = value.get('runtimeOwner', {})
        identifier = identity.get('containerId', '')
        _require(isinstance(identifier, str) and 12 <= len(identifier) <= 64 and api['Id'].startswith(identifier), 'GATE_RUNTIME_CAPABILITY_MISMATCH')
    return {**value, 'hostRuntime': {'containerId': api['Id'], 'startedAt': api['State']['StartedAt']}}

def _private_read(path):
    info = path.lstat()
    _require(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and not path.is_symlink(), 'GATE_JOURNAL_PERMISSIONS_INVALID')
    return json.loads(path.read_bytes())

def _journal_paths():
    _require(os.geteuid() == 0, 'GATE_ROOT_REQUIRED')
    for ancestor in (ROOT, ROOT / 'operations', ROOT / 'operations/release'):
        info = ancestor.lstat()
        _require(info.st_uid == 0 and not ancestor.is_symlink() and not (stat.S_IMODE(info.st_mode) & 0o022), 'GATE_JOURNAL_PERMISSIONS_INVALID')
    directory = ROOT / 'operations/release/maintenance-gate-state'
    state = ROOT / 'operations/release/state'
    for parent in (directory, state):
        parent.mkdir(mode=0o700, exist_ok=True)
        info = parent.lstat()
        _require(info.st_uid == 0 and not parent.is_symlink() and stat.S_IMODE(info.st_mode) == 0o700, 'GATE_JOURNAL_PERMISSIONS_INVALID')
    return directory, state / 'maintenance-gate.pending.json'

def _fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)

def _atomic_private(path, value):
    if path.exists() or path.is_symlink(): _private_read(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(value, handle, separators=(',', ':')); handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)

def _read_pending():
    _, pending = _journal_paths()
    _require(pending.exists() or pending.is_symlink(), 'GATE_PENDING_NOT_FOUND')
    return _private_read(pending)

def _journal(value):
    directory, _ = _journal_paths()
    descriptor = os.open(directory / '.journal.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        _require(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600, 'GATE_JOURNAL_PERMISSIONS_INVALID')
        try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise GateRefused('GATE_JOURNAL_BUSY')
        _journal_locked(value)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

def _journal_locked(value):
    directory, pending = _journal_paths()
    operation_id = value.get('operationId')
    _require(isinstance(operation_id, str) and str(uuid.UUID(operation_id)) == operation_id, 'GATE_OPERATION_INVALID')
    path = directory / (operation_id + '.json')
    if path.exists() or path.is_symlink():
        previous = _private_read(path)
        _require(previous.get('ownerToken') == value.get('ownerToken'), 'GATE_JOURNAL_COLLISION')
        value = {**previous, **value}
    if pending.exists() or pending.is_symlink():
        existing = _private_read(pending)
        _require(existing.get('operationId') == operation_id and existing.get('ownerToken') == value.get('ownerToken'), 'GATE_PENDING_OPERATION')
    # The shared marker is fsynced first, so existing host guards see uncertainty
    # even if the helper dies before writing the per-operation journal or PG.
    _atomic_private(pending, value)
    _atomic_private(path, value)
    if value.get('phase') == 'released':
        confirmed = _private_read(pending)
        _require(confirmed.get('operationId') == operation_id and confirmed.get('ownerToken') == value.get('ownerToken'), 'GATE_PENDING_OPERATION')
        pending.unlink()
        _fsync_directory(pending.parent)

def _terminated(owner):
    """A successful Docker inventory or exact /proc identity proves death, never age."""
    identifier = owner.get('containerId', '')
    _require(isinstance(identifier, str) and 12 <= len(identifier) <= 64 and all(c in '0123456789abcdef' for c in identifier), 'GATE_TERMINATION_PROOF_INVALID')
    ticks, boot_id = owner.get('processStartTicks'), owner.get('bootId')
    _require(type(owner.get('pid')) is int and 0 < owner['pid'] < 2**31 and isinstance(ticks, str) and ticks.isdigit() and str(int(ticks)) == ticks, 'GATE_TERMINATION_PROOF_INVALID')
    try: valid_boot_id = isinstance(boot_id, str) and str(uuid.UUID(boot_id)) == boot_id
    except ValueError: valid_boot_id = False
    _require(valid_boot_id, 'GATE_TERMINATION_PROOF_INVALID')
    ops = _ops()
    code, output = _run(ops.DOCKER + ['ps', '-aq', '--no-trunc'], env=ops.fixed_env())
    _require(code == 0, 'GATE_TERMINATION_PROOF_UNAVAILABLE')
    matches = [item for item in output.decode().split() if item.startswith(identifier)]
    if not matches:
        return True
    _require(len(matches) == 1, 'GATE_TERMINATION_PROOF_INVALID')
    code, output = _run(ops.DOCKER + ['inspect', matches[0]], env=ops.fixed_env())
    _require(code == 0, 'GATE_TERMINATION_PROOF_UNAVAILABLE')
    container = json.loads(output)[0]
    labels = container['Config']['Labels']
    _require(labels.get('com.docker.compose.project') == 'teaching-production' and labels.get('com.docker.compose.service') == 'api', 'GATE_TERMINATION_PROOF_INVALID')
    if not container['State']['Running']:
        return True
    script = "const fs=require('node:fs');try{const s=fs.readFileSync('/proc/'+process.argv[1]+'/stat','utf8');console.log(JSON.stringify({alive:true,start:s.slice(s.lastIndexOf(')')+2).trim().split(/\\s+/)[19],boot:fs.readFileSync('/proc/sys/kernel/random/boot_id','utf8').trim()}))}catch(e){if(e.code==='ENOENT')console.log(JSON.stringify({alive:false}));else process.exit(2)}"
    code, output = _run(ops.DOCKER + ['exec', matches[0], 'node', '-e', script, str(owner['pid'])], env=ops.fixed_env())
    _require(code == 0, 'GATE_TERMINATION_PROOF_UNAVAILABLE')
    proof = json.loads(output)
    _require(type(proof.get('alive')) is bool, 'GATE_TERMINATION_PROOF_INVALID')
    return not proof['alive'] or proof.get('start') != owner['processStartTicks'] or proof.get('boot') != owner['bootId']

def _healthy():
    sample = _ops().snapshot()
    return sample.get('healthy') is True and all(not value.get('paused', False) for value in sample.get('services', {}).values())

def dispatch(request, *, bridge=None, health_check=None, storage_barrier=None, journal_writer=None, journal_reader=None, termination_check=None, sleep=time.sleep, monotonic=time.monotonic):
    """Dependency injection permits fully offline receiver/backup contract tests."""
    _require(isinstance(request, dict) and request.get('action') in ACTIONS, 'GATE_ACTION_INVALID')
    use_real_bridge = bridge is None
    bridge = bridge or _bridge
    journal_writer = journal_writer or (_journal if use_real_bridge else lambda _: None)
    journal_reader = journal_reader or _read_pending
    health_check = health_check or _healthy
    termination_check = termination_check or _terminated
    action = request['action']
    if action == 'status':
        try:
            return bridge(request)
        except GateRefused as error:
            if error.code in ('DEPLOYMENT_ADMISSION_DISABLED', 'GATE_NOT_INSTALLED'):
                return {'status': 'unsupported', 'code': error.code, 'capabilities': {'protocolVersion': 1, 'reversibleHttpAdmission': False, 'directStorageWriteBarrier': False}}
            raise
    if action == 'recover_pending':
        pending = journal_reader()
        _require(request.get('operationId') == pending.get('operationId') and request.get('ownerToken') == pending.get('ownerToken'), 'GATE_OWNER_MISMATCH')
        observed = bridge({'action': 'status'})
        if observed.get('operationId') == pending['operationId']:
            recovered = bridge({**pending, 'action': 'acquire'})
            _require(recovered.get('operationId') == pending['operationId'] and recovered.get('ownerToken') == pending['ownerToken'] and str(recovered.get('generation', '')).isdigit(), 'GATE_ACQUIRE_RECEIPT_INVALID')
            journal_writer({**pending, **recovered, 'phase': recovered['state'], 'acquisitionStatus': 'acquired'})
            return recovered
        _require(observed.get('operationId') is None and observed.get('state') == 'accepting', 'GATE_PENDING_OWNER_CONFLICT')
        _require(health_check() is True, 'GATE_RUNTIME_UNHEALTHY')
        journal_writer({**pending, 'phase': 'released', 'recovery': 'pg_unowned_healthy'})
        return observed
    if action == 'acquire':
        request = {**request, 'operationId': request.get('operationId') or str(uuid.uuid4()), 'ownerToken': request.get('ownerToken') or secrets.token_urlsafe(32)}
        journal_writer({**request, 'phase': 'acquiring', 'acquisitionStatus': 'pending'})
        result = bridge(request)
        _require(result.get('operationId') == request['operationId'] and result.get('ownerToken') == request['ownerToken'] and str(result.get('generation', '')).isdigit(), 'GATE_ACQUIRE_RECEIPT_INVALID')
        journal_writer({**request, **result, 'phase': result['state'], 'acquisitionStatus': 'acquired'})
        return result
    if action != 'acquire':
        _require(isinstance(request.get('operationId'), str) and request['operationId'], 'GATE_OWNER_REQUIRED')
        _require(isinstance(request.get('ownerToken'), str) and len(request['ownerToken']) >= 32, 'GATE_OWNER_REQUIRED')
        _require(str(request.get('generation', '')).isdigit(), 'GATE_OWNER_REQUIRED')
    if action == 'reconcile_owner':
        observed = bridge({**request, 'action': 'status'})
        owner = next((item for item in observed.get('runtimeOwners', []) if item.get('id') == request.get('terminatedInstanceId')), None)
        _require(owner and termination_check(owner) is True, 'GATE_OWNER_STILL_ALIVE')
        return bridge({**request, 'evidence': {'verifiedByHostHelper': True, 'verifiedTerminated': True}})
    if action == 'await_quiescent':
        timeout = request.get('timeoutSeconds', 300)
        _require(type(timeout) in (int, float) and 0 <= timeout <= 900, 'GATE_TIMEOUT_INVALID')
        deadline = monotonic() + timeout
        while True:
            result = bridge(request)
            if result.get('quiescent') is True:
                journal_writer({**request, **result, 'phase': result['state']})
                return result
            for owner in result.get('runtimeOwners', []):
                if termination_check(owner) is True:
                    bridge({**request, 'action': 'reconcile_owner', 'terminatedInstanceId': owner['id'], 'evidence': {'verifiedByHostHelper': True, 'verifiedTerminated': True}})
            if monotonic() >= deadline:
                raise GateRefused('GATE_QUIESCE_TIMEOUT')
            sleep(min(2, max(0, deadline - monotonic())))
    if action == 'enter_critical' and request.get('requireStorageWriteBarrier') is True:
        # Application admission cannot stop an already-issued presigned PUT.
        # A real host adapter must close that ingress and drain confirmed writes.
        _require(callable(storage_barrier), 'GATE_STORAGE_BARRIER_UNAVAILABLE')
        proof = storage_barrier(request)
        _require(isinstance(proof, dict) and proof.get('closed') is True and proof.get('inFlightWrites') == 0 and proof.get('operationId') == request['operationId'], 'GATE_STORAGE_BARRIER_UNPROVEN')
    if action == 'release_after_healthy':
        proof = request.get('evidence', {})
        _require(proof.get('healthy') is True and proof.get('servicesRestored') is True and proof.get('migrationReviewRequired') is False, 'GATE_HEALTH_PROOF_REQUIRED')
        _require(health_check() is True, 'GATE_RUNTIME_UNHEALTHY')
        request = {**request, 'evidence': {**proof, 'verifiedByHostHelper': True}}
    result = bridge(request)
    if action == 'enter_critical':
        result = {**result, 'storageWriteBarrierVerified': request.get('requireStorageWriteBarrier') is True}
    if action in ('begin_quiesce', 'enter_critical', 'release_after_healthy'):
        journal_writer({**request, **result, 'operationId': request['operationId'], 'phase': 'released' if action == 'release_after_healthy' else result['state']})
    return result

def main():
    _require(len(sys.argv) == 1, 'GATE_ARGUMENTS_FORBIDDEN')
    raw = sys.stdin.buffer.read(65537)
    _require(len(raw) <= 65536, 'GATE_INPUT_TOO_LARGE')
    result = dispatch(json.loads(raw))
    sys.stdout.write(json.dumps(result, separators=(',', ':')) + '\n')

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        code = error.code if isinstance(error, GateRefused) else 'GATE_CONTROL_FAILED'
        sys.stdout.write(json.dumps({'status': 'failed', 'code': code}, separators=(',', ':')) + '\n')
        sys.exit(1)
