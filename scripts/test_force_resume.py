import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load():
    path=Path(__file__).resolve().parents[1]/'server/tencent_receiver.py'
    spec=importlib.util.spec_from_file_location('force_resume_test',path)
    module=importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(),str(path),'exec'),module.__dict__)
    return module


class ForceResumeTests(unittest.TestCase):
    def setUp(self):
        self.receiver=load();self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup);self.receiver.STATE=Path(self.directory.name)
        self.h={'sourceSha':'a'*40,'version':'7.4.2',
                'bundleSha256':'f'*64,'bundleBytes':1234}
        self.value={'buildSha':'b'*40}
        self.api={'Id':'a'*64,'Image':'sha256:'+'b'*64,'State':{'StartedAt':'original'}}
        self.registry={'transport':'registry','previousTransferTicket':'c'*32,'bundleSha256':'f'*64,'bundleBytes':1234}
        self.auth={'schemaVersion':1,'action':'one-shot-resume-force-stop','nonce':'d'*32,'bootId':'test-boot',
                   'sourceSha':self.h['sourceSha'],'version':self.h['version'],'bundleSha256':'f'*64,'bundleBytes':1234,
                   'previousTicketSha256':hashlib.sha256(('c'*32).encode()).hexdigest(),
                   'previousSourceSha':self.value['buildSha'],
                   'apiId':self.api['Id'],'apiImage':self.api['Image'],'apiStartedAt':'original','expiresAt':1200}
        self.path=self.receiver.STATE/'api-force-stop-once.json'

    def call(self):
        with patch.object(self.receiver,'private'),patch.object(self.receiver,'process_identity',return_value={'bootId':'test-boot'}),\
             patch.object(self.receiver.time,'time',return_value=1000):
            return self.receiver.claim_force_resume(self.h,self.value,self.api,self.registry)

    def test_matching_resume_is_claimed_once_and_retained_for_audit(self):
        self.path.write_text(json.dumps(self.auth))
        self.assertEqual(self.call(),{'expiresAt':1200,'nonce':'d'*32})
        self.assertFalse(self.path.exists())
        self.assertTrue((self.receiver.STATE/('api-force-stop-used-'+'d'*32+'.json')).exists())
        self.assertIsNone(self.call())
        self.path.write_text(json.dumps(self.auth))
        with self.assertRaisesRegex(self.receiver.Refused,'FORCE_RESUME_AUTH_USED'):self.call()

    def test_expiry_identity_lineage_and_shape_fail_closed_without_consuming(self):
        for field,value in [('expiresAt',999),('expiresAt',2801),('sourceSha','e'*40),('version','0.0.134'),
                            ('bundleSha256','e'*64),('bundleBytes',1235),('previousTicketSha256','e'*64),
                            ('previousSourceSha','e'*40),('apiId','e'*64),('apiImage','sha256:'+'e'*64),
                            ('apiStartedAt','different'),('bootId','different'),('action','other')]:
            with self.subTest(field=field,value=value):
                auth=dict(self.auth);auth[field]=value;self.path.write_text(json.dumps(auth))
                with self.assertRaisesRegex(self.receiver.Refused,'FORCE_RESUME_AUTH_REFUSED'):self.call()
                self.assertTrue(self.path.exists())
        self.path.write_text(json.dumps(self.auth));self.registry.pop('previousTransferTicket')
        with self.assertRaisesRegex(self.receiver.Refused,'FORCE_RESUME_AUTH_REFUSED'):self.call()
        self.assertTrue(self.path.exists())
        self.registry['previousTransferTicket']='e'*32
        with self.assertRaisesRegex(self.receiver.Refused,'FORCE_RESUME_AUTH_REFUSED'):self.call()
        self.assertTrue(self.path.exists())

    def test_no_authorization_preserves_normal_graceful_path(self):
        self.assertIsNone(self.call())


if __name__=='__main__':unittest.main()
