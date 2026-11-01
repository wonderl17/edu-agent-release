#!/usr/bin/python3
"""Review candidate: synthetic namespace/systemd watchdog crash experiment.

No application, business task, production namespace or provider is accessed.
An outer transient unit owns one unshare-created netns anchor. Every network
command enters that exact inode-pinned namespace. ExecStopPost stops
only the uniquely named synthetic child units and removes only its own run dir.
"""
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

BASE=Path('/run/teaching-drain-synthetic')
IP='/usr/sbin/ip';IPTABLES=('/usr/sbin/iptables','/usr/sbin/ip6tables')
NSENTER='/usr/bin/nsenter';SYSTEMD_RUN='/usr/bin/systemd-run';SYSTEMCTL='/usr/bin/systemctl'
PYTHON='/usr/bin/python3';UNSHARE='/usr/bin/unshare';SLEEP='/usr/bin/sleep'
CASES=('marker','ready','ipv4','dual')


def check(value,code):
    if not value:raise RuntimeError(code)


def call(args,timeout=15,allow_failure=False,pass_fds=()):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          timeout=timeout,pass_fds=pass_fds,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
    if not allow_failure:check(result.returncode==0,'SYNTHETIC_COMMAND_FAILED')
    return result


def context(identifier):
    check(os.geteuid()==0 and re.fullmatch('[0-9a-f]{32}',identifier),'SYNTHETIC_IDENTIFIER_REFUSED')
    check(BASE.is_dir()and not BASE.is_symlink()and BASE.resolve()==BASE,'SYNTHETIC_BASE_UNSAFE')
    check(BASE.stat().st_uid==0 and stat.S_IMODE(BASE.stat().st_mode)==0o700,'SYNTHETIC_BASE_UNSAFE')
    root=BASE/identifier
    check(root.is_dir()and not root.is_symlink()and root.resolve()==root and root.stat().st_uid==0 and
          stat.S_IMODE(root.stat().st_mode)==0o700,'SYNTHETIC_DIRECTORY_UNSAFE')
    return root


def unit(identifier,role,case=None):
    check(re.fullmatch('[0-9a-f]{32}',identifier)and role in ('outer','caller','watchdog'),'SYNTHETIC_UNIT_REFUSED')
    if role=='outer':check(case is None,'SYNTHETIC_UNIT_REFUSED')
    else:check(case in CASES,'SYNTHETIC_UNIT_REFUSED')
    return 'teaching-drain-synthetic-'+identifier+'-'+role+('-'+case if case else '')+'.service'


def write(root,name,value):
    check(re.fullmatch('[a-z0-9.-]+',name),'SYNTHETIC_FILE_REFUSED')
    target=root/name;check(not target.exists()and not target.is_symlink(),'SYNTHETIC_FILE_EXISTS')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as stream:json.dump(value,stream);stream.flush();os.fsync(stream.fileno())


def read(root,name):
    path=root/name;check(not path.is_symlink()and path.is_file()and path.stat().st_uid==0 and
                         stat.S_IMODE(path.stat().st_mode)==0o600 and path.stat().st_size<8192,'SYNTHETIC_FILE_UNSAFE')
    return json.loads(path.read_text())


def starttime(pid):
    data=Path('/proc',str(pid),'stat').read_text();return int(data[data.rfind(')')+2:].split()[19])


def live(pid,start):
    try:return starttime(pid)==start
    except (FileNotFoundError,ProcessLookupError):return False


def anchor(root,name):
    check(name=='server','SYNTHETIC_NAMESPACE_REFUSED')
    info=read(root,name);pid=info['pid'];inode=info['inode']
    check(type(pid)is int and pid>1 and live(pid,info['start']) and os.stat(f'/proc/{pid}/ns/net').st_ino==inode and
          inode!=os.stat('/proc/1/ns/net').st_ino,'SYNTHETIC_NAMESPACE_CHANGED')
    return pid,inode


def network(root,name,args):
    pid,inode=anchor(root,name)
    check(args and args[0] in (IP,*IPTABLES,PYTHON),'SYNTHETIC_COMMAND_REFUSED')
    fd=os.open(f'/proc/{pid}/ns/net',os.O_RDONLY|os.O_CLOEXEC)
    try:
        check(os.fstat(fd).st_ino==inode and inode!=os.stat('/proc/1/ns/net').st_ino,'SYNTHETIC_NAMESPACE_CHANGED')
        result=call([NSENTER,'--net=/proc/self/fd/'+str(fd),'--',*args],pass_fds=(fd,))
        anchor(root,name)
        return result
    finally:os.close(fd)


def rules(root,binary,op,tag):
    check(binary in IPTABLES and op in ('-I','-C','-D')and re.fullmatch('edrain-[0-9a-f]{32}-(ready|ipv4|dual)',tag),
          'SYNTHETIC_RULE_REFUSED')
    args=[binary,'-w','5',op,'INPUT']+(['1']if op=='-I'else[])+['!','-i','lo','-p','tcp','--dport','4999',
          '--syn','-m','comment','--comment',tag,'-j','REJECT']
    return network(root,'server',args)


