import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import cnb_controller as controller
import cnb_release as contract
import tencent_production as production
import tencent_registry as registry


def fixture():
    return dict(schemaVersion=2, deployment='teaching-production', sourceRepository=contract.SOURCE,
                sourceSha='a'*40, sourceTree='b'*40, snapshotSha='c'*40, requestId='d'*32,
                version='0.0.126', timings=dict(api=1, worker=2, web=3, total=6),
                images={kind:dict(ref=contract.REGISTRY+'/'+kind+'@sha256:'+'e'*64, id='sha256:'+'f'*64)
                        for kind in ('api','worker','web')})


class ManifestTests(unittest.TestCase):
    def test_immutable_exact_source_contract(self):
        value=fixture()
        self.assertIs(contract.validate_manifest(value,'a'*40,'0.0.126','c'*40,'d'*32),value)
        for change in (
            lambda x:x.update(sourceSha='e'*40),
            lambda x:x.update(snapshotSha='e'*40),
            lambda x:x.update(requestId='e'*32),
            lambda x:x.update(schemaVersion=True),
            lambda x:x['images']['api'].update(ref=contract.REGISTRY+'/api:latest'),
            lambda x:x['images']['api'].update(ref='docker.io/outsider/api@sha256:'+'e'*64),
            lambda x:x['images'].pop('worker'),
            lambda x:x['timings'].update(total=float('nan')),
        ):
            bad=copy.deepcopy(value);change(bad)
            with self.assertRaises((ValueError,TypeError)):
                contract.validate_manifest(bad,'a'*40,'0.0.126','c'*40,'d'*32)

    def test_receiver_hash_covers_the_entire_original_manifest(self):
        value=fixture();raw=contract.canonical(value)
        h=dict(sourceSha='a'*40,version='0.0.126',bundleBytes=len(raw),bundleSha256=hashlib.sha256(raw).hexdigest())
        def require(ok,code):
            if not ok:raise ValueError(code)
        c=types.SimpleNamespace(require=require,Refused=ValueError)
        self.assertEqual(registry.validate(c,value,h),raw)
        value['timings']['api']=4
        with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):registry.validate(c,value,h)

    def test_same_version_staging_is_bound_to_observed_current_source(self):
        request=production.release_request('stage','b'*40,'0.0.126',dict(version='0.0.126',currentSourceSha='a'*40))
        self.assertEqual(request['expectedCurrentSourceSha'],'a'*40)
        self.assertTrue(request['allowSameVersionSourceChange'])


