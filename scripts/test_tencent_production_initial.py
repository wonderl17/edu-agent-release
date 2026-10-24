"""A root-issued initial gate is a narrow resume capability, never healthy runtime proof."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tencent_production as target

class InitialActivationTests(unittest.TestCase):
    GATE='b'*32
    HASH='c'*64
    TICKET='d'*32
    SECRET='PRIVATE_INITIAL_SENTINEL'

    def state(self):
        return {'status':'verified','configValid':True,'bootstrapReady':True,'healthy':False,'phase':'bootstrap',
                'currentSourceSha':None,'baselineSourceSha':target.INITIAL_SOURCE_SHA,'version':target.INITIAL_VERSION,
                'lastAttemptedVersion':target.INITIAL_VERSION,'initialActivationReady':True,'initialGateId':self.GATE,
                'initialBundleBytes':123,'initialBundleSha256':self.HASH}

    def fixture(self, *, state=None, ticket=None, result=None, version='0.0.125', operation='resume'):
        calls=[];metadata=[];authenticated=[];output=io.StringIO()
        env={'PUBLIC_REPOSITORY':target.PUBLIC,'GITHUB_REF':'refs/heads/main','GITHUB_SHA':'a'*40,
             'TENCENT_OPERATION':operation,'TENCENT_SOURCE_SHA':target.INITIAL_SOURCE_SHA,
             'TENCENT_CONFIRM_RELEASE':'yes','RELEASES_ENABLED':'true','TENCENT_PRODUCTION_ENABLED':'true',
             'TENCENT_PRODUCTION_DEPLOY_SSH_KEY':'-----BEGIN OPENSSH PRIVATE KEY-----\n'+self.SECRET,
             'TENCENT_PRODUCTION_SSH_KNOWN_HOSTS':target.HOST+' ssh-ed25519 SYNTHETIC',
             'PUBLIC_GH_TOKEN':self.SECRET}
        if operation=='resume':env['TENCENT_PREVIOUS_TRANSFER_TICKET']='e'*32
        def source(sha,token,path,child,log):
            self.assertEqual(sha,target.INITIAL_SOURCE_SHA);(path/'package.json').write_text(json.dumps({'version':version}))
        def auth(*args,**kwargs):authenticated.append(True);return self.SECRET
        def github(token,method,path,body=None):
            if method=='DELETE':return {}
            if path.endswith('/statuses'):return {}
            metadata.append(body);return {'id':123}
        def remote(ssh,child,log,request,payload=None):
            calls.append(dict(request));action=request['action']
            if action=='verify':return self.state() if state is None else state
            if action=='resume-transfer':
                self.assertEqual(set(request),{'action','sourceSha','version','previousTransferTicket','initialGateId'});self.assertEqual(request['initialGateId'],self.GATE)
                return ticket or {'status':'transfer_resumed','transferTicket':self.TICKET,'bundleBytes':123,'bundleSha256':self.HASH}
            if action=='deploy':
                self.assertNotIn('allowSameVersionSourceChange',request);self.assertNotIn('expectedCurrentSourceSha',request);self.assertEqual(request['initialGateId'],self.GATE)
                self.assertEqual((request['bundleBytes'],request['bundleSha256'],request['transferTicket']),(123,self.HASH,self.TICKET))
                return result or {'status':'deployed','sourceSha':target.INITIAL_SOURCE_SHA,'version':target.INITIAL_VERSION,
                                  'initialActivation':True,'gateId':self.GATE,'verification':{'healthy':True}}
            if action=='diagnostics':return {}
            raise AssertionError('Initial activation used ordinary record-attempt or build/transfer preparation')
        failure=None
        with tempfile.TemporaryDirectory() as directory:
            env['RUNNER_TEMP']=directory
            with patch.object(target,'installation_token',side_effect=auth),patch.object(target,'clone_source',side_effect=source), \
                 patch.object(target,'remote',side_effect=remote),patch.object(target,'github_request',side_effect=github), \
                 patch.object(target,'build_bundle',side_effect=AssertionError('unexpected build')), \
                 patch.object(target,'upload_bundle',side_effect=AssertionError('unexpected upload')), \
                 patch.object(target.subprocess,'Popen',side_effect=AssertionError('unexpected subprocess')), \
                 patch.object(target,'urlopen',side_effect=AssertionError('unexpected HTTP')),contextlib.redirect_stdout(output):
                try:target.execute(env)
                except target.Failure as error:failure=error
            self.assertEqual(list(Path(directory).iterdir()),[])
        self.assertNotIn(self.SECRET,output.getvalue())
        return calls,metadata,authenticated,failure

    def test_root_gate_permits_only_initial_exact_package_resume_and_records_github_attempt(self):
        calls,metadata,auth,failure=self.fixture();self.assertIsNone(failure);self.assertTrue(auth)
        self.assertEqual([x['action'] for x in calls],['verify','resume-transfer','deploy'])
        self.assertEqual((metadata[0]['task'],metadata[0]['payload']['operation']),('release-cloud','cloud'))
        self.assertTrue(metadata[0]['production_environment'])

    def test_malformed_incomplete_or_wrong_source_gate_is_refused_before_source_access(self):
        for changes in ({'initialActivationReady':False},{'initialGateId':'../x'},{'initialBundleBytes':True},
                        {'initialBundleBytes':0},{'initialBundleBytes':6*1024**3},{'initialBundleSha256':'x'},
                        {'baselineSourceSha':'f'*40},{'version':'0.0.126'},{'currentSourceSha':'f'*40},
                        {'phase':'active'},{'configValid':False},{'bootstrapReady':False},{'healthy':None}):
            with self.subTest(changes=changes):
                calls,metadata,auth,failure=self.fixture(state=dict(self.state(),**changes));self.assertIsInstance(failure,target.Failure)
                self.assertEqual(failure.code,44);self.assertFalse(auth);self.assertEqual(metadata,[])
                self.assertNotIn('deploy',[x['action'] for x in calls])

    def test_initial_gate_cannot_authorize_ordinary_deploy_or_readonly_verify(self):
        for operation in ('deploy','verify'):
            calls,metadata,auth,failure=self.fixture(operation=operation);self.assertIsInstance(failure,target.Failure)
            self.assertFalse(auth);self.assertEqual(metadata,[])
        self.assertFalse(target.initial_activation('automatic',target.INITIAL_SOURCE_SHA,self.state()))
        self.assertFalse(target.initial_activation('resume','f'*40,self.state()))

    def test_private_source_package_version_must_match_initial_gate(self):
        calls,metadata,auth,failure=self.fixture(version='0.0.126');self.assertEqual(failure.code,41)
        self.assertEqual(metadata,[]);self.assertNotIn('resume-transfer',[x['action'] for x in calls])

    def test_resumed_package_must_match_exact_root_gate_bytes_and_hash_before_deploy(self):
        for changes in ({'bundleBytes':124},{'bundleSha256':'f'*64}):
            ticket={'status':'transfer_resumed','transferTicket':self.TICKET,'bundleBytes':123,'bundleSha256':self.HASH}
            calls,metadata,auth,failure=self.fixture(ticket=dict(ticket,**changes));self.assertEqual(failure.code,47)
            self.assertNotIn('deploy',[x['action'] for x in calls])

    def test_success_receipt_must_confirm_same_one_shot_gate_and_actual_health(self):
        result={'status':'deployed','sourceSha':target.INITIAL_SOURCE_SHA,'version':target.INITIAL_VERSION,
                'initialActivation':True,'gateId':self.GATE,'verification':{'healthy':True}}
        for changes in ({'initialActivation':False},{'gateId':'f'*32},{'verification':{'healthy':False}}):
            calls,metadata,auth,failure=self.fixture(result=dict(result,**changes));self.assertEqual(failure.code,44)

if __name__=='__main__':unittest.main()
