import hashlib
import fcntl
import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'server/api_drain_install.py'
spec = importlib.util.spec_from_file_location('api_drain_installer_test', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Fixture:
    def __init__(self, root):
        self.root = root
        for directory in ('operations/release/state', 'operations/startup',
                          'operations/release/drain-update-bundles', 'locks'):
            (root / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.before = {}
        self.after = {}
        for name in m.FILES:
            data = None if name in m.OPTIONAL_FILES else ('# old ' + name + '\n').encode()
            self.before[name] = data
            self.after[name] = ('# reviewed ' + name + '\n').encode()
            if data is not None:
                (root / name).write_bytes(data); (root / name).chmod(0o600)
        (root / m.OPS).write_bytes(b'# test production ops\n'); (root / m.OPS).chmod(0o600)
        self.runtime = {'configSha256': 'c' * 64,
                        'containers': {'api': {'id': 'a' * 64, 'image': 'sha256:' + 'b' * 64, 'startedAt': 'stable'}}}
        rows = {name: {'beforeSha256': m.digest(self.before[name]) if self.before[name] is not None else None,
                       'afterSha256': m.digest(self.after[name]), 'payload': 'files/' + filename}
                for name, filename in m.FILES.items()}
        manifest = {'schemaVersion': 1, 'scope': m.SCOPE, 'root': str(m.ROOT),
                    'installerSha256': m.digest(SOURCE.read_bytes()), 'files': rows,
                    'dependencies': {m.OPS: m.digest((root / m.OPS).read_bytes())}}
        raw = m.canonical(manifest)
        bundle = root / 'operations/release/drain-update-bundles' / m.digest(raw)
        (bundle / 'files').mkdir(mode=0o700, parents=True)
        (bundle / 'manifest.json').write_bytes(raw); (bundle / 'manifest.json').chmod(0o600)
        (bundle / 'install.py').write_bytes(SOURCE.read_bytes()); (bundle / 'install.py').chmod(0o600)
        for name, filename in m.FILES.items():
            (bundle / 'files' / filename).write_bytes(self.after[name]); (bundle / 'files' / filename).chmod(0o600)
        self.installer = m.Installer(bundle, root=root, uid=os.getuid(), gid=os.getgid(), lock_dir=root / 'locks')
        self.installer.runtime = lambda: self.runtime
        self.installer.fence = lambda runtime: self.assert_runtime(runtime)

    def assert_runtime(self, runtime):
        if runtime != self.runtime: raise AssertionError('fence used wrong service identities')

    def read(self, name):
        path = self.root / name
        return path.read_bytes() if path.exists() else None


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name).resolve())

    def test_install_writes_reviewed_files_without_service_mutation(self):
        self.assertEqual(self.f.installer.check()['status'], 'verified')
        self.assertEqual(self.f.installer.apply()['status'], 'installed')
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.after)
        self.assertIsNone(self.f.read(m.PENDING))

    def test_new_dependencies_are_installed_before_new_receiver_is_exposed(self):
        events=[]
        self.f.installer.fault=lambda name,data:events.append((name,data))
        self.f.installer.apply()
        final=events.index((m.RECEIVER,self.f.after[m.RECEIVER]))
        self.assertLess(events.index((m.RECEIVER,m.GATE)),final)
        for name in ('release_capacity.py','maintenance_gate.py','image_retention.py','codex_runtime.py'):
            target='operations/release/'+name
            if name!='codex_runtime.py':self.assertIsNone(self.f.before[target])
            self.assertLess(events.index((target,self.f.after[target])),final)

    def test_existing_runtime_policy_is_required_and_installed_as_same_transaction(self):
        target='operations/release/codex_runtime.py'
        self.assertIn(target,m.FILES);self.assertNotIn(target,m.OPTIONAL_FILES)
        (self.f.root/target).unlink()
        with self.assertRaisesRegex(ValueError,'DRAIN_INSTALL_STALE_SOURCE'):self.f.installer.apply()
        self.assertIsNone(self.f.read(m.PENDING));self.assertIsNone(self.f.read(m.ROLLBACK_READY))
        for name in set(m.FILES)-{target}:self.assertEqual(self.f.read(name),self.f.before[name])

    def test_admission_and_shared_gate_pending_state_block_install_without_writes(self):
        for name in ('release-admission.pending.json','maintenance-gate.pending.json'):
            path=self.f.root/'operations/release/state'/name
            path.write_bytes(b'{}');path.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'DRAIN_INSTALL_CONTROL_PENDING'):self.f.installer.apply()
            self.assertEqual({name:self.f.read(name)for name in m.FILES},self.f.before)
            self.assertIsNone(self.f.read(m.PENDING));path.unlink()

    def test_wrong_before_hash_and_pending_operation_refuse_before_writes(self):
        (self.f.root / m.RECEIVER).write_bytes(b'# unexpected\n')
        with self.assertRaisesRegex(ValueError, 'DRAIN_INSTALL_STALE_SOURCE'): self.f.installer.apply()
        self.assertIsNone(self.f.read(m.PENDING))
        (self.f.root / m.RECEIVER).write_bytes(self.f.before[m.RECEIVER])
        (self.f.root / m.BLOCKERS[2]).write_bytes(b'{}'); (self.f.root / m.BLOCKERS[2]).chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'DRAIN_INSTALL_CONTROL_PENDING'): self.f.installer.apply()
        self.assertEqual(self.f.read(m.RECEIVER), self.f.before[m.RECEIVER])

    def test_crash_after_receiver_gate_recovers_exact_old_files(self):
        def fault(name, data):
            if name == m.RECEIVER and data == m.GATE: raise SystemExit('synthetic crash')
        self.f.installer.fault = fault
        with self.assertRaises(SystemExit): self.f.installer.apply()
        self.assertEqual(self.f.read(m.RECEIVER), m.GATE)
        self.assertIsNotNone(self.f.read(m.ROLLBACK_READY))
        self.f.installer.fault = None
        self.assertEqual(self.f.installer.recover()['status'], 'restored')
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.before)
        self.assertIsNone(self.f.read(m.PENDING))

    def test_crash_after_commit_revalidates_then_finishes(self):
        def fault(name, data):
            if name == m.PENDING and data and m.decode(data)['phase'] == 'committed': raise SystemExit('synthetic crash')
        self.f.installer.fault = fault
        with self.assertRaises(SystemExit): self.f.installer.apply()
        self.f.installer.fault = None
        self.assertEqual(self.f.installer.recover()['status'], 'installed')
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.after)

    def test_unknown_change_during_recovery_is_never_overwritten(self):
        def fault(name, data):
            if name == m.RECEIVER and data == m.GATE: raise SystemExit('synthetic crash')
        self.f.installer.fault = fault
        with self.assertRaises(SystemExit): self.f.installer.apply()
        self.f.installer.fault = None
        (self.f.root / m.RECEIVER).write_bytes(b'# external change\n')
        with self.assertRaisesRegex(ValueError, 'DRAIN_INSTALL_RESTORE_MISMATCH'): self.f.installer.recover()
        self.assertEqual(self.f.read(m.RECEIVER), b'# external change\n')
        self.assertIsNotNone(self.f.read(m.ROLLBACK_READY))

    def test_recovery_refuses_other_pending_host_operations(self):
        def fault(name, data):
            if name == m.RECEIVER and data == m.GATE: raise SystemExit('synthetic crash')
        self.f.installer.fault = fault
        with self.assertRaises(SystemExit): self.f.installer.apply()
        self.f.installer.fault = None
        blocker = self.f.root / m.BLOCKERS[0]; blocker.write_bytes(b'{}'); blocker.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'DRAIN_INSTALL_CONTROL_PENDING'): self.f.installer.recover()
        self.assertEqual(self.f.read(m.RECEIVER), m.GATE)
        self.assertIsNotNone(self.f.read(m.ROLLBACK_READY))

    def test_fence_detects_direct_docker_operations_on_real_container_identity(self):
        inst = self.f.installer; runtime = self.f.runtime
        self.assertTrue(inst.control_command(['/usr/bin/docker', 'stop', 'a' * 64], runtime))
        self.assertTrue(inst.control_command(['/usr/bin/docker', 'stop', 'a' * 12], runtime))
        self.assertTrue(inst.control_command(['/usr/bin/docker', 'exec', 'teaching-production-api-1', 'node'], runtime))
        self.assertTrue(inst.control_command(['/usr/bin/python3', '-c', 'load(' + str(self.f.root / m.RECEIVER) + ')'], runtime))
        self.assertTrue(inst.control_command(['/usr/bin/python3', '-I', str(self.f.root / 'operations/release/api_image_shutdown.py')], runtime))
        self.assertFalse(inst.control_command(['/usr/bin/docker', 'stop', 'c' * 64], runtime))

    def test_exclusive_control_lock_refuses_without_creating_journal(self):
        path = self.f.root / 'locks/teaching-production-release.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'DRAIN_INSTALL_CONTROL_BUSY'): self.f.installer.apply()
            self.assertIsNone(self.f.read(m.PENDING))
        finally: os.close(fd)

    def test_crash_after_each_file_replace_is_recoverable(self):
        for target in m.FILES:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory).resolve())
                def fault(name, data):
                    if name == target and data == fixture.after[target]: raise SystemExit('synthetic crash')
                fixture.installer.fault = fault
                with self.assertRaises(SystemExit): fixture.installer.apply()
                fixture.installer.fault = None
                self.assertEqual(fixture.installer.recover()['status'], 'restored')
                self.assertEqual({name: fixture.read(name) for name in m.FILES}, fixture.before)
                self.assertIsNone(fixture.read(m.PENDING))

    def test_entry_gate_is_executable_python(self):
        compile(m.GATE, 'recovery-gate', 'exec')

    def test_crash_between_pending_clear_and_restoring_old_entry_is_recoverable(self):
        def first_crash(name, data):
            if name.endswith('/api_drain.py') and data == self.f.after[name]: raise SystemExit('initial synthetic crash')
        self.f.installer.fault = first_crash
        with self.assertRaises(SystemExit): self.f.installer.apply()
        def rollback_crash(name, data):
            if name == m.PENDING and data is None: raise SystemExit('rollback synthetic crash')
        self.f.installer.fault = rollback_crash
        with self.assertRaises(SystemExit): self.f.installer.recover()
        self.assertIsNone(self.f.read(m.PENDING))
        self.assertIsNotNone(self.f.read(m.ROLLBACK_READY))
        self.assertEqual(self.f.read(m.RECEIVER), m.GATE)
        self.f.installer.fault = None
        self.assertEqual(self.f.installer.recover()['status'], 'restored')
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.before)
        self.assertIsNone(self.f.read(m.ROLLBACK_READY))

    def test_crash_after_restoring_old_entry_cleans_rollback_checkpoint(self):
        def first_crash(name, data):
            if name.endswith('/api_drain.py') and data == self.f.after[name]: raise SystemExit('initial synthetic crash')
        self.f.installer.fault = first_crash
        with self.assertRaises(SystemExit): self.f.installer.apply()
        def rollback_crash(name, data):
            if name == m.RECEIVER and data == self.f.before[m.RECEIVER]: raise SystemExit('rollback synthetic crash')
        self.f.installer.fault = rollback_crash
        with self.assertRaises(SystemExit): self.f.installer.recover()
        self.assertEqual(self.f.read(m.RECEIVER), self.f.before[m.RECEIVER])
        self.assertIsNotNone(self.f.read(m.ROLLBACK_READY))
        self.f.installer.fault = None
        self.assertEqual(self.f.installer.recover()['status'], 'restored')
        self.assertIsNone(self.f.read(m.ROLLBACK_READY))

    def test_crash_after_prepared_checkpoint_leaves_legacy_files_intact(self):
        def fault(name, data):
            if name == m.ROLLBACK_READY and data and m.decode(data)['phase'] == 'prepared':
                raise SystemExit('synthetic crash')
        self.f.installer.fault = fault
        with self.assertRaises(SystemExit): self.f.installer.apply()
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.before)
        self.assertIsNone(self.f.read(m.PENDING))
        self.f.installer.fault = None
        self.assertEqual(self.f.installer.recover()['status'], 'restored')

    def test_commit_cleanup_failure_never_rolls_back_committed_files(self):
        def fault(name, data):
            if name == m.PENDING and data and m.decode(data)['phase'] == 'committed':
                raise ValueError('synthetic cleanup failure')
        self.f.installer.fault = fault
        with self.assertRaisesRegex(ValueError, 'synthetic cleanup failure'): self.f.installer.apply()
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.after)
        self.assertEqual(m.decode(self.f.read(m.PENDING))['phase'], 'committed')
        self.f.installer.fault = None
        self.assertEqual(self.f.installer.recover()['status'], 'installed')
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.after)

    def test_crash_after_commit_marker_removed_finishes_from_checkpoint(self):
        def fault(name, data):
            if name == m.PENDING and data is None: raise SystemExit('synthetic crash')
        self.f.installer.fault = fault
        with self.assertRaises(SystemExit): self.f.installer.apply()
        self.assertIsNone(self.f.read(m.PENDING))
        self.assertEqual(m.decode(self.f.read(m.ROLLBACK_READY))['phase'], 'committed')
        self.assertEqual({name: self.f.read(name) for name in m.FILES}, self.f.after)
        self.f.installer.fault = None
        self.assertEqual(self.f.installer.recover()['status'], 'installed')

    def test_real_sigkill_after_gate_and_commit_recovers_without_service_changes(self):
        child = r'''
import os, signal, sys
from pathlib import Path
from scripts.test_api_drain_install import m
root=Path(sys.argv[1]); bundle=Path(sys.argv[2]); point=sys.argv[3]
runtime={'configSha256':'c'*64, 'containers':{'api': {'id': 'a'*64, 'image': 'sha256:'+'b'*64, 'startedAt':'stable'}}}
inst=m.Installer(bundle,root=root,uid=os.getuid(),gid=os.getgid(),lock_dir=root/'locks')
inst.runtime=lambda:runtime
inst.fence=lambda value:None
def kill(name,data):
    if point=='gate' and name==m.RECEIVER and data==m.GATE:os.kill(os.getpid(),signal.SIGKILL)
    if point=='commit' and name==m.PENDING and data and m.decode(data)['phase']=='committed':os.kill(os.getpid(),signal.SIGKILL)
inst.fault=kill
inst.apply()
'''
        for point in ('gate', 'commit'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory).resolve())
                run = subprocess.run([sys.executable, '-B', '-c', child, str(fixture.root),
                                      str(fixture.installer.bundle), point], timeout=20, cwd=SOURCE.parent.parent)
                self.assertEqual(run.returncode, -signal.SIGKILL)
                self.assertEqual(fixture.installer.recover()['status'], 'restored' if point == 'gate' else 'installed')
                self.assertEqual({name: fixture.read(name) for name in m.FILES},
                                 fixture.before if point == 'gate' else fixture.after)

    def test_bundle_builder_binds_reviewed_sources_and_rejects_pending_state(self):
        path = SOURCE.parent.parent / 'scripts/build_api_drain_install_bundle.py'
        spec = importlib.util.spec_from_file_location('drain_bundle_test', path)
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        audit = {'schemaVersion': 1, 'scope': 'teaching-production-api-drain-audit-v1', 'root': str(m.ROOT),
                 'kernel': '6.8.0-124-generic',
                 'targets': {name: m.digest(raw) if raw is not None else None for name, raw in self.f.before.items()},
                 'dependencies': {m.OPS: m.digest((self.f.root / m.OPS).read_bytes())},
                 'blockersPresent': {name: False for name in (*m.BLOCKERS, m.PENDING, m.ROLLBACK_READY)}}
        reviewed = {name: m.digest(path.read_bytes()) for name, path in builder.SOURCES.items()}
        audit_path = self.f.root / 'audit.json'; audit_path.write_text(json.dumps(audit))
        review_path = self.f.root / 'review.json'; review_path.write_text(json.dumps(reviewed))
        out = self.f.root / 'output'; out.mkdir(mode=0o700)
        result = builder.build(audit_path, review_path, out)
        bundle = Path(result['bundle'])
        self.assertEqual(m.digest((bundle / 'manifest.json').read_bytes()), bundle.name)
        self.assertEqual((bundle / 'install.py').read_bytes(), SOURCE.read_bytes())
        audit['blockersPresent'][m.PENDING] = True; audit_path.write_text(json.dumps(audit))
        with self.assertRaisesRegex(ValueError, 'DRAIN_BUNDLE_AUDIT_REFUSED'):
            builder.build(audit_path, review_path, out)
        audit['blockersPresent'][m.PENDING] = False; audit_path.write_text(json.dumps(audit))
        reviewed['receiver.py'] = '0' * 64; review_path.write_text(json.dumps(reviewed))
        with self.assertRaisesRegex(ValueError, 'DRAIN_BUNDLE_SOURCE_CHANGED'):
            builder.build(audit_path, review_path, out)

    def test_private_receiver_checks_exact_members_hashes_paths_and_idempotence(self):
        root = SOURCE.parent.parent
        def module(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value
        builder = module('drain_builder_transport', root / 'scripts/build_api_drain_install_bundle.py')
        receiver = module('drain_receiver_transport', root / 'server/api_drain_bundle_receive.py')
        sender = module('drain_sender_transport', root / 'scripts/send_api_drain_install_bundle.py')
        audit = {'schemaVersion': 1, 'scope': 'teaching-production-api-drain-audit-v1', 'root': str(m.ROOT),
                 'kernel': '6.8.0-124-generic',
                 'targets': {name: m.digest(raw) if raw is not None else None for name, raw in self.f.before.items()},
                 'dependencies': {m.OPS: m.digest((self.f.root / m.OPS).read_bytes())},
                 'blockersPresent': {name: False for name in (*m.BLOCKERS, m.PENDING, m.ROLLBACK_READY)}}
        reviewed = {name: m.digest(path.read_bytes()) for name, path in builder.SOURCES.items()}
        audit_path = self.f.root / 'audit.json'; audit_path.write_text(json.dumps(audit))
        review_path = self.f.root / 'review.json'; review_path.write_text(json.dumps(reviewed))
        out = self.f.root / 'output'; out.mkdir(mode=0o700)
        bundle = Path(builder.build(audit_path, review_path, out)['bundle'])
        raw = sender.payload(bundle, receiver)
        endpoint = receiver.Receiver(root=self.f.root, uid=os.getuid(), gid=os.getgid(), lock_dir=self.f.root / 'locks')
        with self.assertRaises(Exception): endpoint.receive(raw[:len(raw)//2], bundle.name)
        self.assertFalse((endpoint.parent / bundle.name).exists())
        message = json.loads(raw); message['files']['../escape'] = base64.b64encode(b'bad').decode()
        with self.assertRaisesRegex(ValueError, 'DRAIN_RECEIVE_MEMBERS_REFUSED'):
            endpoint.receive(json.dumps(message).encode(), bundle.name)
        message = json.loads(raw); message['files']['files/api_drain.py'] = base64.b64encode(b'changed').decode()
        with self.assertRaisesRegex(ValueError, 'DRAIN_RECEIVE_MANIFEST_REFUSED'):
            endpoint.receive(json.dumps(message).encode(), bundle.name)
        for helper in ('release_capacity.py','maintenance_gate.py','image_retention.py','codex_runtime.py'):
            message=json.loads(raw);del message['files']['files/'+helper]
            with self.assertRaisesRegex(ValueError,'DRAIN_RECEIVE_MEMBERS_REFUSED'):
                endpoint.receive(json.dumps(message).encode(),bundle.name)
        self.assertEqual(endpoint.receive(raw, bundle.name)['status'], 'received')
        self.assertEqual(endpoint.receive(raw, bundle.name)['status'], 'present')
        for name in receiver.MEMBERS:
            self.assertEqual((endpoint.parent / bundle.name / name).read_bytes(), (bundle / name).read_bytes())

    def test_private_receiver_refuses_symlink_and_stale_source_before_staging(self):
        path = SOURCE.parent / 'api_drain_bundle_receive.py'
        spec = importlib.util.spec_from_file_location('drain_receiver_scope', path)
        receiver = importlib.util.module_from_spec(spec); spec.loader.exec_module(receiver)
        endpoint = receiver.Receiver(root=self.f.root, uid=os.getuid(), gid=os.getgid(), lock_dir=self.f.root / 'locks')
        target = self.f.root / 'operations/release/api_drain.py'
        target.symlink_to(self.f.root / m.RECEIVER)
        with self.assertRaisesRegex(ValueError, 'DRAIN_RECEIVE_PATH_UNSAFE'):
            endpoint.private(target, missing=True)

    def test_sender_timeout_is_unknown_and_never_retries(self):
        path = SOURCE.parent.parent / 'scripts/send_api_drain_install_bundle.py'
        spec = importlib.util.spec_from_file_location('drain_sender_timeout', path)
        sender = importlib.util.module_from_spec(spec); spec.loader.exec_module(sender)
        identity = self.f.root / 'identity'; identity.write_bytes(b'fixture')
        known = self.f.root / 'known'; known.write_bytes(b'fixture')
        bundle = self.f.root / ('a' * 64)
        with patch.object(sender, 'payload', return_value=b'fixture'), patch.object(sender, 'IDENTITY', identity), \
             patch.object(sender, 'KNOWN_HOSTS', known), \
             patch.object(sender.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 180)) as call:
            result = sender.send(bundle, m.digest(sender.RECEIVER.read_bytes()))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(call.call_count, 1)

    def test_sender_rejects_changed_root_receiver_before_ssh(self):
        path = SOURCE.parent.parent / 'scripts/send_api_drain_install_bundle.py'
        spec = importlib.util.spec_from_file_location('drain_sender_hash', path)
        sender = importlib.util.module_from_spec(spec); spec.loader.exec_module(sender)
        with patch.object(sender.subprocess, 'run') as call:
            with self.assertRaisesRegex(ValueError, 'DRAIN_SEND_RECEIVER_CHANGED'):
                sender.send(self.f.root / ('a' * 64), '0' * 64)
        call.assert_not_called()

    def test_audit_receiver_builder_and_installer_share_exact_fixed_scope(self):
        root=SOURCE.parent.parent
        modules={}
        for name,file in (('audit','server/api_drain_install_audit.py'),('receive','server/api_drain_bundle_receive.py'),
                          ('builder','scripts/build_api_drain_install_bundle.py')):
            spec=importlib.util.spec_from_file_location('matched_'+name,root/file)
            value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);modules[name]=value
        self.assertEqual(set(modules['audit'].TARGETS),set(m.FILES))
        self.assertEqual(modules['receive'].FILES,m.FILES)
        self.assertEqual(set(modules['builder'].SOURCES),{'installer'}|set(m.FILES.values()))
        self.assertEqual(modules['receive'].OPTIONAL_FILES,m.OPTIONAL_FILES)
        self.assertEqual(modules['audit'].OPTIONAL_FILES,m.OPTIONAL_FILES)
        self.assertEqual(set(modules['receive'].BLOCKERS),set(m.BLOCKERS)|{m.PENDING,m.ROLLBACK_READY})
        self.assertEqual(set(modules['audit'].BLOCKERS),set(modules['receive'].BLOCKERS))


if __name__ == '__main__': unittest.main()
