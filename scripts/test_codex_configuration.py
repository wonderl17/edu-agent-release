"""Configuration transaction tests with real files and synthetic host effects."""
import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_codex_runtime import runtime, configuration, IDENTITY, IMAGE

spec=importlib.util.spec_from_file_location('codex_configure',Path(__file__).resolve().parents[1]/'server/codex_configure.py')
configure=importlib.util.module_from_spec(spec);spec.loader.exec_module(configure)


class Refused(Exception): pass


def require(ok,code):
    if not ok:raise Refused(code)


class Host:
    def __init__(self,root,initial=None):
        self.ROOT=root;self.STATE=root/'state';self.STATE.mkdir()
        self.OVERRIDE=root/'compose.release.json';(root/'environments').mkdir()
        self.settings_path=root/configure.SETTINGS
        self.initial=initial or configuration();self.events=[];self.fault=None;self.rolling_back=False
        self.DOCKER=['synthetic-docker'];self.require=require;self.decode=json.loads
        self.records={name:dict(Id=format(i+1,'x')*64,Image='sha256:'+format(i+1,'x')*64,
            State=dict(StartedAt='before',Running=True),Config=dict(Labels={
                'com.docker.compose.project':'teaching-production','com.docker.compose.service':name,
                'com.docker.compose.oneoff':'False'})) for i,name in enumerate(sorted(configure.SERVICES))}
        self.serial=10
        self.override=runtime.extension(IMAGE,IDENTITY,self.initial)
        self.override['services']['api']['image']='synthetic-api'
        self.settings_path.write_bytes(configure.canonical(self.initial));self.OVERRIDE.write_bytes(configure.canonical(self.override))
        self.actual_settings=copy.deepcopy(self.initial)
        self.ops=types.SimpleNamespace(codex_module=lambda:runtime,codex_context=lambda:self,
            maintenance=self.maintenance,preflight=self.preflight,config=lambda **kw:{'base':True},
            compose_argv=lambda **kw:['synthetic-compose'],validate_release=self.validate,snapshot=self.snapshot)

    def hit(self,event):
        self.events.append(event)
        if self.fault:self.fault(event)

    @contextlib.contextmanager
    def release_lock(self):
        self.hit('release-lock')
        try:yield
        finally:self.hit('release-unlock')

    @contextlib.contextmanager
    def maintenance(self,*args,**kwargs):
        self.hit('maintenance')
        try:yield
        finally:self.hit('maintenance-end')

    def private(self,path):
        require(path.is_file() and path.resolve().is_relative_to(self.ROOT),'UNSAFE_PRIVATE_FILE')

    safe_file=private

    def atomic(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        raw=value if isinstance(value,bytes)else configure.canonical(value)
        path.write_bytes(raw)
        if path.name==configure.PENDING:self.hit('journal-'+json.loads(raw)['phase'])
        elif path==self.settings_path:self.hit('settings-'+('old'if json.loads(raw)==self.initial else'new'))
        elif path==self.OVERRIDE:self.hit('override-'+('old'if json.loads(raw)==self.override else'new'))
        else:self.hit('write-'+path.name)

    def run(self,argv,**kw):
        require(argv[1:3]==['image','inspect'],'UNEXPECTED_HOST_COMMAND')
        return json.dumps([dict(RepoDigests=[IMAGE],Os='linux',Architecture='amd64',Config=dict(User='node',
            Cmd=['node','server.mjs'],Entrypoint=None,Labels={'io.shukezhinao.codex.protocol':IDENTITY['protocol'],
            'io.shukezhinao.codex.sdk-version':IDENTITY['sdkVersion']}))]).encode()

    def validate(self,base,candidate,approved_codex_settings=None):
        self.hit('validate-candidate')
        # The production complete-policy comparison has separate real Compose
        # tests; this fixture only supplies its external host contract.
        expected=runtime.extension(IMAGE,IDENTITY,approved_codex_settings)
        require(candidate['services']['codex-agent-runtime']==expected['services']['codex-agent-runtime'],'CANDIDATE_REFUSED')
        require(candidate['services']['api']['environment']==expected['services']['api']['environment'],'CANDIDATE_REFUSED')
        require(json.loads(self.settings_path.read_bytes())==self.initial,'PREVIEW_MUTATED_LIVE_SETTINGS')

    def preflight(self):
        config=json.loads(self.settings_path.read_bytes());value=json.loads(self.OVERRIDE.read_bytes())
        expected=runtime.extension(IMAGE,IDENTITY,config)
        require(value['services']['codex-agent-runtime']==expected['services']['codex-agent-runtime'],'MIXED_CONFIGURATION')
        require(value['services']['api']['environment']==expected['services']['api']['environment'],'MIXED_CONFIGURATION')
        return value

    def current(self):
        self.preflight()
        require(self.actual_settings==json.loads(self.settings_path.read_bytes()),'STALE_API_CONFIGURATION')
        return dict(version='0.0.126',buildSha='a'*40),copy.deepcopy(self.records)

    def snapshot(self):
        self.hit('snapshot')
        failures=[]
        if not all(x['State']['Running']for x in self.records.values()):failures.append('SERVICE_UNHEALTHY')
        if self.actual_settings!=json.loads(self.settings_path.read_bytes()):failures.append('STALE_API_CONFIGURATION')
        if (self.STATE/configure.PENDING).exists():failures.append('CODEX_CONFIGURATION_PENDING')
        return dict(healthy=not failures,failures=failures)

    def command(self,argv,**kw):
        if argv[0]=='synthetic-compose':
            if argv[-3:]==['config','--format','json']:return Path(argv[argv.index('-f')+1]).read_bytes()
            require('up'in argv and argv[-1]in configure.CHANGED,'UNEXPECTED_SERVICE_CHANGE')
            service=argv[-1];self.serial+=1
            self.records[service]['Id']=format(self.serial,'064x');self.records[service]['State']=dict(Running=True,StartedAt=str(self.serial))
            if service=='api':self.actual_settings=json.loads(self.settings_path.read_bytes())
            self.hit('up-'+service);return b''
        if argv[1]=='ps':return self.records['api']['Id'].encode()
        if argv[1]=='inspect':return json.dumps([self.records['api']]).encode()
        require(argv[1]=='stop'and argv[-1]==self.records['api']['Id'],'UNEXPECTED_HOST_COMMAND')
        self.records['api']['State'].update(Running=False,ExitCode=0);self.hit('stop-api');return b''

    def request(self):
        desired=dict(self.initial,rolloutMode='allowlist',userIds=['teacher-synthetic'],runnerToken='n'*40,egressToken='m'*40)
        return dict(settingsSha256=configure.digest(self.settings_path.read_bytes()),
                    overrideSha256=configure.digest(self.OVERRIDE.read_bytes()),settings=desired)


class ConfigurationTests(unittest.TestCase):
    def host(self,initial=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return Host(Path(temp.name).resolve(),initial)

    def crash_once(self,host,at,exception=SystemExit):
        triggered=False
        def fault(event):
            nonlocal triggered
            if event==at and not triggered:triggered=True;raise exception('synthetic failure')
        host.fault=fault

    def test_pending_installation_refuses_configure_and_recover_without_side_effects(self):
        host=self.host();marker=host.STATE/'codex-host-install.pending.json'
        for dangling in(False,True):
            if dangling:marker.symlink_to(host.STATE/'missing')
            else:marker.write_text('{}')
            for action in(lambda:configure.apply(host,host.request()),lambda:configure.recover(host)):
                with self.assertRaisesRegex(Refused,'HOST_INSTALLATION_RECOVERY_REQUIRED'):action()
            self.assertEqual(host.events,[]);marker.unlink()

    def test_allowlist_and_token_change_only_restarts_api_and_runner(self):
        host=self.host();before=copy.deepcopy(host.records);request=host.request()
        result=configure.apply(host,request)
        self.assertEqual(result['status'],'configured');self.assertEqual(result['userCount'],1)
        self.assertEqual(json.loads(host.settings_path.read_bytes()),request['settings'])
        self.assertEqual([x for x in host.events if x.startswith('up-')],['up-codex-agent-runtime','up-api'])
        self.assertLess(host.events.index('validate-candidate'),host.events.index('stop-api'))
        self.assertEqual(host.events[:2],['release-lock','maintenance'])
        for name in configure.SERVICES-configure.CHANGED:self.assertEqual(host.records[name],before[name])
        self.assertFalse((host.STATE/configure.PENDING).exists())
        evidence=(host.STATE/configure.RECEIPT).read_text()+json.dumps(configure.status(host))+json.dumps(result)
        for secret in [host.initial['runnerToken'],host.initial['egressToken'],request['settings']['runnerToken'],request['settings']['egressToken'],'teacher-synthetic']:
            self.assertNotIn(secret,evidence)

    def test_stop_refuses_an_api_owned_by_another_project(self):
        host=self.host()
        record={'containers':configure.identities(host,host.records)}
        host.records['api']['Config']['Labels']['com.docker.compose.project']='unrelated-project'
        with self.assertRaisesRegex(Refused,'CODEX_CONFIGURATION_API_SCOPE'):
            configure.stop_api(host,record)
        self.assertNotIn('stop-api',host.events)
        self.assertTrue(host.records['api']['State']['Running'])

    def test_unsuccessful_api_exit_restores_previous_configuration_before_recreate(self):
        host=self.host();original=host.command
        def failed_stop(argv,**kwargs):
            result=original(argv,**kwargs)
            if argv[0]=='synthetic-docker'and argv[1]=='stop':host.records['api']['State']['ExitCode']=1
            return result
        host.command=failed_stop
        result=configure.apply(host,host.request())
        self.assertEqual(result['code'],'CODEX_CONFIGURATION_ROLLBACK_RESTORED')
        self.assertEqual(result['causeCode'],'CODEX_CONFIGURATION_API_SHUTDOWN_INCOMPLETE')
        self.assertEqual(host.actual_settings,host.initial)
        self.assertEqual(host.events.count('stop-api'),1)
        self.assertTrue(host.snapshot()['healthy'])

    def test_close_admission_keeps_existing_workflow_execution_enabled(self):
        initial=dict(configuration(),rolloutMode='allowlist',userIds=['teacher-synthetic']);host=self.host(initial)
        request=host.request();request['settings']=dict(initial,rolloutMode='closed',userIds=[])
        self.assertEqual(configure.apply(host,request)['rolloutMode'],'closed')
        env=json.loads(host.OVERRIDE.read_bytes())['services']['api']['environment']
        self.assertEqual(env['AGENT_WORKFLOWS_ENABLED'],'true');self.assertEqual(env['AGENT_ROLLOUT_USER_IDS'],'')

    def test_stale_invalid_endpoint_and_pending_transaction_do_not_stop_services(self):
        for change,code in ((lambda r:r.update(settingsSha256='f'*64),'STALE_REQUEST'),
                            (lambda r:r['settings'].update(modelBaseUrls=['https://127.0.0.1']),'ENDPOINT_INVALID'),
                            (lambda r:r['settings'].update(rolloutMode='all'),'PILOT_MODE')):
            host=self.host();request=host.request();change(request)
            with self.assertRaisesRegex((Refused,ValueError),code):configure.apply(host,request)
            self.assertNotIn('stop-api',host.events);self.assertFalse((host.STATE/configure.PENDING).exists())
        host=self.host();(host.STATE/configure.PENDING).write_text('{}')
        with self.assertRaisesRegex(Refused,'RECOVERY_REQUIRED'):configure.apply(host,host.request())
        self.assertNotIn('stop-api',host.events)

    def test_root_operator_can_change_exact_approved_model_endpoints_transactionally(self):
        host=self.host();request=host.request()
        request['settings']['modelBaseUrls']=['https://api.deepseek.com','https://other.example.com/v1']
        result=configure.apply(host,request)
        self.assertEqual(result['status'],'configured')
        self.assertEqual(host.actual_settings['modelBaseUrls'],request['settings']['modelBaseUrls'])
        self.assertEqual([x for x in host.events if x.startswith('up-')],['up-codex-agent-runtime','up-api'])
        self.assertLess(host.events.index('validate-candidate'),host.events.index('stop-api'))
        self.assertFalse((host.STATE/configure.PENDING).exists())

    def test_interrupted_endpoint_change_recovers_original_allowlist(self):
        host=self.host();request=host.request()
        request['settings']['modelBaseUrls']=['https://api.deepseek.com']
        before_settings=host.settings_path.read_bytes();before_override=host.OVERRIDE.read_bytes()
        self.crash_once(host,'settings-new')
        with self.assertRaises(SystemExit):configure.apply(host,request)
        self.assertEqual(configure.recover(host)['status'],'restored')
        self.assertEqual(host.settings_path.read_bytes(),before_settings)
        self.assertEqual(host.OVERRIDE.read_bytes(),before_override)

    def test_replay_after_success_requires_fresh_state_and_same_config_is_noop(self):
        host=self.host();request=host.request();configure.apply(host,request);count=host.events.count('stop-api')
        with self.assertRaisesRegex(Refused,'STALE_REQUEST'):configure.apply(host,request)
        fresh=configure.status(host);request.update(settingsSha256=fresh['settingsSha256'],overrideSha256=fresh['overrideSha256'])
        self.assertEqual(configure.apply(host,request)['status'],'unchanged')
        self.assertEqual(host.events.count('stop-api'),count)

    def test_each_interrupted_write_or_restart_recovers_old_pair(self):
        for point in ('journal-prepared','settings-new','override-new','up-api'):
            with self.subTest(point=point):
                host=self.host();before_settings=host.settings_path.read_bytes();before_override=host.OVERRIDE.read_bytes()
                self.crash_once(host,point)
                with self.assertRaises(SystemExit):configure.apply(host,host.request())
                self.assertEqual(configure.status(host)['status'],'recovery_required')
                self.assertEqual(configure.recover(host)['status'],'restored')
                self.assertEqual(host.settings_path.read_bytes(),before_settings);self.assertEqual(host.OVERRIDE.read_bytes(),before_override)
                self.assertTrue(host.snapshot()['healthy']);self.assertFalse((host.STATE/configure.PENDING).exists())

    def test_committed_close_survives_crash_before_receipt_cleanup_without_reopening(self):
        host=self.host(dict(configuration(),rolloutMode='allowlist',userIds=['teacher-synthetic']))
        request=host.request();request['settings']=dict(host.initial,rolloutMode='closed',userIds=[])
        self.crash_once(host,'journal-committed')
        with self.assertRaises(SystemExit):configure.apply(host,request)
        restarts=host.events.count('up-api')
        self.assertTrue(configure.recover(host)['recoveredCommit'])
        self.assertEqual(configure.status(host)['rolloutMode'],'closed');self.assertEqual(host.events.count('up-api'),restarts)

    def test_start_failure_rolls_back_both_files_and_verifies_health(self):
        host=self.host();self.crash_once(host,'up-codex-agent-runtime',Refused)
        result=configure.apply(host,host.request())
        self.assertEqual(result['code'],'CODEX_CONFIGURATION_ROLLBACK_RESTORED')
        self.assertEqual(host.actual_settings,host.initial);self.assertTrue(host.snapshot()['healthy'])

    def test_unhealthy_new_proxy_times_out_and_restores_old_configuration(self):
        host=self.host();snapshot=host.snapshot
        def unhealthy():
            if host.actual_settings!=host.initial:return dict(healthy=False,failures=['CODEX_PRIVATE_HEALTH','CODEX_CONFIGURATION_PENDING'])
            return snapshot()
        host.ops.snapshot=unhealthy
        with patch.object(configure.time,'monotonic',side_effect=[0,181,182]):
            result=configure.apply(host,host.request())
        self.assertEqual(result['code'],'CODEX_CONFIGURATION_ROLLBACK_RESTORED')
        self.assertEqual(result['causeCode'],'CODEX_CONFIGURATION_HEALTH_TIMEOUT')

    def test_rejected_preview_cleans_candidate_without_journal_or_service_changes(self):
        host=self.host();self.crash_once(host,'validate-candidate',Refused)
        original=(host.settings_path.read_bytes(),host.OVERRIDE.read_bytes())
        with self.assertRaises(Refused):configure.apply(host,host.request())
        self.assertEqual((host.settings_path.read_bytes(),host.OVERRIDE.read_bytes()),original)
        self.assertNotIn('stop-api',host.events);self.assertEqual(list(host.STATE.iterdir()),[])

    def test_corrupted_journal_refuses_recovery_before_touching_services(self):
        host=self.host();self.crash_once(host,'journal-prepared')
        with self.assertRaises(SystemExit):configure.apply(host,host.request())
        path=host.STATE/configure.PENDING;value=json.loads(path.read_bytes());value['candidateName']='../../unrelated'
        path.write_bytes(configure.canonical(value));commands=host.events.count('stop-api')
        with self.assertRaisesRegex(Refused,'CONFIGURATION_JOURNAL'):configure.recover(host)
        self.assertEqual(host.events.count('stop-api'),commands);self.assertTrue(path.exists())

    def test_recovery_failure_retains_journal_and_can_be_retried(self):
        host=self.host()
        def fail(event):
            if event=='up-api':raise Refused('HEALTH_FAILED')
        host.fault=fail
        result=configure.apply(host,host.request())
        self.assertEqual(result['code'],'CODEX_CONFIGURATION_ROLLBACK_INCOMPLETE')
        self.assertTrue((host.STATE/configure.PENDING).exists());host.fault=None
        self.assertEqual(configure.recover(host)['status'],'restored')

    def test_recovery_refuses_external_file_changes_and_preserves_them(self):
        host=self.host();self.crash_once(host,'settings-new')
        with self.assertRaises(SystemExit):configure.apply(host,host.request())
        external=configure.canonical(dict(host.initial,runnerToken='z'*40));host.settings_path.write_bytes(external)
        with self.assertRaisesRegex(Refused,'EXTERNAL_CHANGE'):configure.recover(host)
        self.assertEqual(host.settings_path.read_bytes(),external);self.assertTrue((host.STATE/configure.PENDING).exists())

    def test_unrelated_worker_restart_prevents_success_even_when_http_is_healthy(self):
        host=self.host()
        def changed(event):
            if event=='up-api':host.records['worker-general']['State']['StartedAt']='unexpected'
        host.fault=changed
        result=configure.apply(host,host.request())
        self.assertEqual(result['status'],'recovery_required')
        self.assertEqual(result['causeCode'],'CODEX_CONFIGURATION_UNRELATED_SERVICE_CHANGED')


if __name__=='__main__':unittest.main()