def remove_own(root,binary,tag):
    result=network(root,'server',[binary,'-w','5','-S','INPUT']).stdout.decode()
    lines=[line for line in result.splitlines() if tag in line]
    check(len(lines)<=1,'SYNTHETIC_DUPLICATE_RULE')
    if lines:rules(root,binary,'-C',tag);rules(root,binary,'-D',tag)
    result=network(root,'server',[binary,'-w','5','-S','INPUT']).stdout.decode()
    check(tag not in result,'SYNTHETIC_RULE_STILL_PRESENT')


def wait_path(path,seconds=15):
    end=time.monotonic()+seconds
    while not path.is_file():
        check(time.monotonic()<end,'SYNTHETIC_TIMEOUT');time.sleep(.1)


def watchdog(root,identifier,case,parent_pid,parent_start):
    check(case in ('ready','ipv4','dual')and live(parent_pid,parent_start),'SYNTHETIC_PARENT_INVALID')
    anchor(root,'server');tag='edrain-'+identifier+'-'+case
    write(root,case+'.watchdog-ready',{'pid':os.getpid()})
    deadline=time.monotonic()+45
    while live(parent_pid,parent_start) and time.monotonic()<deadline:time.sleep(.1)
    check(not live(parent_pid,parent_start),'SYNTHETIC_PARENT_DID_NOT_EXIT')
    # Short synthetic grace demonstrates independent cleanup; production's
    # complete stop/watchdog grace must be checked in a separate exact test.
    time.sleep(2)
    for binary in IPTABLES:remove_own(root,binary,tag)
    write(root,case+'.watchdog-done',{'cleaned':True})


def caller(root,identifier,case):
    check(case in CASES,'SYNTHETIC_CASE_REFUSED')
    write(root,case+'.journal',{'case':case})
    if case=='marker':
        write(root,case+'.checkpoint',{'point':'marker'})
        time.sleep(120);return
    script=root/'experiment.py';tag='edrain-'+identifier+'-'+case
    parent_pid=os.getpid();parent_start=starttime(parent_pid)
    call([SYSTEMD_RUN,'--quiet','--unit='+unit(identifier,'watchdog',case),'--slice=system.slice',
          '--property=RuntimeMaxSec=60s',PYTHON,str(script),'watchdog',identifier,case,str(parent_pid),str(parent_start)])
    wait_path(root/(case+'.watchdog-ready'))
    if case=='ready':write(root,case+'.checkpoint',{'point':'ready'});time.sleep(120);return
    rules(root,IPTABLES[0],'-I',tag)
    if case=='ipv4':write(root,case+'.checkpoint',{'point':'ipv4'});time.sleep(120);return
    rules(root,IPTABLES[1],'-I',tag)
    write(root,case+'.checkpoint',{'point':'dual'});time.sleep(120)


