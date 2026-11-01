#!/usr/bin/python3
"""Review candidate: real drain helper plus one scoped synthetic Docker ID.

The launcher must be staged together with verify_api_drain_systemd.py and
api_drain_source.py. A restricted wrapper validates the exact random synthetic
container before every real Docker inspect or one stop. No business containers
or application code are allowed.
"""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
def trusted_bootstrap():
    if os.geteuid()!=0 or not HERE.is_relative_to(Path('/run')) or HERE.is_symlink()or HERE.stat().st_uid!=0 or \
       HERE.stat().st_gid!=0 or stat.S_IMODE(HERE.stat().st_mode)!=0o700:
        raise RuntimeError('FIXTURE_STAGING_UNSAFE')
    program=Path(__file__)
    if program.resolve()!=program or program.is_symlink():raise RuntimeError('FIXTURE_STAGING_UNSAFE')
    required=(program.name,'verify_api_drain_systemd.py','api_drain_source.py')
    manifest=HERE/'manifest.json'
    def safe(path):
        info=path.lstat()
        if path.is_symlink()or not stat.S_ISREG(info.st_mode)or info.st_uid!=0 or info.st_gid!=0 or \
           info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600:
            raise RuntimeError('FIXTURE_STAGING_UNSAFE')
    safe(manifest)
    expected=json.loads(manifest.read_text())
    if not isinstance(expected,dict)or set(expected)!=set(required):raise RuntimeError('FIXTURE_MANIFEST_UNSAFE')
    for filename in required:
        path=HERE/filename;safe(path)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected[filename]:raise RuntimeError('FIXTURE_MANIFEST_MISMATCH')
trusted_bootstrap()
spec=importlib.util.spec_from_file_location('synthetic_base',HERE/'verify_api_drain_systemd.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
check=base.check;call=base.call;wait_path=base.wait_path
CASES=('daemon-normal','daemon-lost')
TEST_PREFIX='teaching-drain-real-fixture-'
DOCKER='/usr/bin/docker'
IMAGE='sha256:78d0480ac131e31d0149d4dd11915fd9aab46afe4aa3bad4819a13b6f8008150'
SAFE_ENV={'PATH':'/usr/local/bin:/usr/bin:/bin','NODE_VERSION':'synthetic','YARN_VERSION':'synthetic',
          'PLAYWRIGHT_BROWSERS_PATH':'/nonexistent','NODE_ENV':'test','HOST':'0.0.0.0','PORT':'4999',
          'PPT_MASTER_PYTHON_BIN':'/nonexistent'}
NODE_PROGRAM="const h=require('http');const s=h.createServer((q,r)=>r.end('synthetic'));s.listen(4999,'0.0.0.0',()=>console.log('SYNTH_READY'));process.on('SIGTERM',()=>{console.log('SYNTH_SIGNAL');setTimeout(()=>s.close(()=>process.exit(0)),5000)});"


def name(identifier,role,case=None):
    check(re.fullmatch('[0-9a-f]{32}',identifier)and role in ('outer','caller'),'FIXTURE_UNIT_REFUSED')
    if role=='outer':check(case is None,'FIXTURE_UNIT_REFUSED')
    else:check(case in CASES,'FIXTURE_UNIT_REFUSED')
    return TEST_PREFIX+identifier+'-'+role+('-'+case if case else '')+'.service'


def store(path,value):
    temp=path.with_name(path.name+'.'+secrets.token_hex(8))
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as stream:json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)


def load_fixture(root):
    path=root/'fixture.py';check(path.is_file()and not path.is_symlink()and path.stat().st_uid==0 and
                                 stat.S_IMODE(path.stat().st_mode)==0o600,'FIXTURE_PROGRAM_UNSAFE')
    spec=importlib.util.spec_from_file_location('isolated_api_drain',path)
    module=importlib.util.module_from_spec(spec);exec(compile(path.read_bytes(),str(path),'exec'),module.__dict__)
    check(module.JOURNAL==root/'journal.json'and module.RUN==root and module.DOCKER==[base.PYTHON,str(root/'fake-docker.py')],
          'FIXTURE_SCOPE_REFUSED')
    return module


