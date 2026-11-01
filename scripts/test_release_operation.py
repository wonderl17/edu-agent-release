"""Real root-owned files, isolated filesystem only; no Docker or app changes."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


def load():
    path=Path(__file__).resolve().parents[1]/'server/tencent_receiver.py'
    spec=importlib.util.spec_from_file_location('release_operation_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


@unittest.skipUnless(os.geteuid()==0 and Path('/proc/sys/kernel/random/boot_id').exists(),'requires isolated Linux root')
class ReleaseOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.receiver=load();self.receiver.STATE=Path(self.temp.name).resolve()
        self.header={'version':'0.0.133','sourceSha':'a'*40,'bundleBytes':123,'bundleSha256':'d'*64};self.previous={'buildSha':'b'*40}
        self.before={name:{'Id':str(index)*64,'Image':'sha256:'+str(index)*64,'State':{'StartedAt':'synthetic'}}
                     for index,name in enumerate(('api','worker-general','worker-ppt'),start=1)}

    def test_exclusive_marker_tracks_phases_and_owner_until_clear(self):
        r=self.receiver;operation=r.release_operation_create(self.header,self.previous,self.before)
        path=r.STATE/'release-operation.pending.json';self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(path.read_text())['phase'],'prepared')
        with self.assertRaises(r.Refused):r.release_operation_create(self.header,self.previous,self.before)
        with self.assertRaisesRegex(r.Refused,'RELEASE_OPERATION_RECOVERY_REQUIRED'):r.require_host_ready()
        operation=r.release_operation_phase(operation,'api_stop_requested')
        self.assertEqual(json.loads(path.read_text())['phase'],'api_stop_requested')
        altered=dict(operation,operationId='f'*32)
        with self.assertRaisesRegex(r.Refused,'OWNER_CHANGED'):r.release_operation_clear(altered)
        self.assertTrue(path.exists());r.release_operation_clear(operation);self.assertFalse(path.exists())

    def test_existing_symlink_is_never_overwritten(self):
        r=self.receiver;path=r.STATE/'release-operation.pending.json';target=r.STATE/'unrelated';target.write_text('keep')
        path.symlink_to(target)
        with self.assertRaises(r.Refused):r.release_operation_create(self.header,self.previous,self.before)
        self.assertEqual(target.read_text(),'keep')


if __name__=='__main__':unittest.main()
