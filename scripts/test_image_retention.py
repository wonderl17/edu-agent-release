import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('image_retention',Path(__file__).resolve().parents[1]/'server/image_retention.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'production'; self.root.mkdir(mode=0o700)
        self.ops=self.root/'operations/release'; self.ops.mkdir(mode=0o700,parents=True)
        (self.ops/'state').mkdir(mode=0o700); (self.root/'releases').mkdir(mode=0o700)
        self.locks=Path(self.temp.name)/'locks'; self.locks.mkdir(mode=0o700)
        self.images=[]; self.containers=[]; self.commands=[]
        self.target=module.Retention(self.root,uid=os.getuid(),command=self.command,lock_dir=self.locks,clock=lambda:1000)
        for index in range(1,5): self.package(index)

    def write(self,path,value):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        path.write_bytes(module.canonical(value)); path.chmod(0o600)

    def package(self,index,*,state='deployed'):
        char=str(index); sha=char*40; key=char*64; image='sha256:'+char*64
        ref='docker.cnb.cool/shukezhinao/edu-agent-cloud-build/api@'+image
        self.images.append({'Id':image,'RepoDigests':[ref],'RepoTags':[], 'Size':1000,
                            'Config':{'Labels':{'org.opencontainers.image.revision':sha}}})
        base=self.root/'releases'/sha/key
        self.write(base/'manifest.json',dict(sourceSha=sha,images={'api':dict(ref=ref,id=image)}))
        if state=='deployed':
            self.write(base/'deployment-receipt.json',dict(status=state,bundleSha256=key,
                       deployedAt=f'2026-11-0{index}T00:00:00Z',verification={'healthy':True}))
        else:
            self.write(base/(state+'-receipt.json'),dict(status=state,bundleSha256=key))
        return image,ref,key

    def command(self,args):
        self.commands.append(args)
        if args==['ps','-aq']: return ('\n'.join(x['Id'] for x in self.containers)).encode()
        if args[0]=='inspect': return module.canonical(self.containers)
        if args[:2]==['image','ls']: return ('\n'.join(x['Id'] for x in self.images)).encode()
        if args[:2]==['image','inspect']:
            return module.canonical([x for x in self.images if x['Id'] in args[2:]])
        if args[:2]==['image','rm']:
            self.images=[x for x in self.images if not set(args[2:]) & set(x['RepoDigests']+x['RepoTags'])]
            return b''
        raise AssertionError(args)

    def save_plan(self):
        plan=self.target.make_plan(); path=self.ops/'plan.json'; self.write(path,plan)
        return plan,path,module.digest(path.read_bytes())

    def test_current_and_previous_success_and_all_containers_are_protected(self):
        self.containers=[dict(Id='a'*64,Image='sha256:'+'1'*64,State={'StartedAt':'fixed'})]
        plan=self.target.make_plan()
        self.assertEqual([x['id'] for x in plan['images']],['sha256:'+'2'*64])
        self.assertTrue(any(x['id']=='sha256:'+'1'*64 and 'container' in x['reasons'] for x in plan['protected']))

    def test_apply_only_reviewed_references_without_force_or_prune(self):
        plan,path,sha=self.save_plan()
        result=self.target.apply_plan(path,sha)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(result['images']),2)
        self.assertEqual(len(self.images),2)
        self.assertFalse(any('prune' in x or '--force' in x for x in self.commands))
        self.assertTrue((self.root/'releases'/('1'*40)/('1'*64)/'manifest.json').exists())

    def test_new_container_or_policy_invalidates_reviewed_plan(self):
        for mutation in ('container','policy'):
            with self.subTest(mutation=mutation):
                _,path,sha=self.save_plan()
                if mutation=='container':
                    self.containers=[dict(Id='b'*64,Image='sha256:'+'2'*64,State={'StartedAt':'now'})]
                else:
                    self.write(self.ops/'image-retention-policy.json',dict(schemaVersion=1,retainSuccessfulPackages=3,
                        protectedPackageHashes=[],releasedPackageHashes=[]))
                with self.assertRaisesRegex(ValueError,'RETENTION_PLAN_STALE'): self.target.apply_plan(path,sha)
                self.assertFalse(any(x[:2]==['image','rm'] for x in self.commands))
                self.containers=[]

    def test_pending_operation_reports_but_prevents_deletion(self):
        self.write(self.ops/'state/release-operation.pending.json',dict(sourceSha='1'*40))
        plan,path,sha=self.save_plan(); self.assertTrue(plan['pending'])
        with self.assertRaisesRegex(ValueError,'RETENTION_RECOVERY_PENDING'): self.target.apply_plan(path,sha)

    def test_staged_failed_unowned_and_unknown_aliases_are_retained(self):
        for state in ('stage','failure'):
            self.package(5 if state=='stage' else 6,state=state)
        self.images.append(dict(Id='sha256:'+'a'*64,RepoDigests=[],RepoTags=['foreign:latest'],Config={}))
        self.images[0]['RepoTags']=['unrelated:alias']
        plan=self.target.make_plan()
        self.assertEqual([x['id'] for x in plan['images']],['sha256:'+'2'*64])

    def test_expired_or_tampered_plan_is_refused(self):
        _,path,sha=self.save_plan()
        with self.assertRaisesRegex(ValueError,'RETENTION_PLAN_HASH'): self.target.apply_plan(path,'f'*64)
        self.target.clock=lambda:1700
        with self.assertRaisesRegex(ValueError,'RETENTION_PLAN_EXPIRED'): self.target.apply_plan(path,sha)

    def test_symlink_metadata_and_out_of_scope_output_are_refused(self):
        link=self.ops/'state/last-success.json'; link.symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError,'RETENTION_METADATA_UNSAFE'): self.target.make_plan()
        with self.assertRaisesRegex(ValueError,'RETENTION_SCOPE_REFUSED'): self.target.write(Path(self.temp.name)/'outside',{})

    def test_uncertain_deletion_retains_journal_and_does_not_continue(self):
        _,path,sha=self.save_plan(); original=self.target.command
        def command(args):
            if args[:2]==['image','rm']: raise OSError('private Docker details')
            return original(args)
        self.target.command=command
        with self.assertRaises(OSError): self.target.apply_plan(path,sha)
        receipt=json.loads((self.ops/'state/image-retention'/(sha+'.json')).read_text())
        self.assertEqual(receipt['status'],'in_progress')
        self.assertEqual(receipt['images'][0]['status'],'deletion_started')
        self.assertEqual(len(receipt['images']),1)

    def test_incomplete_or_duplicate_container_inventory_cannot_form_a_deletion_plan(self):
        self.containers=[dict(Id='a'*64,Image='sha256:'+'1'*64,State={'StartedAt':'fixed'}),
                         dict(Id='b'*64,Image='sha256:'+'2'*64,State={'StartedAt':'fixed'})]
        original=self.target.command
        for result in ([self.containers[0]], [self.containers[0], self.containers[0]]):
            with self.subTest(result=result):
                self.target.command=lambda args: module.canonical(result) if args[0]=='inspect' else original(args)
                with self.assertRaisesRegex(ValueError,'RETENTION_(INSPECTION_INCOMPLETE|CONTAINER_SET_CHANGED)'):
                    self.target.make_plan()
        self.assertFalse(any(x[:2]==['image','rm']for x in self.commands))

    def test_shared_layers_and_package_pin_are_conservative_not_promised_capacity(self):
        first=self.root/'releases'/('1'*40)/('1'*64)/'manifest.json'
        self.write(first,dict(sourceSha='1'*40,images={'api':dict(ref=self.images[3]['RepoDigests'][0],id=self.images[3]['Id'])}))
        plan=self.target.make_plan()
        self.assertNotIn(self.images[3]['Id'],[x['id']for x in plan['images']])
        self.assertTrue(all('logicalSizeBytes'in x for x in plan['protected']))

    def test_common_gate_pending_and_corrupt_success_receipt_fail_closed(self):
        marker=self.ops/'state/maintenance-gate.pending.json';self.write(marker,dict(operationId='uncertain-acquire'))
        _,path,sha=self.save_plan()
        with self.assertRaisesRegex(ValueError,'RETENTION_RECOVERY_PENDING'):self.target.apply_plan(path,sha)
        marker.unlink()
        receipt=self.root/'releases'/('1'*40)/('1'*64)/'deployment-receipt.json'
        self.write(receipt,dict(status='unknown',bundleSha256='1'*64))
        with self.assertRaisesRegex(ValueError,'RETENTION_RECEIPT_INVALID'):self.target.make_plan()

    def test_legacy_unbound_receipt_is_explicitly_pinned_not_counted_as_success(self):
        path=self.root/'releases'/('1'*40)/('1'*64)/'deployment-receipt.json'
        self.write(path,dict(status='deployed',sourceSha='1'*40,deployedAt='2100-01-01T00:00:00Z',verification={'healthy':True}))
        self.write(self.ops/'image-retention-policy.json',dict(schemaVersion=1,retainSuccessfulPackages=2,
            protectedPackageHashes=[],releasedPackageHashes=['1'*64]))
        plan=self.target.make_plan()
        self.assertEqual([x['id']for x in plan['images']],['sha256:'+'2'*64])
        row=next(x for x in plan['protected']if x['id']=='sha256:'+'1'*64)
        self.assertIn('unverified-receipt:'+'1'*64,row['reasons'])
        self.assertEqual(next(x for x in plan['packages']if x['packageHash']=='1'*64)['receiptState'],'unverified')

    def test_unknown_receipt_schema_is_pinned_but_corruption_or_identity_conflicts_refuse(self):
        path=self.root/'releases'/('1'*40)/('1'*64)/'deployment-receipt.json'
        legacy=dict(schemaVersion=99,status='deployed',sourceSha='1'*40,bundleSha256='1'*64,
            deployedAt='2026-11-01T00:00:00Z',verification={'healthy':True})
        self.write(path,legacy)
        self.assertNotIn('sha256:'+'1'*64,[x['id']for x in self.target.make_plan()['images']])
        for field,value in [('sourceSha','f'*40),('bundleSha256','f'*64)]:
            self.write(path,dict(legacy,**{field:value}))
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'RETENTION_RECEIPT_IDENTITY'):self.target.make_plan()
        path.write_bytes(b'{broken json');path.chmod(0o600)
        with self.assertRaises(ValueError):self.target.make_plan()
        self.assertFalse(any(x[:2]==['image','rm']for x in self.commands))

    def test_active_transfer_overrides_explicit_release_and_expired_transfer_needs_review(self):
        policy=dict(schemaVersion=1,retainSuccessfulPackages=2,protectedPackageHashes=[],releasedPackageHashes=['1'*64])
        self.write(self.ops/'image-retention-policy.json',policy)
        transfer=self.ops/'transfers'/('a'*32+'.json')
        self.write(transfer,dict(bundleSha256='1'*64,used=False,expiresAt='2100-01-01T00:00:00Z'))
        self.assertNotIn('sha256:'+'1'*64,[x['id']for x in self.target.make_plan()['images']])
        self.write(transfer,dict(bundleSha256='1'*64,used=False,expiresAt='1970-01-01T00:00:00Z'))
        self.assertIn('sha256:'+'1'*64,[x['id']for x in self.target.make_plan()['images']])


if __name__=='__main__': unittest.main()
