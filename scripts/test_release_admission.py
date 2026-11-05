import contextlib
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('admission_receiver',Path(__file__).resolve().parents[1]/'server/tencent_receiver.py')
        self.r=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.r)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.r.STATE=Path(self.temp.name).resolve()
        self.events=[];self.records={};self.fail=None;self.legacy=False
        class Error(Exception):pass
        self.error=Error
        self.module=types.SimpleNamespace(dispatch=self.dispatch,GateRefused=Error)

    def dispatch(self,request):
        action=request['action'];self.events.append(action)
        if self.fail==action:raise self.error('PRIVATE_SENTINEL')
        if action=='status':
            if self.legacy:return dict(status='unsupported',code='GATE_NOT_INSTALLED')
            return dict(status='ok',capabilities=dict(protocolVersion=2,reversibleHttpAdmission=True))
        if action=='acquire':return dict(status='ok',operationId='lease-1',generation='1',ownerToken='a'*43)
        if action=='await_quiescent':return dict(status='ok',quiescent=True)
        if action=='release_after_healthy':return dict(status='ok',state='accepting',generation='1',operationId=None)
        return dict(status='ok')

    @contextlib.contextmanager
    def mocks(self):
        def atomic(path,value):self.records[path]=value;path.write_text('private');path.chmod(0o600)
        with patch.object(self.r,'sibling_module',return_value=self.module), \
             patch.object(self.r,'atomic',side_effect=atomic), \
             patch.object(self.r,'optional_record',side_effect=lambda path:self.records.get(path)), \
             patch.object(self.r,'sync_directory'):
            yield

    def header(self):return dict(sourceSha='a'*40,version='0.0.137')

    def test_v2_quiesces_before_critical_and_releases_only_after_health_check(self):
        with self.mocks(),self.r.admission_scope(self.header()) as lease:
            self.assertEqual(self.events,['status','acquire','begin_quiesce','await_quiescent'])
            self.r.admission_request(lease,'enter_critical',requireStorageWriteBarrier=False)
            lease['migrationStarted']=True
            self.r.admission_release(lease)
        self.assertEqual(self.events[-2:],['enter_critical','release_after_healthy'])
        self.assertFalse((self.r.STATE/'release-admission.pending.json').exists())

    def test_old_binary_is_explicit_fallback_and_control_error_is_not(self):
        self.legacy=True
        with self.mocks(),self.r.admission_scope(self.header()) as lease:self.assertIsNone(lease)
        self.assertEqual(self.events,['status'])
        self.legacy=False;self.fail='status'
        with self.mocks(),self.assertRaisesRegex(self.r.Refused,'RELEASE_ADMISSION_CONTROL_FAILED'):
            self.r.admission_quiesce(self.header())

    def test_cancel_before_migration_releases_but_preserves_cancellation(self):
        with self.mocks(),self.assertRaises(KeyboardInterrupt):
            with self.r.admission_scope(self.header()):raise KeyboardInterrupt()
        self.assertEqual(self.events[-1],'release_after_healthy')
        self.events=[];self.fail='release_after_healthy'
        with self.mocks(),self.assertRaises(KeyboardInterrupt):
            with self.r.admission_scope(self.header()):raise KeyboardInterrupt()
        self.assertTrue((self.r.STATE/'release-admission.pending.json').exists())

    def test_migration_uncertainty_leaves_owner_and_gate_closed(self):
        with self.mocks(),self.r.admission_scope(self.header()) as lease:
            lease['migrationStarted']=True
        self.assertNotIn('release_after_healthy',self.events)
        self.assertTrue((self.r.STATE/'release-admission.pending.json').exists())

    def test_await_failure_can_reopen_only_via_host_verified_release(self):
        self.fail='await_quiescent'
        with self.mocks(),self.assertRaises(self.r.Refused):self.r.admission_quiesce(self.header())
        self.assertEqual(self.events[-1],'release_after_healthy')

    def test_lost_acquire_response_preserves_shared_owner_and_requires_reconciliation(self):
        spec=importlib.util.spec_from_file_location('actual_admission_gate',Path(__file__).resolve().parents[1]/'server/maintenance_gate.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        pending={};database={'state':'accepting','operationId':None};events=[];lost=[True]
        marker=self.r.STATE/'maintenance-gate.pending.json'
        def journal(value):
            pending.clear();pending.update(value)
            if value.get('phase')=='released':marker.unlink(missing_ok=True)
            else:marker.write_text('private-owner-context');marker.chmod(0o600)
        def bridge(request):
            events.append(request['action'])
            if request['action']=='status':return dict(database,status='ok',capabilities=dict(protocolVersion=2,reversibleHttpAdmission=True))
            if request['action']=='acquire':
                database.update(state='accepting',operationId=request['operationId'],ownerToken=request['ownerToken'],generation='1')
                if lost[0]:lost[0]=False;raise KeyboardInterrupt()
                return dict(database,status='ok')
            if request['action']=='release_after_healthy':
                database.update(state='accepting',operationId=None)
                return dict(database,status='ok')
            self.fail('Unexpected mutation')
        def dispatch(request):return helper.dispatch(request,bridge=bridge,journal_writer=journal,health_check=lambda:True)
        module=types.SimpleNamespace(dispatch=dispatch,GateRefused=helper.GateRefused)
        with patch.object(self.r,'sibling_module',return_value=module),self.assertRaises(KeyboardInterrupt):
            self.r.admission_quiesce(self.header())
        self.assertTrue(marker.exists())
        self.assertEqual(events,['status','acquire'])
        with self.assertRaisesRegex(self.r.Refused,'RELEASE_ADMISSION_RECOVERY_REQUIRED'):self.r.require_host_ready()
        owner=dict(pending)
        recovered=helper.dispatch(dict(owner,action='recover_pending'),bridge=bridge,journal_reader=lambda:dict(pending),journal_writer=journal)
        self.assertEqual(recovered['operationId'],owner['operationId'])
        self.assertTrue(marker.exists()) # Recovered ownership is not permission to reopen.
        helper.dispatch(dict(recovered,action='release_after_healthy',evidence=dict(healthy=True,servicesRestored=True,migrationReviewRequired=False)),
                        bridge=bridge,journal_writer=journal,health_check=lambda:True)
        self.assertFalse(marker.exists())

    def test_runtime_migration_executes_fixed_prisma_without_package_manager_reinstall(self):
        secret='postgresql://PRIVATE_DB_SENTINEL'
        self.r.run_dir=self.r.STATE
        self.r.ops=types.SimpleNamespace(config=lambda:{'services':{'api':{'environment':{'DATABASE_URL':secret}}}},
            isolation=lambda *args:None,fixed_env=lambda:{})
        def atomic(path,value):path.write_bytes(value);path.chmod(0o600)
        for failed in (False,True):
            def command(arguments,timeout):
                self.assertNotIn(secret,str(arguments));self.assertNotIn('pnpm',str(arguments))
                self.assertIn('/app/node_modules/prisma/build/index.js',arguments)
                self.assertEqual(arguments[arguments.index('--entrypoint')+1],'/usr/local/bin/node')
                self.assertIn(secret,(self.r.run_dir/'migration.env').read_text())
                if failed:raise self.r.Refused('MIGRATION_FAILED')
            with patch.object(self.r,'atomic',side_effect=atomic),patch.object(self.r,'command',side_effect=command), \
                 patch.object(self.r.subprocess,'run') as cleanup:
                if failed:
                    with self.assertRaisesRegex(self.r.Refused,'MIGRATION_FAILED'):self.r.migration(self.header(),{'images':{'api':{'tag':'immutable-api'}}})
                else:self.r.migration(self.header(),{'images':{'api':{'tag':'immutable-api'}}})
            self.assertFalse((self.r.run_dir/'migration.env').exists())
            self.assertEqual(cleanup.call_count,1)


if __name__=='__main__':unittest.main()
