"""Linux root filesystem/process proof in an isolated, networkless local container.

Uses the reviewed bundle and exact archived host source bytes. The application
snapshot alone is synthetic: this does not claim a real host installation.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import uuid

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('host_installer',REPO/'server/codex_host_install.py')
contract=importlib.util.module_from_spec(spec);spec.loader.exec_module(contract)

PROGRAM=r'''
import base64,copy,io,json,os,pathlib,shutil,subprocess,sys,time,types
data=json.loads(base64.b64decode('__PAYLOAD__'))
root=pathlib.Path('/opt/teaching-ai-production')
crash_count=0
def put(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_bytes(raw);path.chmod(0o600)
def setup():
    for path in root.iterdir():
        if path.is_dir():shutil.rmtree(path)
        else:path.unlink()
    for path in pathlib.Path('/run/lock').iterdir():path.unlink()
    (root/'environments').mkdir(mode=0o700)
    (root/'operations/release/state').mkdir(parents=True,mode=0o700)
    for name,raw in data['before'].items():put(root/name,base64.b64decode(raw))
    bundle=root/'operations/release/host-install-bundles'/data['identity']
    for name,raw in data['bundle'].items():put(bundle/name,base64.b64decode(raw))
    module=types.ModuleType('root_installer');module.__file__=str(bundle/'install.py')
    exec(compile((bundle/'install.py').read_bytes(),module.__file__,'exec'),module.__dict__)
    module.Installer.runtime=lambda self:copy.deepcopy(data['runtime'])
    return module,bundle
def invoke(module,action,crash=None):
    global crash_count
    read,write=os.pipe();pid=os.fork()
    if pid==0:
        os.close(read);os.dup2(write,1);os.dup2(write,2);os.close(write)
        sys.argv=[module.__file__,action]
        sys.stdin=io.TextIOWrapper(io.BytesIO(json.dumps({'modelBaseUrls':['https://model.example.com/v1']}).encode()))
        if crash is not None:
            original=module.Installer.write;counter=[0]
            def injected(self,name,raw):
                original(self,name,raw);counter[0]+=1
                if counter[0]==crash:os._exit(99)
            module.Installer.write=injected
        try:code=module.main()
        except Exception as error:
            print(json.dumps({'status':'failed','code':module.error_code(error)}));code=1
        sys.stdout.flush();sys.stderr.flush();os._exit(code)
    os.close(write)
    with os.fdopen(read,'rb')as stream:raw=stream.read()
    _,status=os.waitpid(pid,0);code=os.waitstatus_to_exitcode(status)
    if code==99:
        assert not raw;crash_count+=1;return code,None
    return code,json.loads(raw)
def targets(module):
    return {name:(root/name).read_bytes()if(root/name).exists()else None for name in [*module.FILES,module.SETTINGS]}
def files():return {str(p):p.read_bytes()for p in root.rglob('*')if p.is_file()}
checks=[]
module,bundle=setup();before=files();code,result=invoke(module,'check')
assert code==0 and result['status']=='verified'and files()==before
assert not list(root.rglob('__pycache__'))
checks.append('root_check_read_only_exact_audited_source_and_bundle')
old=subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)',str(root/module.RECEIVER)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
started=time.monotonic();code,result=invoke(module,'apply');elapsed=time.monotonic()-started;old.wait(timeout=5)
assert code==0 and result['status']=='installed'and elapsed>=1.5
assert all((root/name).stat().st_uid==0 and (root/name).stat().st_gid==0 and (root/name).stat().st_mode&0o777==0o600 for name in [*module.FILES,module.SETTINGS,module.RECEIPT])
settings=json.loads((root/module.SETTINGS).read_bytes())
assert settings['rolloutMode']=='closed'and settings['userIds']==[]and settings['runnerToken']!=settings['egressToken']
assert not any(settings[key]in json.dumps(result)for key in('runnerToken','egressToken'))
checks.append('real_proc_fence_waits_for_old_control_and_root_apply_stays_closed')
for point in range(1,14):
    module,bundle=setup();before=targets(module);code,_=invoke(module,'apply',point);assert code==99
    after=targets(module);code,result=invoke(module,'recover');assert code==0
    assert targets(module)==(before if point<11 else after)
    assert result['status']==('restored'if point<11 else'not_pending'if point==13 else'installed')
    assert not(root/module.PENDING).exists()
checks.append('13_apply_boundaries_recover_after_real_process_exit')
for point in range(1,14):
    module,bundle=setup();before=targets(module);assert invoke(module,'apply',5)[0]==99
    assert invoke(module,'recover',point)[0]==99
    code,result=invoke(module,'recover');assert code==0 and targets(module)==before and not(root/module.PENDING).exists()
checks.append('13_restore_boundaries_recover_after_real_process_exit')
module,bundle=setup();path=root/module.RECEIVER;path.chmod(0o644);code,result=invoke(module,'check')
assert code==1 and result['code']=='HOST_INSTALL_FILE_UNSAFE'
path.chmod(0o600);path.rename(path.with_name('original.py'));path.symlink_to(path.with_name('original.py'))
code,result=invoke(module,'check');assert code==1 and result['code']=='HOST_INSTALL_SYMLINK_REFUSED'
checks.append('actual_root_permissions_and_symlink_refusal')
module,bundle=setup()
import fcntl
for name in('teaching-production-release.lock','teaching-production-maintenance.lock'):
    fd=os.open('/run/lock/'+name,os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        code,result=invoke(module,'apply');assert code==1 and result['code']=='HOST_INSTALL_CONTROL_BUSY'
        assert not(root/module.PENDING).exists()
    finally:os.close(fd)
checks.append('actual_linux_release_and_maintenance_lock_refusal')
assert crash_count==39
print(json.dumps({'status':'passed','checks':checks,'boundaryCrashes':crash_count,'procFenceSeconds':round(elapsed,3),
    'rootOwnershipVerified':'isolated-local-container-only','runtimeHealth':'synthetic-stable-eight-service-snapshot',
    'productionChanged':False,'realModelCalls':0}))
'''


def command(args,**kw):return subprocess.run(args,check=True,capture_output=True,timeout=120,**kw).stdout


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--audit-dir',type=Path,required=True);parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    endpoint=json.loads(command(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    contract.require(endpoint.startswith('unix://'),'LOCAL_DOCKER_REQUIRED')
    info=json.loads(command(['docker','image','inspect',args.image]))[0]
    contract.require(info['Os']=='linux'and info['Architecture']=='amd64','LOCAL_LINUX_AMD64_IMAGE_REQUIRED')
    bundle=args.bundle.resolve();audit=args.audit_dir.resolve()
    manifest=contract.decode((bundle/'manifest.json').read_bytes())
    contract.require(contract.digest((bundle/'manifest.json').read_bytes())==bundle.name,'BUNDLE_HASH')
    contract.require(contract.digest((audit/'installation-precondition.json').read_bytes())==manifest['sourceAuditSha256'],'AUDIT_HASH')
    contract.require(manifest['settingsBeforeSha256']is None,'INITIAL_SETTINGS_MUST_BE_ABSENT')
    before={}
    for name,row in manifest['files'].items():
        if row['beforeSha256']is not None:
            raw=(audit/Path(name).name).read_bytes();contract.require(contract.digest(raw)==row['beforeSha256'],'AUDITED_SOURCE_HASH')
            before[name]=base64.b64encode(raw).decode()
    for name,identity in manifest['dependencies'].items():
        raw=(audit/Path(name).name).read_bytes();contract.require(contract.digest(raw)==identity,'AUDITED_SOURCE_HASH')
        before[name]=base64.b64encode(raw).decode()
    payload=dict(identity=bundle.name,before=before,
        bundle={name:base64.b64encode((bundle/name).read_bytes()).decode()for name in ['install.py','manifest.json',*[r['payload']for r in manifest['files'].values()]]},
        runtime=dict(configSha256='a'*64,containers={name:dict(id=format(i+1,'064x'),image='sha256:'+format(i+1,'064x'),startedAt='before')for i,name in enumerate(sorted(contract.SERVICES))}))
    script=PROGRAM.replace('__PAYLOAD__',base64.b64encode(json.dumps(payload).encode()).decode()).encode()
    name='codex-host-install-proof-'+uuid.uuid4().hex[:12]
    try:
        raw=command(['docker','run','--rm','--pull','never','--name',name,'--platform','linux/amd64','--network','none','--read-only',
            '--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','0:0','--pids-limit','64','--memory','256m',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777',
            '--tmpfs',str(contract.ROOT)+':rw,nosuid,nodev,size=16m,mode=0700',
            '--tmpfs','/run/lock:rw,nosuid,nodev,size=1m,mode=0700','--entrypoint','/usr/bin/python3','-i',args.image,'-'],input=script)
        report=json.loads(raw)
    except subprocess.CalledProcessError as error:
        # The container receives source code and synthetic service tokens only.
        report=dict(status='failed',code='LOCAL_INSTALLER_VERIFICATION_FAILED',containerStderr=error.stderr.decode(errors='replace')[-6000:])
    finally:
        subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    remaining=command(['docker','ps','-aq','--filter','name=^/'+name+'$']).strip()
    report.update(bundleSha256=bundle.name,imageId=info['Id'],testContainer=name,cleanup=not bool(remaining),
        sourceHashes={name:hashlib.sha256((REPO/name).read_bytes()).hexdigest()for name in('server/codex_host_install.py','scripts/verify_codex_host_install.py')},
        scope='actual-Linux-root-files-locks-proc-fence-and-process-exits; application-health-is-synthetic; no-Docker-socket-network-or-production-access')
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items()if k!='containerStderr'}))
    if report['status']!='passed'or remaining:raise SystemExit(1)


if __name__=='__main__':main()