def run_test(root,identifier):
    anchors=[]
    try:
        check(os.stat('/proc/self/ns/net').st_ino==os.stat('/proc/1/ns/net').st_ino,
              'SYNTHETIC_HOST_NAMESPACE_REQUIRED')
        for name in ('server',):
            proc=subprocess.Popen([UNSHARE,'--net','--',SLEEP,'300'],stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            anchors.append(proc);time.sleep(.2)
            check(proc.poll()is None,'SYNTHETIC_UNSHARE_FAILED')
            inode=os.stat(f'/proc/{proc.pid}/ns/net').st_ino
            write(root,name,{'pid':proc.pid,'start':starttime(proc.pid),'inode':inode})
        anchor(root,'server')
        # No interfaces or routes are created. Only this private netns's own
        # INPUT chains are exercised; there is no synthetic network traffic.
        # Foreign tags on an unrelated synthetic port must survive every cleanup.
        foreign='edrain-foreign-'+identifier
        for binary in IPTABLES:
            network(root,'server',[binary,'-w','5','-I','INPUT','1','-p','tcp','--dport','5999','-m','comment','--comment',foreign,'-j','REJECT'])
        results=[];script=root/'experiment.py'
        for case in CASES:
            call([SYSTEMD_RUN,'--quiet','--unit='+unit(identifier,'caller',case),'--slice=system.slice',
                  '--property=RuntimeMaxSec=130s',PYTHON,str(script),'caller',identifier,case])
            wait_path(root/(case+'.checkpoint'))
            if case!='marker':
                caller_group=call([SYSTEMCTL,'show','--value','-p','ControlGroup',unit(identifier,'caller',case)]).stdout.strip()
                watcher_group=call([SYSTEMCTL,'show','--value','-p','ControlGroup',unit(identifier,'watchdog',case)]).stdout.strip()
                check(caller_group and watcher_group and caller_group!=watcher_group,'SYNTHETIC_CGROUP_NOT_INDEPENDENT')
            call([SYSTEMCTL,'kill','--kill-whom=all','--signal=SIGKILL',unit(identifier,'caller',case)])
            if case!='marker':wait_path(root/(case+'.watchdog-done'),15)
            tag='edrain-'+identifier+'-'+case
            for binary in IPTABLES:
                data=network(root,'server',[binary,'-w','5','-S','INPUT']).stdout.decode()
                check(tag not in data and foreign in data,'SYNTHETIC_CLEANUP_SCOPE_FAILED')
            results.append(case)
        print(json.dumps({'status':'passed','cases':results,'independentCgroups':True,'foreignRulesPreserved':True,
                          'productionNamespaceAccessed':False,'businessTasksRun':0,'modelCalls':0}),flush=True)
    finally:
        for proc in anchors:proc.terminate()
        for proc in anchors:
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()


def cleanup(root,identifier):
    for case in CASES:
        for role in ('caller','watchdog'):
            child=unit(identifier,role,case)
            call([SYSTEMCTL,'stop',child],timeout=10,allow_failure=True)
            status=call([SYSTEMCTL,'show','--value','-p','ActiveState',child],timeout=10,allow_failure=True)
            check(status.returncode==0 and status.stdout.strip()in (b'inactive',b'failed'),
                  'SYNTHETIC_UNIT_CLEANUP_FAILED')
            if status.stdout.strip()==b'failed':
                call([SYSTEMCTL,'reset-failed',child],timeout=10)
    check(root.parent==BASE and root.name==identifier and not root.is_symlink(),'SYNTHETIC_CLEANUP_SCOPE')
    shutil.rmtree(root)


def launch():
    check(os.geteuid()==0,'SYNTHETIC_ROOT_REQUIRED')
    check(os.stat('/proc/self/ns/net').st_ino==os.stat('/proc/1/ns/net').st_ino,
          'SYNTHETIC_HOST_NAMESPACE_REQUIRED')
    BASE.mkdir(mode=0o700,exist_ok=True)
    check(not BASE.is_symlink()and BASE.resolve()==BASE and BASE.stat().st_uid==0 and
          stat.S_IMODE(BASE.stat().st_mode)==0o700,'SYNTHETIC_BASE_UNSAFE')
    identifier=secrets.token_hex(16)
    candidates=[unit(identifier,'outer')]+[unit(identifier,role,case)for case in CASES for role in ('caller','watchdog')]
    for candidate in candidates:
        state=call([SYSTEMCTL,'show','--value','-p','LoadState',candidate],allow_failure=True)
        check(state.returncode==0 and state.stdout.strip()==b'not-found','SYNTHETIC_UNIT_EXISTS_OR_UNAVAILABLE')
    root=BASE/identifier;root.mkdir(mode=0o700)
    script=root/'experiment.py'
    fd=os.open(script,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb')as stream:stream.write(Path(__file__).read_bytes());stream.flush();os.fsync(stream.fileno())
    outer=unit(identifier,'outer')
    try:
        call([SYSTEMD_RUN,'--quiet','--unit='+outer,'--slice=system.slice','--property=RuntimeMaxSec=240s',
              '--property=ExecStopPost='+PYTHON+' '+str(script)+' cleanup '+identifier,
              PYTHON,str(script),'run',identifier])
    except BaseException:
        # A failed systemd-run response does not prove the unit was not
        # created. Preserve its program and cleanup hook if state is unknown.
        state=call([SYSTEMCTL,'show','--value','-p','LoadState',outer],allow_failure=True)
        if state.returncode==0 and state.stdout.strip()==b'not-found':shutil.rmtree(root)
        raise
    print(json.dumps({'unit':outer,'id':identifier,'privateRunDirectory':str(root)}))


def main():
    check(len(sys.argv)>=2,'SYNTHETIC_MODE_REQUIRED')
    if sys.argv[1]=='launch':check(len(sys.argv)==2,'SYNTHETIC_ARGUMENTS');launch();return
    check(len(sys.argv)>=3,'SYNTHETIC_ARGUMENTS');identifier=sys.argv[2];root=context(identifier)
    if sys.argv[1]=='run':check(len(sys.argv)==3,'SYNTHETIC_ARGUMENTS');run_test(root,identifier);return
    if sys.argv[1]=='cleanup':check(len(sys.argv)==3,'SYNTHETIC_ARGUMENTS');cleanup(root,identifier);return
    if sys.argv[1]=='caller':check(len(sys.argv)==4,'SYNTHETIC_ARGUMENTS');caller(root,identifier,sys.argv[3]);return
    if sys.argv[1]=='watchdog':
        check(len(sys.argv)==6 and sys.argv[4].isdigit()and sys.argv[5].isdigit(),'SYNTHETIC_ARGUMENTS')
        watchdog(root,identifier,sys.argv[3],int(sys.argv[4]),int(sys.argv[5]));return
    raise RuntimeError('SYNTHETIC_MODE_REFUSED')


if __name__=='__main__':
    try:main()
    except Exception as error:
        code=str(error)if re.fullmatch('SYNTHETIC_[A-Z_]+',str(error))else type(error).__name__
        print(json.dumps({'status':'failed','code':code,'productionNamespaceAccessed':False,'businessTasksRun':0,'modelCalls':0}),flush=True)
        sys.exit(1)