def inspect_real(identifier,case,container_id):
    check(re.fullmatch('[0-9a-f]{32}',identifier)and case in CASES and re.fullmatch('[0-9a-f]{64}',container_id),
          'FIXTURE_CONTAINER_SCOPE')
    rows=json.loads(call([DOCKER,'inspect',container_id]).stdout);check(len(rows)==1,'FIXTURE_CONTAINER_SCOPE')
    item=rows[0];cfg=item['Config'];host=item['HostConfig'];env=cfg.get('Env')or[]
    check(item['Id']==container_id and item['Image']==IMAGE and cfg.get('Labels',{}).get('teaching.drain.helperfixture')==identifier and
          cfg.get('Labels',{}).get('teaching.drain.case')==case and cfg.get('User')=='65534:65534'and
          cfg.get('Entrypoint')==['/usr/local/bin/node']and cfg.get('Cmd')==['-e',NODE_PROGRAM]and
          cfg.get('Healthcheck')in (None,{'Test':['NONE']})and cfg.get('WorkingDir')=='/'and
          host.get('NetworkMode')=='none'and host.get('ReadonlyRootfs')is True and not host.get('Privileged')and
          host.get('RestartPolicy',{}).get('Name')=='no'and not item.get('Mounts')and not host.get('PortBindings')and
          not host.get('CapAdd')and 'ALL'in(host.get('CapDrop')or[])and
          any(v in ('no-new-privileges','no-new-privileges=true')for v in(host.get('SecurityOpt')or[]))and
          all(isinstance(v,str)and '='in v for v in env)and dict(v.split('=',1)for v in env)==SAFE_ENV and len(env)==len(SAFE_ENV),
          'FIXTURE_CONTAINER_UNSAFE')
    return item


def container_create(identifier,case):
    check(re.fullmatch('[0-9a-f]{32}',identifier)and case in CASES,'FIXTURE_CONTAINER_SCOPE')
    args=[DOCKER,'create','--name','teaching-drain-helper-synthetic-'+identifier+'-'+case,
          '--label','teaching.drain.helperfixture='+identifier,'--label','teaching.drain.case='+case,
          '--network','none','--read-only','--user','65534:65534','--workdir','/',
          '--cap-drop','ALL','--security-opt','no-new-privileges','--restart','no','--memory','128m','--pids-limit','32',
          *[part for key,value in SAFE_ENV.items()for part in ('--env',key+'='+value)],
          '--no-healthcheck','--entrypoint','/usr/local/bin/node',IMAGE,'-e',NODE_PROGRAM]
    container_id=call(args,timeout=30).stdout.decode().strip();inspect_real(identifier,case,container_id)
    call([DOCKER,'start',container_id],timeout=30);deadline=time.monotonic()+20
    while b'SYNTH_READY'not in call([DOCKER,'logs',container_id],allow_failure=True).stdout:
        check(time.monotonic()<deadline,'FIXTURE_CONTAINER_START_TIMEOUT');time.sleep(.2)
    item=inspect_real(identifier,case,container_id)
    check(item['State']['Running']is True,'FIXTURE_CONTAINER_START_FAILED');return item


def remove_containers(identifier):
    check(re.fullmatch('[0-9a-f]{32}',identifier),'FIXTURE_CONTAINER_SCOPE')
    for case in CASES:
        result=call([DOCKER,'ps','-aq','--no-trunc','--filter','label=teaching.drain.helperfixture='+identifier,
                     '--filter','label=teaching.drain.case='+case],allow_failure=True)
        check(result.returncode==0,'FIXTURE_CONTAINER_QUERY_FAILED');ids=result.stdout.decode().split()
        check(len(ids)<=1,'FIXTURE_CONTAINER_SCOPE')
        for container_id in ids:
            inspect_real(identifier,case,container_id);call([DOCKER,'rm','--force',container_id],timeout=30)


