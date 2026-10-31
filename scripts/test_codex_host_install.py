"""Real-file installation/recovery; host health and process census are explicit fixtures.

No Docker mutations, provider calls, root writes or remote connections.
"""
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('host_install',REPO/'server/codex_host_install.py')
install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)
URLS=['https://model.example.com/v1']


class Crash(BaseException):pass


def save(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_bytes(raw);path.chmod(0o600)


class Fixture:
    def __init__(self,folder):
        self.root=Path(folder).resolve()/'target';self.root.mkdir(mode=0o700)
        self.lockdir=self.root/'locks';self.lockdir.mkdir(mode=0o700)
        (self.root/'environments').mkdir(mode=0o700)
        (self.root/'operations/release/state').mkdir(parents=True,mode=0o700)
        self.runtime=dict(configSha256='a'*64,containers={name:dict(id=format(i+1,'064x'),
            image='sha256:'+format(i+1,'064x'),startedAt='before')for i,name in enumerate(sorted(install.SERVICES))})
        self.files={}
        for name in install.FILES:
            if name.endswith(('codex_runtime.py','codex_configure.py')):self.files[name]=None
            else:self.files[name]=('# original '+name+'\n').encode()
        for name in install.DEPENDENCIES:self.files[name]=b'original dependency\n'
        for name,raw in self.files.items():
            if raw is not None:save(self.root/name,raw)
        self.files[install.SETTINGS]=None
        self.payload={name:('# installed '+name+'\n').encode()for name in install.FILES}
        self.payload['operations/release/codex_runtime.py']=(REPO/'server/codex_runtime.py').read_bytes()
        self.rebundle()

    def rebundle(self):
        before={name:(self.root/name).read_bytes()if(self.root/name).exists()else None for name in self.files}
        manifest=dict(schemaVersion=1,scope=install.SCOPE,root=str(install.ROOT),
            files={name:dict(beforeSha256=install.digest(before[name])if before[name]is not None else None,
                afterSha256=install.digest(self.payload[name]),payload='files/'+filename)for name,filename in install.FILES.items()},
            dependencies={name:install.digest(before[name])for name in install.DEPENDENCIES},
            settingsBeforeSha256=install.digest(before[install.SETTINGS])if before[install.SETTINGS]is not None else None,
            installerSha256=install.digest((REPO/'server/codex_host_install.py').read_bytes()),sourceAuditSha256='c'*64,startupPatchSha256='d'*64)
        raw=install.canonical(manifest);self.bundle=self.root/'operations/release/host-install-bundles'/install.digest(raw)
        save(self.bundle/'manifest.json',raw)
        save(self.bundle/'install.py',(REPO/'server/codex_host_install.py').read_bytes())
        for name,filename in install.FILES.items():save(self.bundle/'files'/filename,self.payload[name])

    def installer(self):
        host=install.Installer(self.bundle,self.root,os.getuid(),os.getgid(),self.lockdir)
        host.runtime=lambda:copy.deepcopy(self.runtime)
        host.fence=lambda:None
        return host

    def before(self):return {name:raw for name,raw in self.files.items()if name not in install.DEPENDENCIES}

    def actual(self):return {name:(self.root/name).read_bytes()if(self.root/name).exists()else None for name in self.before()}


class InstallationTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory(prefix='codex-install-');self.addCleanup(temp.cleanup);return Fixture(temp.name)

    def crash(self,host,nth,kind=Crash):
        count=0
        def fault(name,raw):
            nonlocal count
            count+=1
            if count==nth:raise kind('INJECTED_FAILURE')
        host.fault=fault

    def prepare_interruption(self,f):
        host=f.installer();self.crash(host,5)
        with self.assertRaises(Crash):host.apply({'modelBaseUrls':URLS})

    def assert_before(self,f):
        self.assertEqual(f.actual(),f.before());self.assertFalse((f.root/install.PENDING).exists())

    def test_check_is_read_only_including_no_bytecode_tokens_or_locks(self):
        f=self.fixture();before={str(p):p.read_bytes()for p in f.root.rglob('*')if p.is_file()}
        self.assertEqual(f.installer().check()['status'],'verified')
        self.assertEqual(before,{str(p):p.read_bytes()for p in f.root.rglob('*')if p.is_file()})
        module=install.load(f.bundle/'files/codex_runtime.py');self.assertTrue(callable(module.settings))
        self.assertFalse(list(f.root.rglob('__pycache__')))

    def test_apply_generates_closed_distinct_tokens_and_preserves_runtime(self):
        f=self.fixture();runtime=copy.deepcopy(f.runtime);result=f.installer().apply({'modelBaseUrls':URLS})
        self.assertEqual(result['status'],'installed');self.assertEqual(runtime,f.runtime)
        for name,raw in f.payload.items():self.assertEqual((f.root/name).read_bytes(),raw)
        settings=json.loads((f.root/install.SETTINGS).read_bytes())
        self.assertEqual((settings['rolloutMode'],settings['userIds']),('closed',[]))
        self.assertEqual(settings['modelBaseUrls'],URLS);self.assertNotEqual(settings['runnerToken'],settings['egressToken'])
        output=json.dumps(result)+(f.root/install.RECEIPT).read_text()
        for key in ('runnerToken','egressToken'):self.assertNotIn(settings[key],output)
        for name in [*install.FILES,install.SETTINGS,install.RECEIPT]:self.assertEqual((f.root/name).stat().st_mode&0o777,0o600)
        self.assertFalse((f.root/install.PENDING).exists());self.assertFalse(list(f.root.rglob('*.install-*')))

    def test_initial_install_accepts_approved_root_and_versioned_endpoints_while_closed(self):
        f=self.fixture();urls=['https://model.example.com/v1','https://api.deepseek.com']
        self.assertEqual(f.installer().apply({'modelBaseUrls':urls})['status'],'installed')
        settings=json.loads((f.root/install.SETTINGS).read_bytes())
        self.assertEqual(settings['modelBaseUrls'],urls)
        self.assertEqual((settings['rolloutMode'],settings['userIds']),('closed',[]))

    def test_each_successful_write_interruption_recovers_exact_old_or_durable_new_state(self):
        f=self.fixture();events=[];host=f.installer();host.fault=lambda name,raw:events.append((name,raw));host.apply({'modelBaseUrls':URLS})
        self.assertEqual(len(events),13)
        for boundary in range(1,len(events)+1):
            with self.subTest(boundary=boundary,event=events[boundary-1][0]):
                f=self.fixture();host=f.installer();self.crash(host,boundary)
                with self.assertRaises(Crash):host.apply({'modelBaseUrls':URLS})
                committed=boundary>=11;after=f.actual()
                result=f.installer().recover()
                if committed:
                    self.assertEqual(result['status'],'not_pending'if boundary==13 else'installed')
                    self.assertEqual(f.actual(),after)
                else:
                    self.assertEqual(result['status'],'restored');self.assert_before(f)

    def test_ordinary_write_failure_restores_or_leaves_recoverable_commit(self):
        for boundary in range(1,14):
            with self.subTest(boundary=boundary):
                f=self.fixture();host=f.installer();self.crash(host,boundary,ValueError)
                if boundary==1:
                    with self.assertRaisesRegex(ValueError,'INJECTED_FAILURE'):host.apply({'modelBaseUrls':URLS})
                else:
                    result=host.apply({'modelBaseUrls':URLS})
                    self.assertEqual(result['status'],'recovery_required'if boundary>=11 else'failed')
                result=f.installer().recover()
                if boundary<11:self.assert_before(f)
                else:self.assertEqual(json.loads((f.root/install.SETTINGS).read_bytes())['rolloutMode'],'closed')

    def test_each_restore_write_interruption_is_repeatably_recoverable(self):
        f=self.fixture();self.prepare_interruption(f);events=[];host=f.installer();host.fault=lambda name,raw:events.append(name);host.recover()
        self.assertEqual(len(events),13)
        for boundary in range(1,len(events)+1):
            with self.subTest(boundary=boundary,event=events[boundary-1]):
                f=self.fixture();self.prepare_interruption(f);host=f.installer();self.crash(host,boundary)
                with self.assertRaises(Crash):host.recover()
                self.assertIn(f.installer().recover()['status'],('restored','not_pending'));self.assert_before(f)

    def test_current_external_edit_is_never_overwritten_by_recovery(self):
        f=self.fixture();self.prepare_interruption(f);name='operations/release/cnb_release.py';save(f.root/name,b'# other operator\n')
        before=f.actual()
        with self.assertRaisesRegex(ValueError,'HOST_INSTALL_EXTERNAL_CHANGE'):f.installer().recover()
        self.assertEqual(f.actual(),before);self.assertTrue((f.root/install.PENDING).exists())

    def test_missing_old_files_dependency_change_and_pending_configuration_refuse_before_journal(self):
        for mutation,code in (
            (lambda f:(f.root/install.RECEIVER).unlink(),'STALE_SOURCE'),
            (lambda f:save(f.root/'operations/release/queue-probe.mjs',b'changed'),'DEPENDENCY_CHANGED'),
            (lambda f:save(f.root/'operations/release/state/codex-configuration.pending.json',b'{}'),'CONFIGURATION_RECOVERY_REQUIRED'),
            (lambda f:save(f.root/install.PENDING,b'{}'),'INSTALL_RECOVERY_REQUIRED')):
            with self.subTest(code=code):
                f=self.fixture();mutation(f);before=f.actual()
                with self.assertRaisesRegex(ValueError,code):f.installer().apply({'modelBaseUrls':URLS})
                self.assertEqual(f.actual(),before)

    def test_payload_program_and_manifest_tampering_refuse_without_changes(self):
        for name,code in (('files/receiver.py','PAYLOAD_CHANGED'),('install.py','PROGRAM_CHANGED'),('manifest.json','MANIFEST_HASH')):
            f=self.fixture();path=f.bundle/name;path.write_bytes(path.read_bytes()+b'\n')
            with self.assertRaisesRegex(ValueError,code):f.installer().apply({'modelBaseUrls':URLS})
            self.assert_before(f)

    def test_unsafe_files_directories_links_and_owner_are_refused(self):
        for mode in ('permissions','directory','symlink','dangling','hardlink','owner','bundle_directory'):
            with self.subTest(mode=mode):
                f=self.fixture();path=f.root/install.RECEIVER;host=f.installer()
                if mode=='permissions':path.chmod(0o644)
                elif mode=='directory':path.parent.chmod(0o777)
                elif mode=='bundle_directory':(f.bundle/'files').chmod(0o777)
                elif mode in ('symlink','dangling'):
                    target=path.with_name('outside');path.rename(target);path.symlink_to(target if mode=='symlink'else path.with_name('absent'))
                elif mode=='hardlink':os.link(path,path.with_name('other-link'))
                else:host.uid=os.getuid()+1
                with self.assertRaisesRegex(ValueError,'UNSAFE|SYMLINK_REFUSED'):host.check()
                self.assertFalse((f.root/install.PENDING).exists())

    def test_real_release_and_maintenance_locks_refuse_competing_operator(self):
        for name in ('teaching-production-release.lock','teaching-production-maintenance.lock'):
            f=self.fixture();path=f.lockdir/name;fd=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError,'CONTROL_BUSY'):f.installer().apply({'modelBaseUrls':URLS})
                self.assert_before(f)
            finally:os.close(fd)
            self.assertEqual(f.installer().apply({'modelBaseUrls':URLS})['status'],'installed')

    def test_unsafe_lock_is_refused(self):
        f=self.fixture();path=f.lockdir/'teaching-production-release.lock';save(path,b'');path.chmod(0o666)
        with self.assertRaisesRegex(ValueError,'LOCK_UNSAFE'):f.installer().apply({'modelBaseUrls':URLS})
        self.assert_before(f)

    def test_duplicate_keys_unknown_fields_and_model_endpoints_refused(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE_KEY'):install.decode(b'{"x":1,"x":2}')
        for request in ({'modelBaseUrls':URLS,'root':'/tmp'}, {'modelBaseUrls':[]}, {'modelBaseUrls':['http://localhost/v1']},
                        {'modelBaseUrls':['https://user:secret@model.example.com/v1']}):
            f=self.fixture()
            with self.assertRaises(ValueError):f.installer().apply(request)
            self.assert_before(f)

    def test_existing_closed_settings_preserved_but_admission_or_endpoint_change_refused(self):
        policy=install.load(REPO/'server/codex_runtime.py')
        settings=policy.settings(dict(schemaVersion=1,runnerToken='x'*40,egressToken='y'*40,modelBaseUrls=URLS,rolloutMode='closed',userIds=[]))
        for mutate,accepted in ((lambda s:None,True),(lambda s:s.update(rolloutMode='allowlist',userIds=['teacher']),False),
                                (lambda s:s.update(modelBaseUrls=['https://other.example.com/v1']),False)):
            f=self.fixture();value=copy.deepcopy(settings);mutate(value);raw=install.canonical(value)
            save(f.root/install.SETTINGS,raw);f.files[install.SETTINGS]=raw;f.rebundle()
            if accepted:
                self.assertEqual(f.installer().apply({'modelBaseUrls':URLS})['status'],'installed')
                self.assertEqual((f.root/install.SETTINGS).read_bytes(),raw)
            else:
                with self.assertRaisesRegex(ValueError,'EXISTING_SETTINGS_REFUSED'):f.installer().apply({'modelBaseUrls':URLS})
                self.assert_before(f)

    def test_corrupt_journal_never_changes_targets(self):
        for mutate in (lambda r:r.update(bundle='f'*64),lambda r:r['files'][install.RECEIVER].update(before=install.packed(b'bad')),
                       lambda r:r['files'][install.RECEIVER].update(after=install.packed(b'bad')),
                       lambda r:r['runtime']['containers']['api'].update(id='bad')):
            f=self.fixture();self.prepare_interruption(f);path=f.root/install.PENDING;r=json.loads(path.read_bytes());mutate(r);save(path,install.canonical(r))
            before=f.actual()
            with self.assertRaisesRegex(ValueError,'JOURNAL'):f.installer().recover()
            self.assertEqual(f.actual(),before)

    def test_apply_rejects_runtime_change_recovery_reports_restart_and_refuses_identity_or_config_change(self):
        for kind in ('restart','identity','config'):
            f=self.fixture();host=f.installer();baseline=copy.deepcopy(f.runtime)
            def change(name,raw):
                if name==install.SETTINGS:
                    if kind=='restart':f.runtime['containers']['api']['startedAt']='external-restart'
                    elif kind=='identity':f.runtime['containers']['api']['id']='f'*64
                    else:f.runtime['configSha256']='f'*64
            host.fault=change
            result=host.apply({'modelBaseUrls':URLS});self.assertEqual(result['status'],'recovery_required')
            self.assertTrue((f.root/install.PENDING).exists())
            if kind=='restart':
                result=f.installer().recover();self.assertEqual(result['observedExternalRestarts'],['api']);self.assert_before(f)
            else:
                with self.assertRaisesRegex(ValueError,'APPLICATION_.*CHANGED'):f.installer().recover()
                f.runtime=baseline;self.assertEqual(f.installer().recover()['status'],'restored');self.assert_before(f)

    def test_gate_runs_in_a_real_process_and_refuses_all_commands(self):
        f=self.fixture();path=f.root/install.RECEIVER;save(path,install.GATE)
        for action in ('deploy','status','start'):
            result=subprocess.run([sys.executable,str(path),action],capture_output=True,check=False)
            self.assertEqual(result.returncode,1);value=json.loads(result.stdout)
            self.assertEqual(value['code'],'CODEX_HOST_INSTALLATION_RECOVERY_REQUIRED');self.assertEqual(result.stderr,b'')

    def test_process_fence_scope_and_timeout(self):
        f=self.fixture();host=f.installer()
        for args in ([sys.executable,str(f.root/install.STARTUP),'start'],['docker','compose','-p','teaching-production','up'],
                     ['/usr/bin/docker','compose','--project-directory',str(f.root),'config']):self.assertTrue(host.control_command(args))
        for args in (['docker','compose','-p','other','up'],['echo','teaching-production'],['python3','/other/receiver.py']):self.assertFalse(host.control_command(args))
        proc=f.root/'999999/cmdline';save(proc,b'python3\0'+str(f.root/install.RECEIVER).encode()+b'\0')
        # Exercise the real census loop; there is no Linux /proc on the test Mac.
        with patch.object(Path,'glob',return_value=[proc]),patch.object(install.time,'monotonic',side_effect=[0,151]),patch.object(install.time,'sleep')as sleep:
            with self.assertRaisesRegex(ValueError,'OLD_CONTROL_BUSY'):install.Installer.fence(host)
            sleep.assert_not_called()
        with patch.object(Path,'glob',side_effect=[[proc],[]]),patch.object(install.time,'monotonic',side_effect=[0,1]),patch.object(install.time,'sleep')as sleep:
            install.Installer.fence(host);sleep.assert_called_once_with(1)

    def test_atomic_replace_error_leaves_original_and_removes_own_temporary(self):
        f=self.fixture();before=(f.root/install.RECEIVER).read_bytes()
        with patch.object(install.os,'replace',side_effect=OSError('synthetic IO failure')):
            with self.assertRaises(OSError):f.installer().write(install.RECEIVER,b'# new\n')
        self.assertEqual((f.root/install.RECEIVER).read_bytes(),before);self.assertFalse(list(f.root.rglob('*.install-*')))


if __name__=='__main__':unittest.main()
