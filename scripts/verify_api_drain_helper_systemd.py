#!/usr/bin/python3
"""Review candidate: exercise an isolated copy of the real drain helper.

The launcher must be staged together with verify_api_drain_systemd.py and
api_drain_source.py. Its fake Docker only writes private synthetic files and
has no Docker socket access. Production helper paths are never used.
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
CASES=('normal','cancel','cancel-during','dispatch-lost','caller-before','caller-during','watchdog-during','stop-lost','bounded-entry')
TEST_PREFIX='teaching-drain-fixture-'


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
import json,os,sys,time
from pathlib import Path
root=Path(%r);args=sys.argv[1:]
if args[0]=='inspect':
 print(json.dumps([json.loads((root/'api.json').read_text())]));sys.exit(0)
if args[0]=='stop':
 count=root/'stop-count';fd=os.open(count,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'a')as f:f.write('1\\n');f.flush();os.fsync(f.fileno())
 (root/'stop-started').write_text('synthetic')
 mode=(root/'mode').read_text().strip()
 if mode in ('caller-during','watchdog-during','cancel-during'):time.sleep(5)
 if mode=='stop-lost':sys.exit(1)
 value=json.loads((root/'api.json').read_text());value['State'].update(Running=False,ExitCode=0,OOMKilled=False,Restarting=False)
 temp=root/'api.tmp';temp.write_text(json.dumps(value));os.replace(temp,root/'api.json');sys.exit(0)
sys.exit(2)
'''%str(root)
    path=root/'fake-docker.py';fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as stream:stream.write(fake);stream.flush();os.fsync(stream.fileno())
    load_fixture(root)


def caller(root,case):
    module=load_fixture(root);api=json.loads((root/'api.json').read_text());cancel=[False]
    if case=='dispatch-lost':
        original=module.run
        def lost_response(args,**kwargs):
            result=original(args,**kwargs)
            if args and args[0]==module.SYSTEMD_RUN:raise TimeoutError('synthetic-dispatch-response-lost')
            return result
        module.run=lost_response
    def check_cancel():
        if cancel[0]or case=='cancel-during'and(root/'cancel-requested').exists():raise ValueError('synthetic-cancel')
    try:
        started=time.monotonic()
        with module.Fence(api,check_cancel=check_cancel)as fence:
            elapsed=time.monotonic()-started
            store(root/'checkpoint.json',{'tag':fence.tag,'case':case,'entrySeconds':elapsed})
            if case=='caller-before':time.sleep(120)
            if case=='cancel':cancel[0]=True
            if case!='bounded-entry':fence.stop()
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
    # The synthetic wildcard server exists only inside an unshare-created
    # namespace and is never exposed through a host port or network interface.
    program="import socket,time;s=socket.socket();s.bind(('0.0.0.0',4999));s.listen();time.sleep(300)"
    anchor=subprocess.Popen([base.UNSHARE,'--net','--',base.PYTHON,'-c',program],stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        time.sleep(.2);check(anchor.poll()is None,'FIXTURE_UNSHARE_FAILED')
        inode=os.stat(f'/proc/{anchor.pid}/ns/net').st_ino
        check(inode!=os.stat('/proc/1/ns/net').st_ino,'FIXTURE_NAMESPACE_REFUSED')
        base.write(root,'server',{'pid':anchor.pid,'start':base.starttime(anchor.pid),'inode':inode})
        results=[];entry_elapsed=None;selected=base.read(root,'selected.json')
        check(isinstance(selected,list)and selected and len(selected)==len(set(selected))and all(case in CASES for case in selected),
              'FIXTURE_CASE_SELECTION')
        prepare(root)
        for case in selected:
            for item in ('checkpoint.json','caller-result.json','stop-count','stop-started','api.json','mode','cancel-requested'):
                (root/item).unlink(missing_ok=True)
            api={'Id':'a'*64,'Image':'sha256:'+'b'*64,'State':{'Pid':anchor.pid,'Running':True,'StartedAt':'synthetic'}}
            store(root/'api.json',api);(root/'mode').write_text(case)
            caller_unit=name(identifier,'caller',case)
            call([base.SYSTEMD_RUN,'--quiet','--unit='+caller_unit,'--slice=system.slice',
                  '--property=RuntimeMaxSec=240s',base.PYTHON,str(root/'experiment-helper.py'),'caller',identifier,case])
            if case=='dispatch-lost':wait_path(root/'caller-result.json',30)
            else:wait_path(root/'checkpoint.json',25)
            if case=='bounded-entry':
                checkpoint=json.loads((root/'checkpoint.json').read_text())
                check(checkpoint['entrySeconds']<25,'FIXTURE_BOUNDED_ENTRY_TOO_SLOW')
                entry_elapsed=round(checkpoint['entrySeconds'],2)
            if case=='caller-before':
                call([base.SYSTEMCTL,'kill','--kill-whom=all','--signal=SIGKILL',caller_unit])
            elif case=='cancel-during':
                wait_path(root/'stop-started',15);store(root/'cancel-requested',{'cancel':True});time.sleep(1)
                current=json.loads((root/'journal.json').read_text());check(current['phase']=='stop_started','FIXTURE_STOP_ENDED_EARLY')
                module=load_fixture(root);tag=current['tag'];pin=root/(tag+'.pin');fd=os.open(pin,os.O_RDONLY|os.O_NOFOLLOW)
                try:
                    for binary in module.IPTABLES:module.require_first(binary,fd,tag)
                finally:os.close(fd)
            elif case in ('caller-during','watchdog-during'):
                wait_path(root/'stop-started',15)
                target=caller_unit if case=='caller-during'else json.loads((root/'checkpoint.json').read_text())['tag']+'.service'
                check(target==caller_unit or re.fullmatch(TEST_PREFIX+r'[0-9a-f]{32}\.service',target),'FIXTURE_UNIT_REFUSED')
                call([base.SYSTEMCTL,'kill','--kill-whom=all','--signal=SIGKILL',target])
            if case in ('normal','cancel','cancel-during','dispatch-lost','caller-before','bounded-entry'):
                deadline=time.monotonic()+30
                while (root/'journal.json').exists()and time.monotonic()<deadline:time.sleep(.2)
                check(not(root/'journal.json').exists(),'FIXTURE_CLEANUP_TIMEOUT')
                count=(root/'stop-count').read_text().splitlines()if(root/'stop-count').exists()else[]
                check(len(count)==(1 if case in ('normal','cancel-during')else 0),'FIXTURE_STOP_COUNT')
                if case=='cancel-during':
                    wait_path(root/'caller-result.json');result=json.loads((root/'caller-result.json').read_text())
                    check(result['status']=='error'and result['type']=='ValueError','FIXTURE_CANCELLATION_RESULT')
            else:
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    try:
                        if json.loads((root/'journal.json').read_text()).get('phase')=='stop_uncertain':break
                    except (FileNotFoundError,json.JSONDecodeError):pass
                    time.sleep(.2)
                count=(root/'stop-count').read_text().splitlines()if(root/'stop-count').exists()else[]
                check(json.loads((root/'journal.json').read_text())['phase']=='stop_uncertain'and len(count)==1,
                      'FIXTURE_UNKNOWN_NOT_PRESERVED')
                clean_case(root,identifier)
            call([base.SYSTEMCTL,'stop',caller_unit],allow_failure=True)
            call([base.SYSTEMCTL,'reset-failed',caller_unit],allow_failure=True)
            results.append(case)
        print(json.dumps({'status':'passed','cases':results,'fakeDockerOnly':True,'syntheticNamespaceOnly':True,
                          'tcpIdleWaitSeconds':0,'parentDeathGraceSecondsInFixture':2,
                          'boundedEntrySeconds':entry_elapsed,'realDockerDaemonTested':False,'businessTasksRun':0,'modelCalls':0}),flush=True)
    finally:
        try:clean_case(root,identifier)
        finally:
            anchor.terminate()
            try:anchor.wait(timeout=5)
            except subprocess.TimeoutExpired:anchor.kill();anchor.wait()


def cleanup(root,identifier):
    for case in CASES:
        target=name(identifier,'caller',case)
        call([base.SYSTEMCTL,'stop',target],timeout=10,allow_failure=True)
        status=call([base.SYSTEMCTL,'show','--value','-p','ActiveState',target],allow_failure=True)
        check(status.returncode==0 and status.stdout.strip()in (b'inactive',b'failed'),'FIXTURE_UNIT_CLEANUP_FAILED')
        if status.stdout.strip()==b'failed':call([base.SYSTEMCTL,'reset-failed',target])
    clean_case(root,identifier)
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