def prepare(root):
    check(root.is_relative_to(base.BASE)and not str(root).startswith('/opt/teaching-ai-production/'),'FIXTURE_SCOPE_REFUSED')
    source=(root/'api_drain_source.py').read_text()
    substitutions={
      "DOCKER=['/usr/bin/docker','--host','unix:///var/run/docker.sock']":"DOCKER=["+repr(base.PYTHON)+","+repr(str(root/'fake-docker.py'))+"]",
      "JOURNAL=Path('/opt/teaching-ai-production/operations/release/state/api-drain.pending.json')":"JOURNAL=Path("+repr(str(root/'journal.json'))+")",
      "RUN=Path('/run/teaching-production')":"RUN=Path("+repr(str(root))+")",
      "time.sleep(50)":"time.sleep(2)",
      "deadline=time.monotonic()+90":"deadline=time.monotonic()+15",
      "teaching-api-drain-":TEST_PREFIX,
    }
    for old,new in substitutions.items():
        expected=5 if old=='teaching-api-drain-'else 2 if old=='deadline=time.monotonic()+90'else 1
        check(source.count(old)==expected,'FIXTURE_SUBSTITUTION_REFUSED')
        source=source.replace(old,new)
    check('/var/run/docker.sock'not in source and '/opt/teaching-ai-production'not in source,
          'FIXTURE_SCOPE_REFUSED')
    path=root/'fixture.py';fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as stream:stream.write(source);stream.flush();os.fsync(stream.fileno())
    fake='''#!/usr/bin/python3
import importlib.util,json,os,signal,subprocess,sys,time
from pathlib import Path
root=Path(%r);spec=importlib.util.spec_from_file_location('synthetic_scope',root/'experiment-helper.py')
scope=importlib.util.module_from_spec(spec);spec.loader.exec_module(scope)
allowed=json.loads((root/'allowed.json').read_text());identifier=allowed['identifier'];case=allowed['case'];identity=allowed['id'];args=sys.argv[1:]
if args not in (['inspect',identity],['stop','--time','30',identity]):sys.exit(2)
item=scope.inspect_real(identifier,case,identity)
if args[0]=='inspect':print(json.dumps([item]));sys.exit(0)
count=root/'stop-count';fd=os.open(count,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'a')as f:f.write('1\\n');f.flush();os.fsync(f.fileno())
if len(count.read_text().splitlines())!=1:sys.exit(3)
process=subprocess.Popen([scope.DOCKER,'stop','--time','30',identity],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
if case=='daemon-lost':
 pidfd=os.pidfd_open(process.pid)
 deadline=time.monotonic()+15
 while b'SYNTH_SIGNAL'not in scope.call([scope.DOCKER,'logs',identity],allow_failure=True).stdout:
  if process.poll()is not None or time.monotonic()>=deadline:sys.exit(4)
  time.sleep(.1)
 if process.poll()is not None:sys.exit(5)
 signal.pidfd_send_signal(pidfd,signal.SIGKILL);os.close(pidfd);process.wait(timeout=5);sys.exit(1)
stdout,stderr=process.communicate(timeout=40);sys.stdout.buffer.write(stdout);sys.exit(process.returncode)
'''%str(root)
    path=root/'fake-docker.py';fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as stream:stream.write(fake);stream.flush();os.fsync(stream.fileno())
    load_fixture(root)


def caller(root,case):
    module=load_fixture(root);api=json.loads((root/'api.json').read_text())
    try:
        with module.Fence(api)as fence:
            store(root/'checkpoint.json',{'tag':fence.tag,'case':case})
            fence.stop()
        store(root/'caller-result.json',{'case':case,'status':'complete'})
    except Exception as error:
        store(root/'caller-result.json',{'case':case,'status':'error','type':type(error).__name__})


