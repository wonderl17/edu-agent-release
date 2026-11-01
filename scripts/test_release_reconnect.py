"""Offline fault injection at the receiver boundary; no Docker or SSH calls."""
import importlib.util
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

import cnb_release as contract
import tencent_registry as registry


def load():
    path=Path(__file__).resolve().parents[1]/'server/tencent_receiver.py'
    spec=importlib.util.spec_from_file_location('release_reconnect_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class ReleaseReconnectTests(unittest.TestCase):
    def setUp(self):
        self.r=load();self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        root=Path(self.directory.name).resolve();self.r.OPS=root/'operations';self.r.STATE=root/'state';self.r.RELEASES=root/'releases'
        self.identity={'sourceSha':'a'*40,'version':'9.2.1','bundleBytes':321,'bundleSha256':'b'*64}
        self.ticket='c'*32;self.target=self.r.RELEASES/self.identity['sourceSha']/self.identity['bundleSha256']
        self.receipt={**self.identity,'status':'deployed','deployedAt':'2026-11-01T01:00:00Z','verification':{'healthy':True,'images':{
            'api':{'targetId':'sha256:'+'d'*64},'worker':{'targetId':'sha256:'+'e'*64}}},'privateDetail':'DO_NOT_PUBLISH'}
        self.containers={name:{'Image':self.receipt['verification']['images'][kind]['targetId']}
                         for name,kind in(('api','api'),('worker-general','worker'),('worker-ppt','worker'))}
        self.containers['web']={'Mounts':[{'Destination':'/usr/share/nginx/html','Source':str(self.target/'web-dist'),'RW':False}]}
        self.r.ops=types.SimpleNamespace(snapshot=lambda:{'healthy':True})
        # Exercise real private files on any host, retaining all checks except
        # root ownership (the production implementation still requires root).
        def private(path,*_):
            path=Path(path);info=path.lstat()
            self.r.require(stat.S_ISREG(info.st_mode)and not path.is_symlink()and path.resolve()==path and
                           stat.S_IMODE(info.st_mode)==0o600,'UNSAFE_PRIVATE_FILE')
        self.private=patch.object(self.r,'private',side_effect=private);self.private.start();self.addCleanup(self.private.stop)

    def write(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value));path.chmod(0o600)

    def bound_ticket(self):
        self.write(self.r.OPS/'transfers'/(self.ticket+'.json'),{
            **self.identity,'schemaVersion':1,'scope':'teaching-production','sourceRepository':self.r.SOURCE,
            'ticket':self.ticket,'used':True,'expiresAt':'2000-01-01T00:00:00Z'})
        return{'action':'release-status','sourceSha':self.identity['sourceSha'],'version':self.identity['version'],
               'previousTransferTicket':self.ticket}

    def status(self,h=None):
        with patch.object(self.r,'atomic',side_effect=AssertionError('status must not write')),\
             patch.object(self.r,'artifact_directory',side_effect=AssertionError('status must not mkdir')),\
             patch.object(self.r,'start_liveness',side_effect=AssertionError('status has no cancellation monitor')):
            return self.r.release_status(h or self.identity)

    def runtime(self):
        return patch.multiple(self.r,current=lambda:({'version':self.identity['version'],'buildSha':self.identity['sourceSha']},self.containers),
                              command=lambda *args,**kwargs:json.dumps({'version':self.identity['version'],'buildSha':self.identity['sourceSha']}).encode())

    def legacy_failure(self, **changes):
        # Synthetic values with the complete pre-package-identity schema.
        value=dict(status='failed', code='DEPLOY_FAILED_ROLLBACK_RESTORED', causeCode='CLIENT_DISCONNECTED',
                    rollbackCauseCode=None, phase='drain', sourceSha=self.identity['sourceSha'], version=self.identity['version'],
                    snapshotId=None, configurationRollbackSucceeded=True, databaseAutomaticallyRestored=False,
                    migrationStarted=False, migrationSucceeded=False, manualDatabaseReviewRequired=False,
                    failedAt='2026-11-01T14:00:00Z')
        value.update(changes);return value

    def test_readonly_protocol_accepts_ticket_or_explicit_identity_and_rejects_ambiguity(self):
        for h in(self.bound_ticket(),dict(self.identity,action='release-status')):
            request=dict(h,protocol=self.r.PROTOCOL,sourceRepository=self.r.SOURCE)
            self.assertEqual(self.r.header(io.BytesIO(json.dumps(request).encode()+b'\n')),request)
        bad=dict(self.bound_ticket(),bundleBytes=321,bundleSha256='b'*64,
                 protocol=self.r.PROTOCOL,sourceRepository=self.r.SOURCE)
        with self.assertRaisesRegex(self.r.Refused,'FIELDS_REFUSED'):
            self.r.header(io.BytesIO(json.dumps(bad).encode()+b'\n'))

    def test_used_expired_ticket_is_read_only_identity_and_absent_does_not_create_release_tree(self):
        request=self.bound_ticket();before=sorted(str(p)for p in Path(self.directory.name).rglob('*'))
        value=self.status(request)
        self.assertEqual(value,{'status':'release_status',**self.identity,'state':'absent','recoveryRequired':False})
        self.assertEqual(before,sorted(str(p)for p in Path(self.directory.name).rglob('*')))
        self.assertTrue(json.loads((self.r.OPS/'transfers'/(self.ticket+'.json')).read_text())['used'])

    def test_missing_or_foreign_ticket_cannot_guess_a_package(self):
        request=self.bound_ticket();request['previousTransferTicket']='f'*32
        with self.assertRaisesRegex(self.r.Refused,'TRANSFER_TICKET_NOT_FOUND'):self.status(request)
        request=self.bound_ticket();request['sourceSha']='f'*40
        with self.assertRaisesRegex(self.r.Refused,'TRANSFER_TICKET_IDENTITY_MISMATCH'):self.status(request)

    def test_disconnect_during_migration_retains_pending_even_if_success_receipt_exists(self):
        self.write(self.target/'deployment-receipt.json',self.receipt)
        marker=self.r.STATE/'release-operation.pending.json';self.write(marker,{**self.identity,'phase':'migration_started'})
        original=marker.read_bytes()
        with patch.object(self.r,'current',side_effect=AssertionError('pending is not complete')):
            value=self.status(self.bound_ticket())
        self.assertEqual((value['state'],value['phase'],value['recoveryRequired']),('pending','migration_started',True))
        self.assertEqual(marker.read_bytes(),original)

    def test_bound_success_is_reverified_without_repeating_any_mutation_and_drops_private_fields(self):
        self.write(self.target/'deployment-receipt.json',self.receipt)
        with self.runtime(),patch.object(self.r,'migration',side_effect=AssertionError('no second migration')):
            value=self.status(self.bound_ticket())
        self.assertEqual(value['state'],'deployed');self.assertTrue(value['verification']['healthy'])
        self.assertNotIn('DO_NOT_PUBLISH',json.dumps(value));self.assertNotIn('ticket',json.dumps(value))
        self.assertNotIn('images',value['verification'])

    def test_repeat_deploy_returns_current_success_without_transfer_stop_or_migration(self):
        self.write(self.target/'deployment-receipt.json',self.receipt)
        with self.runtime(),patch.object(self.r,'require_host_ready'),\
             patch.object(self.r,'registry_handler',side_effect=AssertionError('no pull or ticket consumption')),\
             patch.object(self.r,'stop_api_guarded',side_effect=AssertionError('no repeated stop')),\
             patch.object(self.r,'migration',side_effect=AssertionError('no repeated migration')):
            value=self.r.deploy(self.identity,object())
        self.assertEqual(value['status'],'deployed');self.assertTrue(value['alreadyDeployed'])

    def test_legacy_unbound_receipt_and_conflicting_hash_are_not_replayed_or_called_success(self):
        for changes,code in(({'bundleSha256':'f'*64},'IDENTITY_MISMATCH'),({'bundleBytes':None},'IDENTITY_MISMATCH')):
            with self.subTest(changes=changes):
                self.write(self.target/'deployment-receipt.json',dict(self.receipt,**changes))
                with self.assertRaisesRegex(self.r.Refused,code):self.status()
        legacy=dict(self.receipt);legacy.pop('bundleSha256');self.write(self.target/'deployment-receipt.json',legacy)
        with self.assertRaisesRegex(self.r.Refused,'IDENTITY_MISSING'):self.status()

    def test_receipt_cannot_mask_unhealthy_or_different_running_image(self):
        self.write(self.target/'deployment-receipt.json',self.receipt)
        self.containers['api']['Image']='sha256:'+'f'*64
        with self.runtime(),self.assertRaisesRegex(self.r.Refused,'RUNNING_IMAGE_ID_MISMATCH'):self.status()
        self.r.ops.snapshot=lambda:{'healthy':False}
        with self.runtime(),self.assertRaisesRegex(self.r.Refused,'TARGET_UNHEALTHY'):self.status()

    def test_staged_and_failed_states_remain_bound_to_exact_package(self):
        self.write(self.target/'stage-receipt.json',dict(self.identity,status='staged',transferTicket=self.ticket))
        self.assertEqual(self.status()['state'],'staged')
        (self.target/'stage-receipt.json').unlink()
        self.write(self.r.STATE/'last-failure.json',dict(self.identity,configurationRollbackSucceeded=True,manualDatabaseReviewRequired=False))
        self.assertEqual((self.status()['state'],self.status()['recoveryRequired']),('failed',False))
        self.write(self.r.STATE/'last-failure.json',dict(self.identity,configurationRollbackSucceeded=True,manualDatabaseReviewRequired=True))
        self.assertTrue(self.status()['recoveryRequired'])

    def test_failure_after_migration_outranks_stage_even_after_another_package_failed(self):
        self.write(self.target/'stage-receipt.json',dict(self.identity,status='staged'))
        failure=dict(self.identity,configurationRollbackSucceeded=True,manualDatabaseReviewRequired=True,failedAt='2026-11-01T02:00:00Z')
        self.write(self.target/'failure-receipt.json',failure)
        self.write(self.r.STATE/'last-failure.json',dict(failure,bundleSha256='f'*64))
        value=self.status();self.assertEqual(value['state'],'failed');self.assertTrue(value['recoveryRequired'])

    def test_loss_after_global_success_receipt_does_not_repeat_migration(self):
        self.write(self.r.STATE/'last-success.json',self.receipt)
        with self.runtime():
            value=self.status(self.bound_ticket())
        self.assertEqual(value['state'],'deployed');self.assertTrue(value['verification']['healthy'])
        self.assertFalse((self.target/'deployment-receipt.json').exists())

    def test_unbound_old_migration_failure_cannot_be_masked_by_stage(self):
        self.write(self.target/'stage-receipt.json',dict(self.identity,status='staged'))
        self.write(self.r.STATE/'last-failure.json',{'sourceSha':self.identity['sourceSha'],'version':self.identity['version'],
                                                  'manualDatabaseReviewRequired':True})
        with self.assertRaisesRegex(self.r.Refused,'RELEASE_RECEIPT_IDENTITY_MISSING'):self.status()

    def test_complete_legacy_cancelled_deploy_reports_restored_failure_without_stage_or_rewriting_identity(self):
        for locations in (('global',), ('package',), ('global', 'package')):
            with self.subTest(locations=locations):
                for path in (self.r.STATE/'last-failure.json', self.target/'failure-receipt.json'):
                    path.unlink(missing_ok=True)
                failure=self.legacy_failure()
                paths=[self.r.STATE/'last-failure.json' if place=='global' else self.target/'failure-receipt.json' for place in locations]
                for path in paths:self.write(path,failure)
                before={path:path.read_bytes()for path in paths}
                value=self.status(self.bound_ticket())
                self.assertEqual(value,dict(status='release_status',**self.identity,state='failed',recoveryRequired=False))
                self.assertEqual(before,{path:path.read_bytes()for path in paths})
                self.assertFalse((self.target/'stage-receipt.json').exists())
                self.assertFalse((self.target/'deployment-receipt.json').exists())

    def test_complete_legacy_idle_timeout_is_a_restored_failure_not_a_success(self):
        self.write(self.target/'failure-receipt.json',self.legacy_failure(causeCode='API_DRAIN_CONNECTION_TIMEOUT'))
        value=self.status(self.bound_ticket())
        self.assertEqual((value['state'],value['recoveryRequired']),('failed',False))
        self.assertNotIn('verification',value)

    def test_legacy_failure_requires_every_field_and_refuses_unsafe_or_partial_identity(self):
        failure=self.legacy_failure()
        for key in failure:
            with self.subTest(missing=key):
                value=dict(failure);value.pop(key)
                self.assertFalse(self.r.restored_legacy_drain_failure(value,self.identity))
                if key not in ('sourceSha','version'):
                    self.write(self.r.STATE/'last-failure.json',value)
                    with self.assertRaisesRegex(self.r.Refused,'RELEASE_RECEIPT_IDENTITY_MISSING'):self.status()
        for fields in ({'status':'deployed'}, {'code':'DEPLOY_FAILED_ROLLBACK_INCOMPLETE'}, {'causeCode':'API_DRAIN_STOP_UNCERTAIN'},
                       {'phase':'migration'}, {'phase':'worker-stop'}, {'migrationStarted':True}, {'migrationSucceeded':True},
                       {'manualDatabaseReviewRequired':True}, {'configurationRollbackSucceeded':False}, {'rollbackCauseCode':'UNKNOWN'},
                       {'databaseAutomaticallyRestored':True}, {'snapshotId':'unknown'}, {'failedAt':'invalid'},
                       {'configurationRollbackSucceeded':1}, {'migrationStarted':0}, {'bundleBytes':321}, {'bundleSha256':'b'*64}):
            with self.subTest(fields=fields):
                value=dict(failure,**fields)
                self.assertFalse(self.r.restored_legacy_drain_failure(value,self.identity))
                self.write(self.r.STATE/'last-failure.json',value)
                with self.assertRaises(self.r.Refused):self.status()
        for fields in ({'sourceSha':'f'*40}, {'version':'0.0.135'}):
            self.assertFalse(self.r.restored_legacy_drain_failure(dict(failure,**fields),self.identity))
            request=dict(self.bound_ticket(),**fields)
            with self.assertRaisesRegex(self.r.Refused,'TRANSFER_TICKET_IDENTITY_MISMATCH'):self.status(request)

    def test_unsafe_legacy_package_failure_cannot_be_rescued_by_a_safe_global_record(self):
        self.write(self.r.STATE/'last-failure.json',self.legacy_failure())
        for fields in ({'phase':'migration'}, {'migrationStarted':True}, {'configurationRollbackSucceeded':False},
                       {'sourceSha':'f'*40}, {'version':'9.2.2'}, {'bundleBytes':321}):
            with self.subTest(fields=fields):
                self.write(self.target/'failure-receipt.json',self.legacy_failure(**fields))
                with self.assertRaisesRegex(self.r.Refused,'RELEASE_RECEIPT_IDENTITY_MISMATCH'):self.status()

    def test_safe_legacy_failure_never_masks_package_migration_failure_or_legacy_success(self):
        self.write(self.target/'failure-receipt.json',dict(self.identity,configurationRollbackSucceeded=True,
                   manualDatabaseReviewRequired=True,failedAt='2026-11-01T01:00:00Z'))
        self.write(self.r.STATE/'last-failure.json',self.legacy_failure())
        self.assertTrue(self.status()['recoveryRequired'])
        (self.target/'failure-receipt.json').unlink()
        legacy=dict(self.receipt);legacy.pop('bundleBytes');legacy.pop('bundleSha256')
        self.write(self.r.STATE/'last-success.json',legacy)
        with self.assertRaisesRegex(self.r.Refused,'RELEASE_RECEIPT_IDENTITY_MISSING'):self.status()

    def test_restored_legacy_deploy_can_prepare_registry_resume_with_the_exact_original_manifest(self):
        manifest=dict(schemaVersion=3,deployment='teaching-production',sourceRepository=self.r.SOURCE,
                      sourceSha=self.identity['sourceSha'],sourceTree='1'*40,snapshotSha='2'*40,requestId='3'*32,
                      version=self.identity['version'],runtime=dict(schemaVersion=1,protocol='teaching-codex-plan-v1',sdkVersion='0.165.0'),
                      timings=dict(api=1,worker=1,web=1,**{'codex-runtime':1,'total':4}),
                      images={kind:dict(ref=contract.REGISTRY+'/'+kind+'@sha256:'+'4'*64,id='sha256:'+'5'*64)
                              for kind in ('api','worker','web','codex-runtime')})
        raw=contract.canonical(manifest)
        self.identity.update(bundleBytes=len(raw),bundleSha256=hashlib.sha256(raw).hexdigest())
        self.target=self.r.RELEASES/self.identity['sourceSha']/self.identity['bundleSha256']
        request=self.bound_ticket();path=self.r.OPS/'transfers'/(self.ticket+'.json')
        record=json.loads(path.read_text());record.update(transport='registry',registryManifest=manifest);self.write(path,record)
        self.write(self.r.STATE/'last-failure.json',self.legacy_failure())
        self.write(self.target/'failure-receipt.json',self.legacy_failure())
        self.assertEqual((self.status(request)['state'],self.status(request)['recoveryRequired']),('failed',False))
        self.r.ops.fixed_env=lambda:{'PRODUCTION_ACTIVATED':'true'}
        context=types.SimpleNamespace(ops=self.r.ops,Refused=self.r.Refused,require=self.r.require,atomic=self.write,OPS=self.r.OPS)
        resume=dict(action='resume-transfer',sourceRepository=self.r.SOURCE,sourceSha=self.identity['sourceSha'],
                    version=self.identity['version'],previousTransferTicket=self.ticket)
        with patch.object(self.r,'current',return_value=({'version':'0.0.133','buildSha':'6'*40},{})),\
             patch.object(self.r,'registry_handler',return_value=(registry,context)):
            value=self.r.resume_transfer(resume)
            self.assertEqual(value['status'],'transfer_resumed')
            fresh=json.loads((self.r.OPS/'transfers'/(value['transferTicket']+'.json')).read_text())
            self.assertEqual(fresh['registryManifest'],manifest);self.assertFalse(fresh['used'])
            self.assertEqual((fresh['bundleBytes'],fresh['bundleSha256']),(len(raw),hashlib.sha256(raw).hexdigest()))
            self.assertTrue(json.loads(path.read_text())['used'])
            bad=copy.deepcopy(record);bad['registryManifest']['timings']['api']=2;self.write(path,bad)
            with self.assertRaisesRegex(self.r.Refused,'REGISTRY_MANIFEST_HASH_MISMATCH'):self.r.resume_transfer(resume)
            self.write(path,dict(record,sourceSha='f'*40))
            with self.assertRaisesRegex(self.r.Refused,'TRANSFER_TICKET_IDENTITY_MISMATCH'):self.r.resume_transfer(resume)

    def test_newer_migration_failure_overrides_stale_success_but_later_success_can_finish(self):
        failure=dict(self.identity,configurationRollbackSucceeded=True,manualDatabaseReviewRequired=True,failedAt='2026-11-01T02:00:00Z')
        self.write(self.target/'deployment-receipt.json',self.receipt);self.write(self.target/'failure-receipt.json',failure)
        with self.runtime():
            self.assertEqual(self.status()['state'],'failed');self.assertTrue(self.status()['recoveryRequired'])
        self.write(self.target/'deployment-receipt.json',dict(self.receipt,deployedAt='2026-11-01T03:00:00Z'))
        with self.runtime():self.assertEqual(self.status()['state'],'deployed')

    def test_receipt_order_missing_or_equal_cannot_mask_a_migration_failure(self):
        self.write(self.target/'deployment-receipt.json',self.receipt)
        failure=dict(self.identity,configurationRollbackSucceeded=True,manualDatabaseReviewRequired=True)
        self.write(self.target/'failure-receipt.json',failure)
        with self.assertRaisesRegex(self.r.Refused,'RELEASE_RECEIPT_ORDER_UNCERTAIN'):self.status()
        self.write(self.target/'failure-receipt.json',dict(failure,failedAt=self.receipt['deployedAt']))
        self.assertTrue(self.status()['recoveryRequired'])

    def test_direct_redeploy_cannot_call_stale_success_complete_after_new_migration_failure(self):
        self.write(self.target/'deployment-receipt.json',self.receipt)
        self.write(self.target/'failure-receipt.json',dict(self.identity,configurationRollbackSucceeded=True,
                   manualDatabaseReviewRequired=True,failedAt='2026-11-01T02:00:00Z'))
        with patch.object(self.r,'require_host_ready'),patch.object(self.r,'migration',side_effect=AssertionError('no replay')):
            with self.assertRaisesRegex(self.r.Refused,'RELEASE_OPERATION_RECOVERY_REQUIRED'):self.r.deploy(self.identity,object())

    def test_broken_pending_symlink_is_not_treated_as_absent(self):
        self.r.STATE.mkdir();(self.r.STATE/'api-drain.pending.json').symlink_to(self.r.STATE/'missing')
        with self.assertRaisesRegex(self.r.Refused,'UNSAFE_PRIVATE_FILE'):self.status()

    def test_pending_owner_requires_same_boot_pid_and_process_start(self):
        record={'bootId':'same-boot','ownerPid':42,'ownerStart':123}
        raw='42 (synthetic command) '+' '.join(['S']+['0']*18+['123'])
        with patch.object(self.r,'process_identity',return_value={'bootId':'same-boot'}),\
             patch.object(self.r,'Path',return_value=types.SimpleNamespace(read_text=lambda:raw)):
            self.assertTrue(self.r.pending_owner_active(record))
            self.assertFalse(self.r.pending_owner_active(dict(record,ownerStart=124)))
            self.assertFalse(self.r.pending_owner_active(dict(record,bootId='old-boot')))
            self.assertFalse(self.r.pending_owner_active(dict(record,ownerPid='../unsafe')))


if __name__=='__main__':unittest.main()
