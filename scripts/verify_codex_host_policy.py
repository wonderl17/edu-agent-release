"""Exercise host patches against an explicit local source copy; never install them.

Docker Compose is used only to render configuration. All host state, image
inspection and health responses are synthetic. The output records that scope.
"""
import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from test_codex_runtime import runtime, configuration, IDENTITY, IMAGE

REPO = Path(__file__).resolve().parents[1]


class HostPolicyTests(unittest.TestCase):
    source = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-host-policy-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        spec = importlib.util.spec_from_file_location('local_host_policy', self.source)
        self.ops = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.ops)
        self.ops.ROOT = self.root; self.ops.RELEASE = self.root/'compose.release.json'
        self.ops.DOCKER = ['docker']; self.ops.fixed_env = lambda: {'PRODUCTION_ACTIVATED':'true'}
        # Local non-root fixture. This check is not proof of host ownership.
        self.ops.safe_file = lambda p: self.assertTrue(p.is_file() and p.resolve().is_relative_to(self.root))
        (self.root/'operations/release').mkdir(parents=True)
        shutil.copyfile(REPO/'server/codex_runtime.py', self.root/'operations/release/codex_runtime.py')
        (self.root/'environments').mkdir()
        for name in ('teaching-api.env','teaching-worker.env','teaching-ppt-worker.env'):
            (self.root/'environments'/name).write_text('')
        (self.root/'environments/codex-runtime.json').write_text(json.dumps(configuration()))
        self.web = self.root/'web'; self.web.mkdir()
        self.base = self.render(self.baseline())
        addition = runtime.extension(IMAGE,IDENTITY,configuration())
        self.candidate = self.render(self.baseline(), addition)
        self.ops.run = self.host_run
        self.ops.config = lambda include_release=True: copy.deepcopy(self.candidate if include_release else self.base)

    def render(self, *values):
        argv = ['docker','compose','--project-name','teaching-production']
        for i,value in enumerate(values):
            path = self.root/('render-'+str(i)+'.json'); path.write_text(json.dumps(value)); argv += ['-f',str(path)]
        return json.loads(subprocess.check_output(argv+['config','--format','json'],stderr=subprocess.PIPE,timeout=10))

    def baseline(self):
        services = {name:dict(image='synthetic/'+name, restart='unless-stopped', networks={'app':None})
                    for name in self.ops.SERVICES}
        env = dict(COMPOSE_PROJECT_NAME='teaching-production',DATABASE_HOST='postgres',TEACHING_DEPLOYMENT_ENV='shukezhinao-prod',
            PRODUCTION_ACTIVATED='true',PRISMA_DB_PUSH='false',PRISMA_MIGRATE_DEPLOY='false',
            DATABASE_URL='postgresql://synthetic:synthetic@postgres:5432/synthetic',REDIS_URL='redis://redis:6379',
            OBJECT_STORAGE_ENDPOINT='https://files-shukezhinao.321weiqi.com',OBJECT_STORAGE_FORCE_PATH_STYLE='true')
        for name in ('api','worker-general','worker-ppt'):
            api = name=='api'; role = 'general' if name=='worker-general' else 'ppt'
            services[name]['image'] = 'teaching-ai-production/'+('api'if api else'worker')+':synthetic'
            services[name]['environment'] = dict(env,API_BACKGROUND_TASKS_ENABLED='true'if api else'false',
                AI_WORKER_ENABLED='false'if api else'true',PRISMA_APPLICATION_NAME='tencent-production-'+name)
            if not api: services[name]['environment'].update(AI_WORKER_ROLE=role,AI_JOB_WORKER_CONCURRENCY='1',
                AI_HEAVY_MEMORY_BUDGET_UNITS='1',AI_GRADING_CONCURRENCY='1',AI_GRADING_VISION_CONCURRENCY='1')
        services['web']['volumes'] = [dict(type='bind',source=str(self.web),target='/usr/share/nginx/html',read_only=True)]
        services['gateway']['ports'] = ['127.0.0.1:28080:8080','80:80','443:443']
        services['storage']['ports'] = ['127.0.0.1:29000:19000']
        volumes = {}
        for name,target,volume in [('postgres','/var/lib/postgresql/data','postgres_data'),('redis','/data','redis_data'),
                ('storage','/data','minio_data'),('api','/var/teaching-ai-agent/ai-job-pdf','ai_job_staging'),
                ('worker-general','/var/teaching-ai-agent/ai-job-pdf','ai_job_staging'),('worker-ppt','/var/teaching-ai-agent/ai-job-pdf','ai_job_staging')]:
            services[name]['volumes'] = [dict(type='volume',source=volume,target=target)]
            volumes[volume] = dict(name='teaching-production_'+volume)
        return dict(name='teaching-production',services=services,networks={'app':dict(name='teaching-production_app')},volumes=volumes)

    def host_run(self, argv, **kw):
        if argv[:3]==['docker','image','inspect']:
            return json.dumps([dict(RepoDigests=[IMAGE],Os='linux',Architecture='amd64',Config=dict(User='node',
                Cmd=['node','server.mjs'],Entrypoint=None,Labels={'io.shukezhinao.codex.protocol':IDENTITY['protocol'],
                'io.shukezhinao.codex.sdk-version':IDENTITY['sdkVersion']}))]).encode()
        if argv[:2]==['docker','compose'] and argv[-3:]==['config','--format','json']:
            return subprocess.check_output(argv,input=kw.get('data'),stderr=subprocess.PIPE,timeout=10)
        raise AssertionError('No actual host commands are allowed in this verification')

    def test_patched_preflight_accepts_rendered_runtime_and_keeps_existing_eight_services(self):
        value = self.ops.preflight()
        self.assertEqual(set(value['services']),set(self.ops.SERVICES)|{'codex-agent-runtime'})
        self.assertEqual(value['services']['api']['environment']['AGENT_ROLLOUT_MODE'],'closed')
        self.assertEqual(value['services']['api']['environment']['DATABASE_URL'],self.base['services']['api']['environment']['DATABASE_URL'])

    def test_candidate_is_validated_even_before_a_release_override_exists(self):
        self.assertFalse(self.ops.RELEASE.exists())
        changed = copy.deepcopy(self.base);changed['services']['api']['image']='untrusted/image:latest'
        with self.assertRaisesRegex(self.ops.Refused,'RELEASE_IMAGE_SCOPE'):self.ops.validate_release(self.base,changed)

    def test_full_policy_rejects_extra_network_credentials_and_changes_to_existing_data_services(self):
        for mutate in (
            lambda c:c['services']['codex-agent-runtime']['networks'].update(app=None),
            lambda c:c['services']['codex-agent-runtime']['environment'].update(DATABASE_URL='secret'),
            lambda c:c['services']['api']['environment'].update(AGENT_ROLLOUT_MODE='all'),
            lambda c:c['services']['postgres'].update(image='unexpected'),
            lambda c:c['services']['api']['networks'].update(extra=None),
        ):
            candidate=copy.deepcopy(self.candidate);mutate(candidate)
            with self.assertRaises((ValueError,self.ops.Refused)):self.ops.validate_release(self.base,candidate)

    def test_old_release_has_no_runtime_dependency_or_settings_requirement(self):
        (self.root/'environments/codex-runtime.json').unlink()
        self.candidate=copy.deepcopy(self.base)
        with patch.object(self.ops,'codex_module',side_effect=AssertionError('Old package must remain independent')):
            self.assertEqual(set(self.ops.preflight()['services']),set(self.ops.SERVICES))

    def test_config_candidate_uses_explicit_approved_settings_without_mutating_live_settings(self):
        next_settings=dict(configuration(),rolloutMode='allowlist',userIds=['teacher-a'],runnerToken='n'*40,egressToken='m'*40)
        candidate=self.render(self.baseline(),runtime.extension(IMAGE,IDENTITY,next_settings))
        self.ops.validate_release(self.base,candidate,approved_codex_settings=next_settings)
        self.assertEqual(runtime.load_settings(self.ops),configuration())
        with self.assertRaisesRegex(ValueError,'CODEX_SERVICE_POLICY'):self.ops.validate_release(self.base,candidate)
        candidate['services']['postgres']['image']='unapproved'
        with self.assertRaisesRegex(self.ops.Refused,'OVERRIDE_SCOPE'):self.ops.validate_release(self.base,candidate,approved_codex_settings=next_settings)

    def test_pending_configuration_blocks_host_start_and_is_visible_in_health(self):
        marker=self.root/'operations/release/state/codex-configuration.pending.json'
        marker.parent.mkdir();marker.write_text('{}')
        with patch.object(self.ops,'run')as command:
            with self.assertRaisesRegex(self.ops.Refused,'CONFIGURATION_RECOVERY_REQUIRED'):self.ops.start_locked()
            command.assert_not_called()
        value=self.snapshot()
        self.assertFalse(value['healthy']);self.assertIn('CODEX_CONFIGURATION_PENDING',value['failures'])

    def test_pending_installation_blocks_all_host_control_without_commands(self):
        marker=self.root/'operations/release/state/codex-host-install.pending.json';marker.parent.mkdir()
        for dangling in(False,True):
            if dangling:marker.symlink_to(marker.with_name('absent'))
            else:marker.write_text('{}')
            with patch.object(self.ops,'run')as command,patch.object(self.ops,'preflight')as preflight:
                with self.assertRaisesRegex(self.ops.Refused,'HOST_INSTALLATION_RECOVERY_REQUIRED'):self.ops.start_locked()
                for action in('start','compose','preflight','recovery-test'):
                    with patch.object(sys,'argv',['production-ops.py',action]):
                        with self.assertRaisesRegex(self.ops.Refused,'HOST_INSTALLATION_RECOVERY_REQUIRED'):self.ops.main()
                for action in('health','status'):
                    output=io.StringIO()
                    with patch.object(sys,'argv',['production-ops.py',action]),contextlib.redirect_stdout(output):
                        self.assertEqual(self.ops.main(),1)
                    self.assertEqual(json.loads(output.getvalue())['failures'],['CODEX_HOST_INSTALLATION_PENDING'])
                command.assert_not_called();preflight.assert_not_called()
            value=self.snapshot();self.assertFalse(value['healthy']);self.assertIn('CODEX_HOST_INSTALLATION_PENDING',value['failures'])
            marker.unlink()

    def snapshot(self, *, runner_health='healthy', probe_fails=False):
        records={name:dict(Id=format(i+1,'x')*64,State=dict(Running=True,Status='running',Health=dict(Status='healthy')),
            HostConfig=dict(RestartPolicy=dict(Name='unless-stopped')),Config=dict(Labels={})) for i,name in enumerate(self.candidate['services'])}
        records['codex-agent-runtime']['State']['Health']['Status']=runner_health
        records['codex-agent-runtime']['Config']['Labels']={'io.shukezhinao.codex.protocol':IDENTITY['protocol'],
            'io.shukezhinao.codex.sdk-version':IDENTITY['sdkVersion']}
        real_read=Path.read_text
        def read(path,*args,**kw):
            if str(path)=='/proc/meminfo':return 'MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n'
            return real_read(path,*args,**kw)
        def probe(*args):
            if probe_fails:raise ValueError('private endpoint unavailable')
            return dict(runtime=IDENTITY,egressProtocol='teaching-codex-egress-v1',unauthorizedRequestDenied=True)
        def http(argv,**kw):
            self.assertEqual(argv[0],'/usr/bin/curl')
            return b'404' if argv[-1].endswith('/console') else b'200'
        with patch.object(self.ops,'inspect',return_value=records),patch.object(self.ops,'isolation'), \
             patch.object(self.ops,'dependency_probe',return_value={'ok':True}),patch.object(self.ops,'run',side_effect=http), \
             patch.object(self.ops,'codex_module',return_value=types.SimpleNamespace(probe=probe)),patch.object(Path,'read_text',read), \
             patch.object(self.ops.shutil,'disk_usage',return_value=types.SimpleNamespace(used=1,total=10)):
            return self.ops.snapshot()

    def test_regular_health_detects_private_gateway_failures_without_model_calls(self):
        self.assertTrue(self.snapshot()['healthy'])
        value=self.snapshot(probe_fails=True)
        self.assertFalse(value['healthy']);self.assertIn('CODEX_PRIVATE_HEALTH',value['failures'])
        self.assertEqual(value['modelCalls'],0)

    def test_running_container_without_verified_sdk_health_is_unhealthy(self):
        for health in (None,'starting','unhealthy'):
            value=self.snapshot(runner_health=health)
            self.assertFalse(value['healthy']);self.assertIn('SERVICE_CODEX_AGENT_RUNTIME',value['failures'])

    def test_host_restart_includes_runtime_and_preserves_container_identities(self):
        commands=[]; records={name:dict(Id=format(i+1,'x')*64) for i,name in enumerate(self.candidate['services'])}
        with patch.object(self.ops,'run',side_effect=lambda argv,**kw:commands.append(argv)or b''), \
             patch.object(self.ops,'preflight',return_value=self.candidate),patch.object(self.ops,'inspect',return_value=records), \
             patch.object(self.ops,'compose_argv',return_value=['docker','compose']),patch.object(self.ops,'snapshot',return_value={'healthy':True}):
            self.assertTrue(self.ops.start_locked()['healthy'])
        self.assertIn('--no-recreate',commands[0]);self.assertIn('codex-agent-runtime',commands[0])
        self.assertEqual(commands[1][-2:],[records[name]['Id']for name in self.ops.WORKERS])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--ops-source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    original=args.ops_source.resolve().read_bytes()
    if b'def codex_module(' in original:raise ValueError('Provide the base source before the Codex patch')
    with tempfile.TemporaryDirectory(prefix='codex-host-patches-') as folder:
        source=Path(folder)/'production-ops.py';source.write_bytes(original)
        # The base may already include the previously deployed CNB image policy.
        patches=['production-ops-codex.patch']
        if b'docker\\.cnb\\.cool' not in original:patches.insert(0,'production-ops-cnb.patch')
        for name in patches:
            subprocess.run(['patch','--batch','--fuzz=0','-p1','-i',str(REPO/'server'/name)],cwd=folder,check=True,capture_output=True)
        HostPolicyTests.source=source
        result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HostPolicyTests))
        report=dict(status='passed'if result.wasSuccessful()else'failed',checks=result.testsRun,
            sourceSha256=hashlib.sha256(original).hexdigest(),patchedSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            patches={name:hashlib.sha256((REPO/'server'/name).read_bytes()).hexdigest()for name in patches},
            scope='local-source-copy-with-synthetic-host-state-and-real-compose-rendering',productionChanged=False,
            targetHostVerified=False,rootFileOwnershipVerified=False,realModelCalls=0)
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
        if not result.wasSuccessful():raise SystemExit(1)


if __name__=='__main__':main()