def clean_case(root,identifier):
    journal=root/'journal.json'
    if not journal.exists():return
    info=json.loads(journal.read_text());tag=info.get('tag','')
    check(re.fullmatch(TEST_PREFIX+'[0-9a-f]{32}',tag),'FIXTURE_TAG_REFUSED')
    watchdog=tag+'.service';call([base.SYSTEMCTL,'stop',watchdog],timeout=10,allow_failure=True)
    status=call([base.SYSTEMCTL,'show','--value','-p','ActiveState',watchdog],allow_failure=True)
    check(status.returncode==0 and status.stdout.strip()in (b'inactive',b'failed'),'FIXTURE_WATCHDOG_STILL_ACTIVE')
    module=load_fixture(root);pin=root/(tag+'.pin');anchor=base.read(root,'server')
    check(info['namespaceInode']==anchor['inode'],'FIXTURE_NAMESPACE_REFUSED')
    if pin.exists()and pin.stat().st_ino==anchor['inode']:
        fd=os.open(pin,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            for binary in module.IPTABLES:module.remove_if_present(binary,fd,tag)
        finally:os.close(fd)
        call([module.UMOUNT,str(pin)])
    if pin.exists():check(pin.is_file()and not pin.is_symlink()and pin.stat().st_uid==0,'FIXTURE_PIN_UNSAFE');pin.unlink()
    for suffix in ('ready','release','cancel','lock'):
        path=root/(tag+'.'+suffix)
        if path.exists():check(path.is_file()and not path.is_symlink()and path.stat().st_uid==0,'FIXTURE_MARKER_UNSAFE');path.unlink()
    journal.unlink()


def run_test(root,identifier):
    check(os.stat('/proc/self/ns/net').st_ino==os.stat('/proc/1/ns/net').st_ino,'FIXTURE_HOST_NAMESPACE_REQUIRED')
    try:
        images=json.loads(call([DOCKER,'image','inspect',IMAGE]).stdout)
        check(len(images)==1 and images[0]['Id']==IMAGE and not images[0].get('Config',{}).get('Volumes'),
              'FIXTURE_IMAGE_REFUSED')
        results=[];selected=base.read(root,'selected.json')
        check(isinstance(selected,list)and selected and len(selected)==len(set(selected))and all(case in CASES for case in selected),
              'FIXTURE_CASE_SELECTION')
        prepare(root)
        for case in selected:
            for item in ('checkpoint.json','caller-result.json','stop-count','api.json','allowed.json','server'):
                (root/item).unlink(missing_ok=True)
            api=container_create(identifier,case);pid=api['State']['Pid'];inode=os.stat(f'/proc/{pid}/ns/net').st_ino
            check(inode!=os.stat('/proc/1/ns/net').st_ino,'FIXTURE_NAMESPACE_REFUSED')
            base.write(root,'server',{'pid':pid,'start':base.starttime(pid),'inode':inode})
            store(root/'api.json',api);store(root/'allowed.json',{'identifier':identifier,'case':case,'id':api['Id']})
            caller_unit=name(identifier,'caller',case)
            call([base.SYSTEMD_RUN,'--quiet','--unit='+caller_unit,'--slice=system.slice',
                  '--property=RuntimeMaxSec=240s',base.PYTHON,str(root/'experiment-helper.py'),'caller',identifier,case])
            wait_path(root/'checkpoint.json',25);wait_path(root/'caller-result.json',35)
            result=json.loads((root/'caller-result.json').read_text())
            count=(root/'stop-count').read_text().splitlines()if(root/'stop-count').exists()else[]
            check(len(count)==1,'FIXTURE_STOP_COUNT')
            if case=='daemon-normal':
                check(result['status']=='complete'and not(root/'journal.json').exists(),'FIXTURE_NORMAL_FAILED')
            else:
                check(result['status']=='error'and json.loads((root/'journal.json').read_text())['phase']=='stop_uncertain',
                      'FIXTURE_UNKNOWN_NOT_PRESERVED')
            deadline=time.monotonic()+30
            while True:
                state=inspect_real(identifier,case,api['Id'])['State']
                if state.get('Running')is False:break
                check(time.monotonic()<deadline,'FIXTURE_DAEMON_TIMEOUT');time.sleep(.2)
            check(state.get('ExitCode')==0 and not state.get('OOMKilled'),'FIXTURE_STOP_UNCONFIRMED')
            if case=='daemon-lost':clean_case(root,identifier)
            call([base.SYSTEMCTL,'stop',caller_unit],allow_failure=True)
            call([base.SYSTEMCTL,'reset-failed',caller_unit],allow_failure=True)
            remove_containers(identifier)
            results.append(case)
        print(json.dumps({'status':'passed','cases':results,'realDockerDaemon':True,'syntheticContainersOnly':True,
                          'tcpIdleWaitSeconds':0,'stopDispatchesPerContainer':1,
                          'businessTasksRun':0,'modelCalls':0}),flush=True)
    finally:
        try:clean_case(root,identifier)
        finally:remove_containers(identifier)


def cleanup(root,identifier):
    for case in CASES:
        target=name(identifier,'caller',case)
        call([base.SYSTEMCTL,'stop',target],timeout=10,allow_failure=True)
        status=call([base.SYSTEMCTL,'show','--value','-p','ActiveState',target],allow_failure=True)
        check(status.returncode==0 and status.stdout.strip()in (b'inactive',b'failed'),'FIXTURE_UNIT_CLEANUP_FAILED')
        if status.stdout.strip()==b'failed':call([base.SYSTEMCTL,'reset-failed',target])
    clean_case(root,identifier)
    remove_containers(identifier)
    check(root.parent==base.BASE and root.name==identifier and not root.is_symlink(),'FIXTURE_CLEANUP_SCOPE')
    shutil.rmtree(root)


def launch(source_dir,selected):
    check(os.geteuid()==0 and os.stat('/proc/self/ns/net').st_ino==os.stat('/proc/1/ns/net').st_ino,
          'FIXTURE_ROOT_OR_NAMESPACE_REFUSED')
    base.BASE.mkdir(mode=0o700,exist_ok=True)
    check(not base.BASE.is_symlink()and base.BASE.resolve()==base.BASE and base.BASE.stat().st_uid==0 and
          stat.S_IMODE(base.BASE.stat().st_mode)==0o700,'FIXTURE_BASE_UNSAFE')
    check(Path(source_dir).resolve()==HERE,'FIXTURE_SOURCE_REFUSED')
    identifier=secrets.token_hex(16)
    units=[name(identifier,'outer')]+[name(identifier,'caller',case)for case in CASES]
    for target in units:
        status=call([base.SYSTEMCTL,'show','--value','-p','LoadState',target],allow_failure=True)
        check(status.returncode==0 and status.stdout.strip()==b'not-found','FIXTURE_UNIT_COLLISION')
    check(selected and len(selected)==len(set(selected))and all(case in CASES for case in selected),'FIXTURE_CASE_SELECTION')
    root=base.BASE/identifier;root.mkdir(mode=0o700)
    store(root/'selected.json',selected)
    for filename in ('verify_api_drain_systemd.py','api_drain_source.py'):
        source=Path(source_dir)/filename
        check(source.is_file()and source.resolve()==source and not str(source).startswith('/opt/teaching-ai-production/'),
              'FIXTURE_SOURCE_REFUSED')
        dest=root/filename;fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb')as stream:stream.write(source.read_bytes())
    target=root/'experiment-helper.py';fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb')as stream:stream.write(Path(__file__).read_bytes())
    hashes={filename:hashlib.sha256((root/filename).read_bytes()).hexdigest()for filename in
            ('experiment-helper.py','verify_api_drain_systemd.py','api_drain_source.py')}
    store(root/'manifest.json',hashes)
    outer=name(identifier,'outer')
    try:
        call([base.SYSTEMD_RUN,'--quiet','--unit='+outer,'--slice=system.slice','--property=RuntimeMaxSec=300s',
              '--property=ExecStopPost='+base.PYTHON+' '+str(target)+' cleanup '+identifier,
              base.PYTHON,str(target),'run',identifier])
    except BaseException:
        state=call([base.SYSTEMCTL,'show','--value','-p','LoadState',outer],allow_failure=True)
        if state.returncode==0 and state.stdout.strip()==b'not-found':shutil.rmtree(root)
        raise
    print(json.dumps({'unit':outer,'id':identifier}))


def main():
    check(len(sys.argv)>=2,'FIXTURE_ARGUMENTS')
    if sys.argv[1]=='launch':
        check(len(sys.argv)in (3,4),'FIXTURE_ARGUMENTS')
        launch(sys.argv[2],[sys.argv[3]]if len(sys.argv)==4 else list(CASES));return
    check(len(sys.argv)>=3,'FIXTURE_ARGUMENTS');identifier=sys.argv[2];root=base.context(identifier)
    if sys.argv[1]=='run':check(len(sys.argv)==3,'FIXTURE_ARGUMENTS');run_test(root,identifier);return
    if sys.argv[1]=='cleanup':check(len(sys.argv)==3,'FIXTURE_ARGUMENTS');cleanup(root,identifier);return
    if sys.argv[1]=='caller':check(len(sys.argv)==4 and sys.argv[3]in CASES,'FIXTURE_ARGUMENTS');caller(root,sys.argv[3]);return
    raise RuntimeError('FIXTURE_ARGUMENTS')


if __name__=='__main__':
    try:main()
    except Exception as error:
        code=str(error)if re.fullmatch('FIXTURE_[A-Z_]+',str(error))else type(error).__name__
        print(json.dumps({'status':'failed','code':code,'syntheticOnly':True}),flush=True);sys.exit(1)
