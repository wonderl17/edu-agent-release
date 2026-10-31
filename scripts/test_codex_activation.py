"""Offline receiver fault injection. No Docker, SSH, provider or production calls."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from test_codex_runtime import runtime, configuration, IDENTITY, IMAGE

ROOT = Path(__file__).resolve().parents[1]


def load_receiver():
    spec = importlib.util.spec_from_file_location('activation_receiver', ROOT/'server/tencent_receiver.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def package(with_runtime=True):
    value = dict(images={name: dict(tag='docker.cnb.cool/shukezhinao/edu-agent-cloud-build/'+name+'@sha256:'+'b'*64)
                         for name in ('api', 'worker', 'web')})
    if with_runtime:
        value['images']['codex-runtime'] = dict(tag=IMAGE)
        value['runtime'] = copy.deepcopy(IDENTITY)
    return value


def containers(with_runtime=False):
    names = ['api', 'worker-general', 'worker-ppt', 'web', 'gateway', 'postgres', 'redis', 'storage']
    if with_runtime: names.append('codex-agent-runtime')
    return {name: dict(Id=format(i+1,'x')*64, Image='sha256:'+format(i+1,'x')*64,
                       State=dict(StartedAt='previous-start')) for i, name in enumerate(names)}


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.receiver = load_receiver()

    def test_candidate_uses_root_settings_and_preserves_existing_release_fields(self):
        receiver = self.receiver
        for mode, ids in [('closed', []), ('allowlist', ['teacher-a'])]:
            config = dict(configuration(), rolloutMode=mode, userIds=ids)
            receiver.ops = types.SimpleNamespace(codex_module=lambda: runtime, codex_context=lambda: None)
            with patch.object(runtime, 'load_settings', return_value=config) as settings:
                value = receiver.candidate_override({}, package(), Path('/synthetic/web'))
            settings.assert_called_once_with(receiver.ops)
            self.assertEqual(value['services']['api']['image'], package()['images']['api']['tag'])
            self.assertEqual(value['services']['worker-general'], dict(image=package()['images']['worker']['tag']))
            self.assertEqual(value['services']['web']['volumes'][0]['source'], '/synthetic/web')
            self.assertEqual(value['services']['api']['environment']['AGENT_ROLLOUT_MODE'], mode)
            self.assertEqual(value['services']['api']['environment']['AGENT_ROLLOUT_USER_IDS'], ','.join(ids))
            self.assertEqual(value['services']['codex-agent-runtime'], runtime.extension(IMAGE, IDENTITY, config)['services']['codex-agent-runtime'])

    def test_incomplete_host_support_refuses_before_candidate_write_or_commands(self):
        receiver = self.receiver
        for ops in (types.SimpleNamespace(), types.SimpleNamespace(codex_module=lambda: runtime),
                    types.SimpleNamespace(codex_module=lambda:types.SimpleNamespace(extension=runtime.extension,load_settings=runtime.load_settings),codex_context=lambda:None)):
            receiver.ops = ops
            with patch.object(receiver, 'atomic') as write, patch.object(receiver, 'command') as command:
                with self.assertRaisesRegex(receiver.Refused, 'ACTIVATION_NOT_INSTALLED'):
                    receiver.candidate_preflight({}, package(), Path('/synthetic/web'))
                write.assert_not_called(); command.assert_not_called()

    def test_pending_configuration_prevents_release_or_stage_side_effects(self):
        receiver=self.receiver
        with tempfile.TemporaryDirectory() as directory:
            receiver.STATE=Path(directory);(receiver.STATE/'codex-configuration.pending.json').write_text('{}')
            for action in (receiver.deploy,receiver.stage):
                with patch.object(receiver,'current')as current, patch.object(receiver,'command')as command:
                    with self.assertRaisesRegex(receiver.Refused,'CONFIGURATION_RECOVERY_REQUIRED'):action({},io.BytesIO())
                    current.assert_not_called();command.assert_not_called()

    def test_pending_installation_blocks_initialization_release_and_stage(self):
        receiver=self.receiver
        with tempfile.TemporaryDirectory()as directory:
            receiver.ROOT=Path(directory).resolve();receiver.OPS=receiver.ROOT/'operations/release';receiver.STATE=receiver.OPS/'state'
            receiver.STATE.mkdir(parents=True);marker=receiver.STATE/'codex-host-install.pending.json'
            for dangling in (False,True):
                if dangling:marker.symlink_to(receiver.STATE/'missing')
                else:marker.write_text('{}')
                with patch.object(receiver.os,'geteuid',return_value=0),patch.object(Path,'stat',return_value=types.SimpleNamespace(st_uid=0)), \
                     patch.object(receiver,'private')as private,patch.object(receiver,'command')as command:
                    with self.assertRaisesRegex(receiver.Refused,'HOST_INSTALLATION_RECOVERY_REQUIRED'):receiver.initialize()
                    for action in(receiver.deploy,receiver.stage):
                        with self.assertRaisesRegex(receiver.Refused,'HOST_INSTALLATION_RECOVERY_REQUIRED'):action({},io.BytesIO())
                    private.assert_not_called();command.assert_not_called()
                marker.unlink()

    def test_runtime_health_is_required_before_starting_business_containers(self):
        receiver = self.receiver; commands = []
        receiver.ops = types.SimpleNamespace(preflight=lambda: dict(services={'codex-agent-runtime': {}}),
                                             compose_argv=lambda: ['docker', 'compose'])
        def command(argv, **_):
            commands.append(argv)
            if argv[-1] == 'codex-agent-runtime': raise receiver.Refused('RUNTIME_UNHEALTHY')
        with patch.object(receiver, 'command', side_effect=command):
            with self.assertRaisesRegex(receiver.Refused, 'RUNTIME_UNHEALTHY'): receiver.up_four()
        self.assertEqual(len(commands), 1); self.assertIn('--wait', commands[0])
        with patch.object(receiver, 'command', side_effect=lambda a, **kw: commands.append(a)):
            receiver.up_four()
        self.assertEqual(commands[-2][-1], 'codex-agent-runtime')
        self.assertEqual(commands[-1][-4:], ['api','worker-general','worker-ppt','web'])

    def test_api_stop_checks_actual_exit_and_identity_before_release_can_continue(self):
        receiver=self.receiver;api=containers()['api']
        stopped=copy.deepcopy(api);stopped['State'].update(Running=False,ExitCode=0,OOMKilled=False,Restarting=False)
        with patch.object(receiver,'command',return_value=json.dumps([stopped]).encode()):
            receiver.stop_api_for_release(api)
        for field,value in [('ExitCode',1),('ExitCode',137),('Running',True),('OOMKilled',True),('Restarting',True)]:
            invalid=copy.deepcopy(stopped);invalid['State'][field]=value
            with patch.object(receiver,'command',return_value=json.dumps([invalid]).encode()):
                with self.assertRaisesRegex(receiver.Refused,'API_SHUTDOWN_INCOMPLETE'):
                    receiver.stop_api_for_release(api)
        invalid=copy.deepcopy(stopped);invalid['Image']='sha256:'+'0'*64
        with patch.object(receiver,'command',return_value=json.dumps([invalid]).encode()):
            with self.assertRaisesRegex(receiver.Refused,'API_SHUTDOWN_INCOMPLETE'):
                receiver.stop_api_for_release(api)

    def test_legacy_restore_starts_apps_before_removing_only_the_owned_stateless_runner(self):
        receiver = self.receiver; commands = []; identity = 'a'*64
        receiver.ops = types.SimpleNamespace(preflight=lambda: dict(services={}), compose_argv=lambda: ['docker','compose'])
        container = dict(Id=identity, Config=dict(User='1000:1000', Labels={
            'com.docker.compose.project':'teaching-production', 'com.docker.compose.service':'codex-agent-runtime',
            'com.docker.compose.oneoff':'False'}), Mounts=[dict(Type='tmpfs',Destination='/tmp')])
        def command(argv, **_):
            commands.append(argv)
            if 'ps' in argv: return identity.encode()
            if 'inspect' in argv: return json.dumps([container]).encode()
            return b''
        with patch.object(receiver, 'command', side_effect=command): receiver.up_four()
        self.assertEqual(commands[0][-4:], ['api','worker-general','worker-ppt','web'])
        self.assertEqual(commands[-1][-3:], ['rm','--force',identity])
        for mutate in (lambda c: c['Mounts'].append(dict(Type='volume',Destination='/data')),
                       lambda c: c['Config']['Labels'].update({'com.docker.compose.project':'another-project'}),
                       lambda c: c.update(Id='b'*64)):
            original=copy.deepcopy(container);mutate(container);commands.clear()
            with patch.object(receiver, 'command', side_effect=command):
                with self.assertRaisesRegex(receiver.Refused,'ORPHAN_SCOPE'): receiver.remove_obsolete_runtime()
            self.assertFalse(any('rm' in command for command in commands)); container=original

    def test_worker_stop_checks_both_exit_codes_and_container_identity(self):
        receiver=self.receiver;before=containers();workers=[before['worker-general'],before['worker-ppt']]
        stopped=copy.deepcopy(workers)
        for item in stopped:item['State'].update(Running=False,ExitCode=0,OOMKilled=False,Restarting=False)
        with patch.object(receiver,'command',return_value=json.dumps(list(reversed(stopped))).encode()):
            receiver.stop_workers_for_release(*workers)
        for index in range(2):
            for field,value in [('ExitCode',1),('ExitCode',137),('Running',True),('OOMKilled',True),('Restarting',True)]:
                invalid=copy.deepcopy(stopped);invalid[index]['State'][field]=value
                with patch.object(receiver,'command',return_value=json.dumps(invalid).encode()):
                    with self.assertRaisesRegex(receiver.Refused,'WORKER_SHUTDOWN_INCOMPLETE'):receiver.stop_workers_for_release(*workers)
            for field in ('Id','Image'):
                invalid=copy.deepcopy(stopped);invalid[index][field]='different'
                with patch.object(receiver,'command',return_value=json.dumps(invalid).encode()):
                    with self.assertRaisesRegex(receiver.Refused,'WORKER_SHUTDOWN_INCOMPLETE'):receiver.stop_workers_for_release(*workers)

    def test_runner_start_failure_can_restore_untouched_already_drained_business_containers(self):
        receiver=self.receiver;before=containers();active=copy.deepcopy(before)
        for item in active.values():item['State'].update(Running=False,ExitCode=0)
        receiver.ops=types.SimpleNamespace(inspect=lambda:active)
        proofs={kind:dict(targetId='new-'+kind)for kind in ('api','worker')}
        with patch.object(receiver,'command')as command,patch.object(receiver,'drain')as drain:
            receiver.quiesce_for_rollback(before,proofs)
            command.assert_not_called();drain.assert_not_called()

    def test_rollback_rejects_unknown_images_and_missing_applications_before_stopping(self):
        receiver=self.receiver;before=containers();proofs={kind:dict(targetId='new-'+kind)for kind in ('api','worker')}
        for mutation,code in [(lambda c:c['api'].update(Image='unrelated'),'IMAGE_CHANGED'),
                              (lambda c:c.pop('worker-general'),'SET_INCOMPLETE')]:
            active=copy.deepcopy(before);mutation(active)
            receiver.ops=types.SimpleNamespace(inspect=lambda:active)
            with patch.object(receiver,'command')as command:
                with self.assertRaisesRegex(receiver.Refused,code):receiver.quiesce_for_rollback(before,proofs)
                command.assert_not_called()

    def test_unavailable_drain_worker_stops_api_but_never_recreates_over_unknown_work(self):
        receiver=self.receiver;before=containers();active=copy.deepcopy(before)
        active['api']['Id']='f'*64;active['worker-general']['State']['Running']=False
        receiver.ops=types.SimpleNamespace(inspect=lambda:active)
        proofs={kind:dict(targetId='new-'+kind)for kind in ('api','worker')}
        with patch.object(receiver,'stop_api_for_release')as stop,patch.object(receiver,'drain')as drain:
            with self.assertRaisesRegex(receiver.Refused,'DRAIN_WORKER_UNAVAILABLE'):receiver.quiesce_for_rollback(before,proofs)
            stop.assert_called_once_with(active['api']);drain.assert_not_called()

    def test_rollback_requires_old_version_images_health_and_immutable_service_identity(self):
        receiver=self.receiver; before=containers(); value=dict(version='0.0.125',buildSha='c'*40)
        for mutate, code in (
            (lambda v,c:v.update(buildSha='b'*40),'IDENTITY'),
            (lambda v,c:c['api'].update(Image='different'),'IMAGE'),
            (lambda v,c:c['postgres'].update(Id='changed'),'CONTAINER'),
            (lambda v,c:c['redis']['State'].update(StartedAt='restarted'),'IMMUTABLE'),
            (lambda v,c:c.update({'orphan':{}}),'IDENTITY'),
        ):
            current_value=copy.deepcopy(value); after=copy.deepcopy(before);mutate(current_value,after)
            receiver.ops=types.SimpleNamespace(snapshot=lambda:dict(healthy=True))
            with patch.object(receiver,'current',return_value=(current_value,after)):
                with self.assertRaisesRegex(receiver.Refused,code): receiver.verify_restored(value,before,True)
        after=copy.deepcopy(before);after['api']['Id']='d'*64
        with patch.object(receiver,'current',return_value=(value,after)):
            receiver.verify_restored(value,before,True)
            with self.assertRaisesRegex(receiver.Refused,'CONTAINER'): receiver.verify_restored(value,before,False)

    def test_rollback_waits_for_readiness_and_times_out_without_claiming_success(self):
        receiver=self.receiver; value=dict(version='0.0.125',buildSha='c'*40); before=containers()
        receiver.ops=types.SimpleNamespace(snapshot=lambda:dict(healthy=False))
        with patch.object(receiver.time,'monotonic',side_effect=[0,181]), patch.object(receiver,'current') as current:
            with self.assertRaisesRegex(receiver.Refused,'ROLLBACK_HEALTH'):receiver.verify_restored(value,before,False)
            current.assert_not_called()
        with patch.object(receiver.ops,'snapshot',side_effect=[dict(healthy=False),dict(healthy=True)]), \
             patch.object(receiver.time,'sleep') as sleep, patch.object(receiver,'current',return_value=(value,before)):
            receiver.verify_restored(value,before,False)
            sleep.assert_called_once_with(4)

    def test_postflight_requires_exact_runtime_image_and_private_endpoint_health(self):
        receiver=self.receiver; before=containers(); after=containers(True); manifest=package()
        value=dict(version='0.0.126',buildSha='a'*40); header=dict(version=value['version'],sourceSha=value['buildSha'])
        proofs={kind:dict(targetId=after[name]['Image'])for kind,name in [('api','api'),('worker','worker-general'),('codex-runtime','codex-agent-runtime')]}
        after['worker-ppt']['Image']=proofs['worker']['targetId']
        result=dict(runtime=IDENTITY,egressProtocol='teaching-codex-egress-v1',unauthorizedRequestDenied=True)
        probe=types.SimpleNamespace(probe=lambda *args:result)
        receiver.ops=types.SimpleNamespace(snapshot=lambda:dict(healthy=True,dependencies=dict(consumers=[dict(workers=1)])),
            codex_module=lambda:probe,codex_context=lambda:None)
        with patch.object(receiver,'current',return_value=(value,after)), \
             patch.object(receiver,'command',return_value=json.dumps(value).encode()):
            self.assertEqual(receiver.postflight(header,manifest,before,proofs)['codex'],result)
            with patch.object(probe,'probe',side_effect=ValueError('gateway unreachable')):
                with self.assertRaisesRegex(ValueError,'gateway unreachable'):receiver.postflight(header,manifest,before,proofs)
            after['codex-agent-runtime']['Image']='different'
            with patch.object(probe,'probe') as health:
                with self.assertRaisesRegex(receiver.Refused,'CODEX_IMAGE_ID'):receiver.postflight(header,manifest,before,proofs)
                health.assert_not_called()

    def deployment(self, fail=None, previous_runtime=False, rollback_healthy=True):
        """Run the real deploy control flow; replace only host/registry side effects."""
        receiver=self.receiver; events=[]; before=containers(previous_runtime)
        old=dict(version='0.0.125',buildSha='c'*40); header=dict(version='0.0.126',sourceSha='a'*40,bundleBytes=1)
        manifest=package(); expected=dict(services={'candidate':{}})
        candidate_containers=containers(True)
        for name in ('api','worker-general','worker-ppt'):
            candidate_containers[name]['Id']='f'+candidate_containers[name]['Id'][1:]
            candidate_containers[name]['State'].update(Running=True,ExitCode=0)
        proofs={kind:dict(targetId=candidate_containers[name]['Image']) for kind,name in
                [('api','api'),('worker','worker-general')]}
        candidate_containers['worker-ppt']['Image']=proofs['worker']['targetId']
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root=Path(directory); target=root/'releases/current'; opsdir=root/'operations';opsdir.mkdir()
            target.parent.mkdir()
            override=opsdir/'compose.release.yaml';previous=b'{"previous":"exact bytes"}\n';override.write_bytes(previous)
            receiver.OPS=opsdir;receiver.OVERRIDE=override;receiver.STATE=opsdir/'state'
            receiver.rolling_back=False
            def atomic(path,value):
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(value if isinstance(value,bytes) else json.dumps(value).encode())
                if path==override:events.append(('override',path.read_bytes()))
            @contextlib.contextmanager
            def maintenance(*args,**kwargs):
                events.append('maintenance')
                yield
            def preflight(*args):
                events.append('preflight')
                return dict(services={})
            receiver.ops=types.SimpleNamespace(fixed_env=lambda:{'PRODUCTION_ACTIVATED':'true'},maintenance=maintenance,
                preflight=preflight,snapshot=lambda:dict(healthy=rollback_healthy),
                inspect=lambda:copy.deepcopy(candidate_containers))
            def pull(*args):
                web=receiver.run_dir/'web-dist';web.mkdir()
                (receiver.run_dir/'manifest.json').write_text('{}')
                return manifest,web,{},proofs
            handler=types.SimpleNamespace(ticket=lambda *args:True,pull=pull)
            def candidate(*args):
                events.append('candidate')
                if fail=='candidate':raise receiver.Refused('CANDIDATE_REFUSED')
                return copy.deepcopy(expected)
            def migration(*args):
                events.append('migration')
                if fail=='migration':raise receiver.Refused('MIGRATION_FAILED')
            def start():
                events.append('up')
                if not receiver.rolling_back:
                    self.assertEqual(json.loads(override.read_bytes()),expected)
            def postflight(*args):
                events.append('postflight')
                if fail in('postflight','rollback-api','rollback-drain','rollback-workers'):raise receiver.Refused('PRIVATE_HEALTH_FAILED')
                if fail=='cancel':raise receiver.Refused('CLIENT_DISCONNECTED')
                return dict(healthy=True)
            def command(argv,**kwargs):
                events.append(('command',argv))
                if 'inspect'in argv and argv[-1]==candidate_containers['api']['Id']:
                    stopped=copy.deepcopy(candidate_containers['api']);stopped['State'].update(Running=False,ExitCode=1 if fail=='rollback-api'else 0)
                    return json.dumps([stopped]).encode()
                if 'inspect'in argv and argv[-2:]==[candidate_containers['worker-general']['Id'],candidate_containers['worker-ppt']['Id']]:
                    stopped=[copy.deepcopy(candidate_containers[name])for name in ('worker-general','worker-ppt')]
                    for item in stopped:item['State'].update(Running=False,ExitCode=0)
                    if fail=='rollback-workers':stopped[1]['State']['ExitCode']=1
                    return json.dumps(stopped).encode()
                if 'inspect'in argv and argv[-1]==before['api']['Id']:
                    stopped=copy.deepcopy(before['api']);stopped['State'].update(Running=False,ExitCode=1 if fail=='shutdown'else 0)
                    return json.dumps([stopped]).encode()
                if 'inspect'in argv and argv[-2:]==[before['worker-general']['Id'],before['worker-ppt']['Id']]:
                    stopped=[copy.deepcopy(before[name])for name in ('worker-general','worker-ppt')]
                    for item in stopped:item['State'].update(Running=False,ExitCode=0)
                    if fail=='worker-shutdown':stopped[1]['State']['ExitCode']=1
                    return json.dumps(stopped).encode()
                return b''
            def drain(general):
                events.append(('drain',general['Id']))
                if receiver.rolling_back and fail=='rollback-drain':raise receiver.Refused('ACTIVE_WORK_DRAIN_TIMEOUT')
            for name, replacement in dict(atomic=atomic,current=lambda:(copy.deepcopy(old),copy.deepcopy(before)),
                registry_handler=lambda:(handler,None),version_policy=lambda *args:None,artifact_directory=lambda h:target,
                candidate_preflight=candidate,migration=migration,drain=drain,
                up_four=start,postflight=postflight,finish_liveness=lambda:None,command=command).items():
                stack.enter_context(patch.object(receiver,name,replacement))
            stack.enter_context(patch.object(receiver.shutil,'disk_usage',return_value=types.SimpleNamespace(free=10**12)))
            stack.enter_context(patch.object(receiver.time,'monotonic',side_effect=[0,181]))
            if fail=='candidate':
                with self.assertRaisesRegex(receiver.Refused,'CANDIDATE_REFUSED'):receiver.deploy(header,io.BytesIO())
                self.assertNotIn('maintenance',events);self.assertNotIn('migration',events)
                self.assertFalse(any(isinstance(e,tuple)and e[0]=='command' for e in events))
                self.assertEqual(override.read_bytes(),previous)
                return
            result=receiver.deploy(header,io.BytesIO())
            if fail:
                if fail.startswith('rollback-'):
                    self.assertEqual(json.loads(override.read_bytes()),expected)
                    self.assertFalse(result['configurationRollbackSucceeded'])
                    self.assertEqual(result['code'],'DEPLOY_FAILED_ROLLBACK_INCOMPLETE')
                    self.assertEqual(result['rollbackCauseCode'],{'rollback-api':'API_SHUTDOWN_INCOMPLETE',
                        'rollback-drain':'ACTIVE_WORK_DRAIN_TIMEOUT','rollback-workers':'WORKER_SHUTDOWN_INCOMPLETE'}[fail])
                    self.assertEqual(events.count('up'),1)
                    return
                self.assertEqual(override.read_bytes(),previous)
                self.assertEqual(result['status'],'failed');self.assertFalse(result['databaseAutomaticallyRestored'])
                self.assertEqual(result['manualDatabaseReviewRequired'],fail not in('shutdown','worker-shutdown'))
                self.assertEqual(result['configurationRollbackSucceeded'],rollback_healthy)
                self.assertEqual(result['code'],'DEPLOY_FAILED_ROLLBACK_RESTORED'if rollback_healthy else'DEPLOY_FAILED_ROLLBACK_INCOMPLETE')
                if fail in('migration','shutdown','worker-shutdown'):
                    self.assertFalse(result['migrationSucceeded'])
                    self.assertFalse(any(isinstance(e,tuple)and e[0]=='override' for e in events))
                    self.assertTrue(any(isinstance(e,tuple)and e[0]=='command'and 'start' in e[1] for e in events))
                    if fail=='shutdown':self.assertNotIn(('drain',before['worker-general']['Id']),events);self.assertNotIn('migration',events)
                    if fail=='worker-shutdown':self.assertIn(('drain',before['worker-general']['Id']),events);self.assertNotIn('migration',events)
                else:
                    self.assertTrue(result['migrationSucceeded']);self.assertEqual(events.count('up'),2)
                    # A failed postflight may occur after the new coordinator
                    # dispatched generation. Never recreate over those writers.
                    stopped_api=('command',receiver.DOCKER+['stop','--time','30',candidate_containers['api']['Id']])
                    stopped_workers=('command',receiver.DOCKER+['stop','--time','45',candidate_containers['worker-general']['Id'],candidate_containers['worker-ppt']['Id']])
                    self.assertIn(stopped_api,events)
                    self.assertLess(events.index(stopped_api),events.index(('drain',candidate_containers['worker-general']['Id'])))
                    self.assertLess(events.index(('drain',candidate_containers['worker-general']['Id'])),events.index(stopped_workers))
                    self.assertLess(events.index(stopped_workers),events.index(('override',previous)))
            else:
                self.assertEqual(result['status'],'deployed')
                self.assertEqual(json.loads(override.read_bytes()),expected)
                self.assertEqual(events.count('candidate'),1)
                self.assertTrue((receiver.STATE/'last-success.json').exists())

    def test_rejected_candidate_never_enters_maintenance_or_stops_services(self): self.deployment('candidate')
    def test_success_activates_the_same_preflighted_candidate(self): self.deployment()
    def test_failed_migration_restarts_original_ids_and_verifies_health(self): self.deployment('migration')
    def test_unclean_api_exit_restores_original_services_before_queue_drain_or_migration(self): self.deployment('shutdown')
    def test_unclean_worker_exit_restores_original_services_before_migration(self): self.deployment('worker-shutdown')
    def test_failed_migration_does_not_claim_recovery_for_unhealthy_old_services(self): self.deployment('migration',rollback_healthy=False)
    def test_failed_activation_restores_exact_prior_override_and_health(self): self.deployment('postflight')
    def test_failed_activation_restores_a_prior_runtime_package(self): self.deployment('postflight',previous_runtime=True)
    def test_failed_recovery_is_reported_incomplete(self): self.deployment('postflight',rollback_healthy=False)
    def test_client_cancellation_after_activation_restores_previous_configuration_and_health(self): self.deployment('cancel')
    def test_incomplete_candidate_api_shutdown_keeps_candidate_and_reports_reason(self): self.deployment('rollback-api')
    def test_candidate_drain_timeout_never_switches_to_old_workers(self): self.deployment('rollback-drain')
    def test_incomplete_candidate_worker_shutdown_keeps_candidate_and_reports_reason(self): self.deployment('rollback-workers')


class PrivateHealthTests(unittest.TestCase):
    def test_real_probe_program_rejects_bad_sdk_dead_gateway_and_missing_auth(self):
        for fault in ('none','sdk','gateway','auth','runner'):
            def run(argv,*,data,timeout):
                self.assertEqual(argv[-2:],['node','--input-type=module']);self.assertEqual(timeout,15)
                self.assertNotIn('Bearer',data.decode())
                setup="""const expectedIdentity=%s;let calls=[];
globalThis.fetch=async (url,options)=>{calls.push(url);
 if(url.endsWith('/responses'))return {status:%s};
 const runner=url.includes(':4986');
 return {ok:!(runner && %s),json:async()=>runner?{ok:true,runtime:{...expectedIdentity,sdkVersion:%s}}:{ok:%s,protocol:'teaching-codex-egress-v1'}};
};
"""%(json.dumps(IDENTITY),'200' if fault=='auth' else '401',str(fault=='runner').lower(),json.dumps('0.0.1'if fault=='sdk'else IDENTITY['sdkVersion']),str(fault!='gateway').lower())
                return subprocess.run(['node','--input-type=module'],input=setup.encode()+data,check=True,capture_output=True,timeout=15).stdout
            ops=types.SimpleNamespace(DOCKER=['docker'],run=run)
            if fault=='none':self.assertTrue(runtime.probe(ops,'a'*64,IDENTITY)['unauthorizedRequestDenied'])
            else:
                with self.assertRaises(subprocess.CalledProcessError):runtime.probe(ops,'a'*64,IDENTITY)


if __name__=='__main__': unittest.main()