class ControllerTests(unittest.TestCase):
    def test_cancellation_stops_cnb_and_keeps_secrets_out_of_status(self):
        calls=[];messages=[]
        def api(token,method,path,value=None):
            calls.append((method,path,value))
            if path=='':return dict(visibility_level='Private',path=contract.REPO)
            if path.endswith('/start'):return dict(success=True,sn='build-123')
            if '/status/' in path:return dict(status='running')
            return {}
        with tempfile.TemporaryDirectory() as directory, patch.object(controller,'api',side_effect=api), \
             patch.object(controller,'mirror',return_value=('c'*40,'b'*40)), \
             patch.object(controller.time,'sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                controller.build(Path(directory),'a'*40,'0.0.126',Path(directory),{},None,'SECRET',None,messages.append)
        self.assertEqual(calls[-1][:2],('POST','/-/build/stop/build-123'))
        self.assertNotIn('SECRET',str(messages)+str(calls))

    def test_public_repo_rejected_before_source_sync(self):
        with patch.object(controller,'api',return_value=dict(visibility_level='Public',path=contract.REPO)), \
             patch.object(controller,'mirror') as mirror:
            with self.assertRaises(ValueError):
                controller.build(None,'a'*40,'0.0.126',None,{},None,'SECRET',None,lambda *_:None)
            mirror.assert_not_called()

    def test_snapshot_copies_exact_committed_tree_without_source_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();dest=root/'remote.git'
            def git(*args,cwd=source):
                return subprocess.check_output(['git',*args],cwd=cwd,stderr=subprocess.DEVNULL).decode().strip()
            git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.com')
            (source/'a.txt').write_text('old');git('add','.');git('commit','-qm','old')
            (source/'a.txt').write_text('new');git('commit','-qam','new');sha=git('rev-parse','HEAD')
            (source/'a.txt').write_text('dirty');(source/'private.token').write_text('UNTRACKED_SECRET')
            subprocess.run(['git','init','--bare','-q',str(dest)],check=True)
            private=root/'private';private.mkdir()
            child=production.safe_environment(dict(os.environ),private)
            def run(command,env,log,**kw):
                command=[str(dest) if x=='https://cnb.cool/'+contract.REPO+'.git' else x for x in command]
                return production.run(command,env,log,**kw)
            with (root/'log').open('w+b') as log:
                snapshot,tree=controller.mirror(source,sha,'0.0.126',private,child,log,'SYNTHETIC',run)
            self.assertEqual(git('rev-parse','main:source',cwd=dest),tree)
            self.assertEqual(git('show','main:source/a.txt',cwd=dest),'new')
            self.assertEqual(git('rev-list','--count','main',cwd=dest),'1')
            self.assertNotIn('private.token',git('ls-tree','-r','--name-only','main',cwd=dest))
            self.assertNotEqual(snapshot,sha)
            again=root/'again';again.mkdir()
            with (root/'log2').open('w+b') as log:
                controller.mirror(source,sha,'0.0.126',again,child,log,'SYNTHETIC',run)
            self.assertEqual(git('rev-list','--count','main',cwd=dest),'2')
            self.assertEqual(git('rev-parse','main:source',cwd=dest),tree)


class ReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(__file__).resolve().parents[1]/'server/tencent_receiver.py'
        spec=importlib.util.spec_from_file_location('test_registry_receiver',path)
        cls.receiver=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.receiver)

    def header(self):
        value=fixture();raw=contract.canonical(value)
        return dict(protocol=production.PROTOCOL,action='prepare-registry',sourceRepository=contract.SOURCE,
                    sourceSha='a'*40,version='0.0.126',bundleBytes=len(raw),
                    bundleSha256=hashlib.sha256(raw).hexdigest(),registryManifest=value)

    def test_protocol_rejects_unknown_fields_and_accepts_bound_manifest(self):
        value=self.header()
        self.assertEqual(self.receiver.header(io.BytesIO(contract.canonical(value))),value)
        value['registryPassword']='SECRET'
        with self.assertRaisesRegex(self.receiver.Refused,'FIELDS_REFUSED'):
            self.receiver.header(io.BytesIO(contract.canonical(value)))

    def test_registry_resume_uses_identical_digests_and_no_rebuild(self):
        writes=[]
        c=types.SimpleNamespace(ops=types.SimpleNamespace(fixed_env=lambda:{'PRODUCTION_ACTIVATED':'true'}),
            require=self.receiver.require,Refused=self.receiver.Refused,OPS=Path('/synthetic'),
            atomic=lambda p,v:writes.append((p,v)))
        h=self.header()
        result=registry.prepare(c,h)
        original=writes[-1][1]
        resumed=registry.prepare(c,{k:v for k,v in h.items() if k!='registryManifest'},original)
        self.assertEqual(original['registryManifest'],writes[-1][1]['registryManifest'])
        self.assertEqual(resumed['bundleSha256'],result['bundleSha256'])
        self.assertNotEqual(resumed['transferTicket'],result['transferTicket'])
        self.assertEqual(resumed['status'],'transfer_resumed')

    def test_registry_cannot_bypass_initial_activation_gate(self):
        c=types.SimpleNamespace(ops=types.SimpleNamespace(fixed_env=lambda:{'PRODUCTION_ACTIVATED':'false'}),
            require=self.receiver.require,Refused=self.receiver.Refused)
        with self.assertRaisesRegex(self.receiver.Refused,'ACTIVE_PRODUCTION'):
            registry.prepare(c,self.header())


if __name__=='__main__':unittest.main()
