"""Final-image proof gating and cancellation, without Docker or production calls."""
import ast
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'server/tencent_receiver.py'

def load():
    spec=importlib.util.spec_from_file_location('shutdown_acceptance_receiver',SOURCE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.r=load()
        self.identity=dict(scope=self.r.SHUTDOWN_ACCEPTANCE_SCOPE,sourceSha='a'*40,version='0.0.135',
            apiImageDigest='sha256:'+'b'*64,apiImageId='sha256:'+'c'*64,helperSha256='d'*64,
            scriptSha256='e'*64,kernel='synthetic-kernel')
        self.report=dict(self.identity,schemaVersion=1,status='passed',executionMode='helper',realModelCalls=0,
            verifiedAt=dt.datetime.now(dt.timezone.utc).isoformat(),
            checks={key:True for key in self.r.SHUTDOWN_ACCEPTANCE_CHECKS},
            timings=[dict(databaseOutage=False,seconds=15.1,exitCode=0),
                     dict(databaseOutage=True,seconds=25.1,exitCode=1)])

    def test_only_full_helper_proof_of_exact_image_and_implementation_counts(self):
        self.assertIs(self.r.validate_shutdown_acceptance(self.report,self.identity),self.report)
        changes=[dict(executionMode='signal'),dict(status='failed'),dict(realModelCalls=1),dict(schemaVersion=True)]
        changes += [{key:'changed'} for key in self.identity]
        for fields in changes:
            with self.subTest(fields=fields),self.assertRaises(self.r.Refused):
                self.r.validate_shutdown_acceptance(dict(self.report,**fields),self.identity)
        for key in self.report['checks']:
            bad=copy.deepcopy(self.report);bad['checks'][key]=False
            with self.subTest(check=key),self.assertRaises(self.r.Refused):
                self.r.validate_shutdown_acceptance(bad,self.identity)
        for fields in (dict(exitCode=137),dict(seconds=30),dict(seconds=float('nan')),dict(exitCode=True)):
            bad=copy.deepcopy(self.report);bad['timings'][0].update(fields)
            with self.subTest(timing=fields),self.assertRaises(self.r.Refused):
                self.r.validate_shutdown_acceptance(bad,self.identity)

    def test_proof_executes_and_is_cached_by_digest_helper_verifier_kernel_and_dependencies(self):
        r=self.r
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory).resolve();r.OPS=root/'operations';r.OPS.mkdir()
            r.STATE=r.OPS/'state';r.STATE.mkdir();r.run_dir=root/'incoming';r.run_dir.mkdir()
            for name in('api_image_shutdown.py','api_drain.py'):
                (r.OPS/name).write_text('# reviewed fixture\n');(r.OPS/name).chmod(0o600)
            before={name:dict(Id=name+'-id',Image='sha256:'+char*64,State=dict(StartedAt='stable'))
                    for name,char in(('api','1'),('postgres','2'),('redis','3'))}
            r.ops=types.SimpleNamespace(inspect=lambda:copy.deepcopy(before),snapshot=lambda:dict(healthy=True))
            header=dict(sourceSha='a'*40,version='0.0.135')
            manifest=dict(images=dict(api=dict(tag='docker.cnb.cool/shukezhinao/edu-agent-cloud-build/api@sha256:'+'b'*64)))
            proofs=dict(api=dict(targetId='sha256:'+'c'*64))
            calls=[]
            def private(path):
                if path.is_symlink()or not path.is_file()or stat.S_IMODE(path.stat().st_mode)!=0o600:
                    raise r.Refused('UNSAFE_PRIVATE_FILE')
            def atomic(path,value):
                path.write_text(json.dumps(value));path.chmod(0o600)
            def execute(args):
                calls.append(args)
                report=dict(self.report,helperSha256=r.hash_file(r.OPS/'api_drain.py'),
                    scriptSha256=r.hash_file(r.OPS/'api_image_shutdown.py'),kernel=os.uname().release)
                atomic(Path(args[args.index('--output')+1]),report)
            original_lstat=Path.lstat
            def owned(path):
                info=original_lstat(path)
                if path==r.STATE/'api-shutdown-acceptance':
                    return types.SimpleNamespace(st_uid=0,st_gid=0,st_mode=info.st_mode)
                return info
            with patch.object(r,'private',side_effect=private),patch.object(r,'atomic',side_effect=atomic),\
                 patch.object(r,'shutdown_acceptance_command',side_effect=execute),patch.object(Path,'lstat',owned):
                self.assertFalse(r.api_shutdown_acceptance(header,manifest,proofs,before)['cached'])
                self.assertTrue(r.api_shutdown_acceptance(header,manifest,proofs,before)['cached'])
                self.assertEqual(len(calls),1)
                # A new run directory is a real new receiver invocation.
                (r.run_dir/'api-shutdown-acceptance.json').unlink()
                (r.OPS/'api_drain.py').write_text('# changed helper\n')
                self.assertFalse(r.api_shutdown_acceptance(header,manifest,proofs,before)['cached'])
                self.assertEqual(len(calls),2)
                self.assertEqual(calls[0][0:2],['/usr/bin/python3','-I'])
                self.assertEqual(calls[0][calls[0].index('--mode')+1],'helper')
                # Never cache an otherwise valid proof if the harness touched a
                # Production container or left the target unhealthy.
                cached=list((r.STATE/'api-shutdown-acceptance').iterdir())
                (r.run_dir/'api-shutdown-acceptance.json').unlink()
                (r.OPS/'api_drain.py').write_text('# another changed helper\n')
                changed=copy.deepcopy(before);changed['api']['Id']='unexpected-replacement'
                r.ops.inspect=lambda:changed
                with self.assertRaisesRegex(r.Refused,'ACCEPTANCE_PRODUCTION_CHANGED'):
                    r.api_shutdown_acceptance(header,manifest,proofs,before)
                self.assertEqual(list((r.STATE/'api-shutdown-acceptance').iterdir()),cached)

    def test_acceptance_failure_happens_before_maintenance_and_stage_has_no_gate(self):
        tree=ast.parse(SOURCE.read_text())
        deploy=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='deploy')
        stage=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='stage')
        gate=[n.lineno for n in ast.walk(deploy)if isinstance(n,ast.Call)and isinstance(n.func,ast.Name)and n.func.id=='api_shutdown_acceptance']
        maintenance=[n.lineno for n in ast.walk(deploy)if isinstance(n,ast.Call)and isinstance(n.func,ast.Attribute)and n.func.attr=='maintenance']
        transactions=[n.lineno for n in ast.walk(deploy)if isinstance(n,ast.Call)and isinstance(n.func,ast.Name)and n.func.id=='release_operation_create']
        self.assertEqual(len(gate),1);self.assertLess(gate[0],min(maintenance+transactions))
        self.assertFalse(any(isinstance(n,ast.Call)and isinstance(n.func,ast.Name)and n.func.id=='api_shutdown_acceptance'for n in ast.walk(stage)))

    def test_cancellation_is_polled_and_allows_isolated_cleanup_without_production_environment(self):
        r=self.r
        class Process:
            pid=42;returncode=-signal.SIGTERM
            def __init__(self):self.timeouts=[]
            def communicate(self,timeout):
                self.timeouts.append(timeout)
                if len(self.timeouts)==1:
                    r.client_cancelled.set();raise subprocess.TimeoutExpired('isolated fixture',timeout)
                return b'',b''
        process=Process()
        with patch.object(r.subprocess,'Popen',return_value=process)as spawn,patch.object(r.os,'killpg')as kill:
            with self.assertRaisesRegex(r.Refused,'CLIENT_DISCONNECTED'):
                r.shutdown_acceptance_command(['/usr/bin/python3','-I','/reviewed/helper'])
        self.assertEqual(process.timeouts,[1,90]);kill.assert_called_once_with(42,signal.SIGTERM)
        self.assertEqual(set(spawn.call_args.kwargs['env']),{'PATH','HOME','LANG'})
        with patch.object(r.subprocess,'Popen')as spawn:
            with self.assertRaises(r.Refused):r.shutdown_acceptance_command([])
            spawn.assert_not_called()

if __name__=='__main__':unittest.main()
