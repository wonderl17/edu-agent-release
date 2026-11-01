#!/usr/bin/python3
"""Narrow, fail-closed TCP admission fence for the existing Production API.

This module is a reviewed candidate, not an installation command. The caller
must hold both production control locks, provide the already inspected API
container, and retain its ordinary clean-exit and rollback checks.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time

DOCKER=['/usr/bin/docker','--host','unix:///var/run/docker.sock']
NSENTER='/usr/bin/nsenter'
IPTABLES=('/usr/sbin/iptables','/usr/sbin/ip6tables')
PYTHON='/usr/bin/python3'
SYSTEMD_RUN='/usr/bin/systemd-run'
SYSTEMCTL='/usr/bin/systemctl'
MOUNT='/usr/bin/mount'
UMOUNT='/usr/bin/umount'
PORT=4999
VERIFIED_KERNEL='6.8.0-124-generic'
COOKIE_GRACE_SECONDS=125
JOURNAL=Path('/opt/teaching-ai-production/operations/release/state/api-drain.pending.json')
RUN=Path('/run/teaching-production')


class DrainError(Exception):pass


def require(value,code):
    if not value:raise DrainError(code)


def run(args,*,fd=None,timeout=10):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          timeout=timeout,pass_fds=(() if fd is None else (fd,)),
                          env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
    require(result.returncode==0,'API_DRAIN_COMMAND_FAILED')
    return result.stdout


def rule(binary,fd,operation,tag):
    require(binary in IPTABLES and operation in ('-I','-C','-D')and re.fullmatch('teaching-api-drain-[0-9a-f]{32}',tag),
            'API_DRAIN_RULE_REFUSED')
    # SYN packets cannot carry a request to an accepted socket when server TFO
    # is disabled. Do not use conntrack state: pre-existing IPv6 flows may be
    # untracked and would otherwise be broken by a strict ESTABLISHED rule.
    args=[NSENTER,'--net=/proc/self/fd/'+str(fd),binary,'-w','5',operation,'INPUT']
    if operation=='-I':args.append('1')
    args+=['!','-i','lo','-p','tcp','--dport',str(PORT),'--syn','-m','comment','--comment',tag,'-j','REJECT']
    run(args,fd=fd,timeout=10)


def remove_if_present(binary,fd,tag):
    def tags():
        data=run([NSENTER,'--net=/proc/self/fd/'+str(fd),binary,'-w','5','-S','INPUT'],fd=fd)
        return data.decode('utf-8','strict').splitlines()
    matches=[line for line in tags() if tag in line]
    require(len(matches)<=1,'API_DRAIN_DUPLICATE_RULE')
    if not matches:return
    rule(binary,fd,'-C',tag)
    rule(binary,fd,'-D',tag)
    require(not any(tag in line for line in tags()),'API_DRAIN_CLEANUP_FAILED')


def marker_exists(tag,suffix):
    path=marker(tag,suffix)
    if not path.exists():
        require(not path.is_symlink(),'API_DRAIN_MARKER_UNSAFE');return False
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_gid==0 and info.st_nlink==1 and
            stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=256 and not path.is_symlink()and
            json.loads(path.read_text())=={'tag':tag},'API_DRAIN_MARKER_UNSAFE')
    return True


def write_marker(tag,suffix):
    path=marker(tag,suffix)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as stream:json.dump({'tag':tag},stream);stream.flush();os.fsync(stream.fileno())


def require_first(binary,fd,tag):
    data=run([NSENTER,'--net=/proc/self/fd/'+str(fd),binary,'-w','5','-S','INPUT'],fd=fd).decode('utf-8','strict')
    entries=[line for line in data.splitlines()if line.startswith('-A INPUT ')]
    require(entries and tag in entries[0] and sum(tag in line for line in entries)==1,'API_DRAIN_RULE_NOT_FIRST')
    rule(binary,fd,'-C',tag)


def require_no_server_fastopen(fd):
    value=run([NSENTER,'--net=/proc/self/fd/'+str(fd),'/usr/bin/cat','/proc/sys/net/ipv4/tcp_fastopen'],fd=fd)
    require(int(value.strip())&2==0,'API_DRAIN_TCP_FASTOPEN_ENABLED')


def socket_entries(text):
    """Count all target TCP sockets except LISTEN and already dead TIME_WAIT.

    Include loopback probes and transient/unknown states. This deliberately
    requires an actually idle instant rather than guessing which clients are
    safe. Input is /proc/<pinned-pid>/net/tcp or tcp6, never returned in logs.
    """
    lines=text.splitlines();require(bool(lines),'API_DRAIN_SOCKET_TABLE_INVALID');entries=[]
    for line in lines[1:]:
        fields=line.split();require(len(fields)>=4,'API_DRAIN_SOCKET_TABLE_INVALID')
        local=fields[1].split(':');require(len(local)==2 and re.fullmatch('[0-9A-Fa-f]{4}',local[1]),'API_DRAIN_SOCKET_TABLE_INVALID')
        state=fields[3];require(re.fullmatch('[0-9A-Fa-f]{2}',state),'API_DRAIN_SOCKET_TABLE_INVALID')
        if int(local[1],16)==PORT:
            require(len(fields)>9 and fields[9].isdigit() and re.fullmatch('[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32}',local[0]),
                    'API_DRAIN_SOCKET_TABLE_INVALID')
            entries.append((local[0].upper(),state.upper(),fields[9]))
    return entries


def proc_start(pid):
    raw=Path('/proc',str(pid),'stat').read_text()
    return int(raw[raw.rfind(')')+2:].split()[19])


def boot_id():
    value=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    require(re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',value),'API_DRAIN_BOOT_ID_INVALID')
    return value


def process_alive(pid,start):
    try:return proc_start(pid)==start
    except (FileNotFoundError,ProcessLookupError):return False


def marker(tag,suffix):
    require(re.fullmatch('teaching-api-drain-[0-9a-f]{32}',tag)and suffix in ('pin','ready','release','lock','cancel'),'API_DRAIN_MARKER_REFUSED')
    return RUN/(tag+'.'+suffix)


def read_journal():
    fd=os.open(JOURNAL,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r')as stream:
        info=os.fstat(stream.fileno())
        require(info.st_uid==0 and info.st_gid==0 and info.st_nlink==1 and info.st_mode&0o777==0o600 and info.st_size<=4096,
                'API_DRAIN_JOURNAL_UNSAFE')
        data=json.load(stream)
    expected={'schemaVersion','tag','containerId','image','pid','startedAt','namespaceInode','parentPid','parentStart','expiresAt','programSha256','phase','bootId'}
    require(isinstance(data,dict)and set(data)==expected and data['schemaVersion']==1 and
            re.fullmatch('teaching-api-drain-[0-9a-f]{32}',data['tag'])and re.fullmatch('[0-9a-f]{64}',data['containerId'])and
            re.fullmatch('sha256:[0-9a-f]{64}',data['image'])and isinstance(data['startedAt'],str)and
            all(type(data[name])is int and data[name]>1 for name in ('pid','namespaceInode','parentPid','parentStart'))and
            isinstance(data['expiresAt'],(int,float))and re.fullmatch('[0-9a-f]{64}',data['programSha256'])and
            data['phase']in ('active','stop_requested','stop_started','stop_complete','stop_uncertain','stop_cancelled',
                             'cleaning','rules_removed')and data['bootId']==boot_id(),
            'API_DRAIN_JOURNAL_INVALID')
    require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==data['programSha256'],'API_DRAIN_PROGRAM_CHANGED')
    return data


def create_journal(tag,api,inode):
    parent=JOURNAL.parent
    require(parent.resolve()==parent and parent.stat().st_uid==0 and not parent.stat().st_mode&0o022,'API_DRAIN_JOURNAL_DIRECTORY')
    data={'schemaVersion':1,'tag':tag,'containerId':api['Id'],'pid':api['State']['Pid'],
          'image':api['Image'],'startedAt':api['State']['StartedAt'],'namespaceInode':inode,
          'parentPid':os.getpid(),'parentStart':proc_start(os.getpid()),'expiresAt':time.monotonic()+300,
          'programSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'phase':'active','bootId':boot_id()}
    fd=os.open(JOURNAL,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w')as stream:json.dump(data,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        directory=os.open(parent,os.O_RDONLY);os.fsync(directory);os.close(directory)
    except BaseException:
        try:os.close(fd)
        except OSError:pass
        raise


def clear_journal(tag):
    require(read_journal()['tag']==tag,'API_DRAIN_JOURNAL_CHANGED')
    JOURNAL.unlink();directory=os.open(JOURNAL.parent,os.O_RDONLY);os.fsync(directory);os.close(directory)


def update_journal(tag,phase):
    require(phase in ('stop_requested','stop_started','stop_complete','stop_uncertain','stop_cancelled',
                      'cleaning','rules_removed'),'API_DRAIN_JOURNAL_PHASE')
    data=read_journal();require(data['tag']==tag,'API_DRAIN_JOURNAL_CHANGED');data['phase']=phase
    temp=JOURNAL.with_name(JOURNAL.name+'.tmp-'+secrets.token_hex(16))
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w')as stream:json.dump(data,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temp,JOURNAL);directory=os.open(JOURNAL.parent,os.O_RDONLY);os.fsync(directory);os.close(directory)
    finally:temp.unlink(missing_ok=True)


@contextlib.contextmanager
def guard_lock(tag):
    path=marker(tag,'lock')
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_gid==0 and info.st_nlink==1 and
                stat.S_IMODE(info.st_mode)==0o600,'API_DRAIN_LOCK_UNSAFE')
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:os.close(fd)


def advance_stop(tag):
    """Advance one stop request once. A restarted started phase is unknown."""
    with guard_lock(tag):
        data=read_journal();require(data['tag']==tag,'API_DRAIN_JOURNAL_CHANGED')
        if data['phase']=='stop_started':update_journal(tag,'stop_uncertain');return
        if data['phase']!='stop_requested':return
        if marker_exists(tag,'cancel')or marker_exists(tag,'release')or not process_alive(data['parentPid'],data['parentStart'])or time.monotonic()>=data['expiresAt']-60:
            update_journal(tag,'stop_cancelled');return
        update_journal(tag,'stop_started')
    outcome='stop_uncertain'
    try:
        run(DOCKER+['stop','--time','30',data['containerId']],timeout=45)
        stopped=json.loads(run(DOCKER+['inspect',data['containerId']]))
        require(len(stopped)==1,'API_DRAIN_STOP_UNCERTAIN');state=stopped[0]['State']
        require(stopped[0]['Id']==data['containerId']and stopped[0]['Image']==data['image']and
                state.get('StartedAt')==data['startedAt']and state.get('Running')is False and
                state.get('ExitCode')==0 and not state.get('OOMKilled')and not state.get('Restarting'),
                'API_DRAIN_STOP_UNCERTAIN')
        outcome='stop_complete'
    except Exception:pass
    with guard_lock(tag):update_journal(tag,outcome)


def watch(tag):
    pin=marker(tag,'pin');ready=marker(tag,'ready');release=marker(tag,'release');cancel=marker(tag,'cancel')
    if not JOURNAL.exists():
        require(not any(path.exists()or path.is_symlink()for path in (pin,ready,release,cancel)),'API_DRAIN_RECOVERY_REQUIRED')
        lock=marker(tag,'lock')
        if lock.exists():
            info=lock.lstat();require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_gid==0 and
                                     stat.S_IMODE(info.st_mode)==0o600 and not lock.is_symlink(),'API_DRAIN_LOCK_UNSAFE')
            lock.unlink()
        return
    data=read_journal();require(data['tag']==tag,'API_DRAIN_JOURNAL_CHANGED')
    require(RUN.resolve()==RUN and RUN.stat().st_uid==0 and RUN.stat().st_mode&0o777==0o700,'API_DRAIN_RUN_DIRECTORY')
    if data['phase']=='rules_removed':
        with guard_lock(tag):finish_cleanup(data)
        return
    if pin.exists()and pin.stat().st_ino!=data['namespaceInode']:
        # A failed first bind can leave only our exclusive empty placeholder.
        # No rule can be installed before readiness was published.
        info=pin.lstat();require(not ready.exists()and stat.S_ISREG(info.st_mode)and info.st_uid==0 and
                                info.st_gid==0 and info.st_mode&0o777==0o600 and info.st_size==0,'API_DRAIN_PIN_UNSAFE')
        pin.unlink()
    if not pin.exists():
        # The random name is exclusive to this durable journal. A restart
        # before readiness may safely create the missing pin; after readiness a
        # missing pin is not proof that all prior rules were removed.
        require(not ready.exists(),'API_DRAIN_PIN_MISSING')
        if not process_alive(data['parentPid'],data['parentStart']):
            with guard_lock(tag):
                update_journal(tag,'rules_removed');finish_cleanup(data)
            return
        source=os.open('/proc/'+str(data['pid'])+'/ns/net',os.O_RDONLY|os.O_CLOEXEC)
        try:
            require(os.fstat(source).st_ino==data['namespaceInode'],'API_DRAIN_NAMESPACE_CHANGED')
            fd=os.open(pin,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
            run([MOUNT,'--bind','/proc/self/fd/'+str(source),str(pin)],fd=source)
            require(pin.stat().st_ino==data['namespaceInode'],'API_DRAIN_NAMESPACE_CHANGED')
        finally:os.close(source)
    require(not pin.is_symlink()and pin.stat().st_ino==data['namespaceInode'],'API_DRAIN_PIN_UNSAFE')
    fd=os.open(pin,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        require(os.fstat(fd).st_ino==data['namespaceInode'],'API_DRAIN_NAMESPACE_CHANGED')
        # A stale ready file from a previous restart is replaced only after
        # validating its random tag and restricted ownership.
        if ready.exists():
            require(not ready.is_symlink()and ready.stat().st_uid==0 and ready.stat().st_mode&0o777==0o600,
                    'API_DRAIN_READY_UNSAFE');ready.unlink()
        out=os.open(ready,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(out,'w')as stream:json.dump({'tag':tag,'pid':os.getpid(),'start':proc_start(os.getpid())},stream);stream.flush();os.fsync(stream.fileno())
        # A process restarted with stop_started cannot know whether Docker
        # received the prior request. Never send it again or release the fence.
        while True:
            data=read_journal();phase=data['phase'];alive=process_alive(data['parentPid'],data['parentStart'])
            if phase in ('stop_requested','stop_started'):
                advance_stop(tag);continue
            if phase=='stop_uncertain':
                # Explicit operator recovery is required. The guard must stay.
                time.sleep(5);continue
            if phase=='stop_complete'and (not alive or time.monotonic()>=data['expiresAt']):
                # A dead caller can no longer establish which later Worker
                # operations or migration side effects were issued.
                with guard_lock(tag):update_journal(tag,'stop_uncertain')
                continue
            if phase in ('cleaning','rules_removed')or marker_exists(tag,'release')or not alive or time.monotonic()>=data['expiresAt']:
                break
            time.sleep(1)
        if not process_alive(data['parentPid'],data['parentStart'])and not marker_exists(tag,'release'):time.sleep(50)
        with guard_lock(tag):
            data=read_journal();require(data['tag']==tag,'API_DRAIN_JOURNAL_CHANGED')
            if data['phase']!='rules_removed':
                update_journal(tag,'cleaning')
                for binary in IPTABLES:remove_if_present(binary,fd,tag)
                update_journal(tag,'rules_removed')
            finish_cleanup(data)
    finally:os.close(fd)


def finish_cleanup(data):
    tag=data['tag'];pin=marker(tag,'pin')
    require(read_journal()['phase']=='rules_removed','API_DRAIN_JOURNAL_PHASE')
    if pin.exists():
        info=pin.lstat();require(not pin.is_symlink(),'API_DRAIN_PIN_UNSAFE')
        if info.st_ino==data['namespaceInode']:
            run([UMOUNT,str(pin)]);info=pin.lstat()
        require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_gid==0 and
                stat.S_IMODE(info.st_mode)==0o600 and info.st_size==0,'API_DRAIN_PIN_UNSAFE')
        pin.unlink()
    for suffix in ('ready','release','cancel'):
        path=marker(tag,suffix)
        if path.exists():
            info=path.lstat();require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_gid==0 and
                                     stat.S_IMODE(info.st_mode)==0o600 and not path.is_symlink(),'API_DRAIN_MARKER_UNSAFE')
            path.unlink()
    clear_journal(tag)
    marker(tag,'lock').unlink()


def unit_state(tag):
    output=run([SYSTEMCTL,'show',tag+'.service','--property=ActiveState,MainPID,ControlGroup','--no-pager']).decode()
    values=dict(line.split('=',1)for line in output.splitlines()if '=' in line)
    require(values.get('ActiveState')=='active'and values.get('MainPID','').isdigit()and int(values['MainPID'])>1 and
            values.get('ControlGroup')=='/system.slice/'+tag+'.service','API_DRAIN_WATCHDOG_FAILED')
    return int(values['MainPID'])


class Fence:
    """One-use fence around stopping the exact preflight-verified API."""
    def __init__(self,api,*,check_cancel=lambda:None,wait_seconds=900,fenced_wait_seconds=180):
        self.api=api;self.check_cancel=check_cancel;self.wait_seconds=wait_seconds;self.fenced_wait_seconds=fenced_wait_seconds
        self.fd=None;self.watcher=False;self.listener=None;self.journal_created=False;self.uncertain=False
        self.tag='teaching-api-drain-'+secrets.token_hex(16)

    def identity(self):
        import json
        api=self.api
        require(isinstance(api,dict)and re.fullmatch('[0-9a-f]{64}',api.get('Id','')),'API_DRAIN_CONTAINER_ID')
        state=api.get('State',{});pid=state.get('Pid')
        require(type(pid)is int and pid>1 and state.get('Running')is True and isinstance(state.get('StartedAt'),str),
                'API_DRAIN_CONTAINER_STATE')
        sample=json.loads(run(DOCKER+['inspect',api['Id']]))
        require(len(sample)==1 and sample[0]['Id']==api['Id'] and sample[0]['Image']==api['Image'] and
                sample[0]['State'].get('Running')is True and sample[0]['State'].get('Pid')==pid and
                sample[0]['State'].get('StartedAt')==state['StartedAt'],'API_DRAIN_IDENTITY_CHANGED')
        require(self.fd is not None and os.fstat(self.fd).st_ino==os.stat('/proc/'+str(pid)+'/ns/net').st_ino,
                'API_DRAIN_NAMESPACE_CHANGED')
        return pid

    def sockets(self):
        pid=self.identity();entries=[]
        for name in ('tcp','tcp6'):
            entries+=socket_entries(Path('/proc',str(pid),'net',name).read_text())
        listeners=[item for item in entries if item[1]=='0A']
        require(len(listeners)==1 and set(listeners[0][0])=={'0'},'API_DRAIN_LISTENER_REFUSED')
        listener=listeners[0][2]
        fds=Path('/proc',str(pid),'fd');owned=False
        for entry in fds.iterdir():
            try:owned=owned or os.readlink(entry)==f'socket:[{listener}]'
            except FileNotFoundError:pass
        require(owned,'API_DRAIN_LISTENER_OWNER_REFUSED')
        if self.listener is None:self.listener=listener
        require(self.listener==listener,'API_DRAIN_LISTENER_CHANGED')
        self.identity()
        return sum(state not in ('0A','06')for _,state,_ in entries)

    def idle(self,timeout,require_rules=False,minimum_seconds=0):
        start=time.monotonic();deadline=start+timeout;consecutive=0
        while True:
            self.check_cancel()
            if require_rules:self.verify()
            consecutive=consecutive+1 if self.sockets()==0 else 0
            if consecutive>=2 and time.monotonic()-start>=minimum_seconds:return
            require(time.monotonic()<deadline,'API_DRAIN_CONNECTION_TIMEOUT')
            time.sleep(1)

    def verify_watcher(self):
        self.identity()
        require(self.watcher,'API_DRAIN_WATCHDOG_FAILED')
        pid=unit_state(self.tag);ready=marker(self.tag,'ready')
        require(not ready.is_symlink()and ready.stat().st_uid==0 and ready.stat().st_mode&0o777==0o600,
                'API_DRAIN_WATCHDOG_FAILED')
        value=json.loads(ready.read_text())
        require(value.get('tag')==self.tag and value.get('pid')==pid and process_alive(pid,value.get('start')),
                'API_DRAIN_WATCHDOG_FAILED')
        require(time.monotonic()<self.expiry-60,'API_DRAIN_WATCHDOG_DEADLINE')
        data=read_journal();require(data['tag']==self.tag and data['phase']=='active','API_DRAIN_JOURNAL_PHASE')

    def verify(self):
        self.verify_watcher()
        require(run(['/usr/bin/uname','-r']).decode().strip()==VERIFIED_KERNEL,'API_DRAIN_KERNEL_UNVERIFIED')
        require_no_server_fastopen(self.fd)
        for binary in IPTABLES:require_first(binary,self.fd,self.tag)

    def __enter__(self):
        try:
            pid=self.api['State']['Pid'];self.fd=os.open('/proc/'+str(pid)+'/ns/net',os.O_RDONLY|os.O_CLOEXEC)
            self.identity()
            require(run(['/usr/bin/uname','-r']).decode().strip()==VERIFIED_KERNEL,'API_DRAIN_KERNEL_UNVERIFIED')
            require_no_server_fastopen(self.fd)
            # Wait without denying anyone; an always-open observer will cause a
            # safe timeout with no changes, never a forced disconnection.
            self.idle(self.wait_seconds)
            create_journal(self.tag,self.api,os.fstat(self.fd).st_ino);self.journal_created=True
            require(RUN.resolve()==RUN and RUN.stat().st_uid==0 and RUN.stat().st_mode&0o777==0o700,'API_DRAIN_RUN_DIRECTORY')
            lock=marker(self.tag,'lock');lock_fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(lock_fd)
            self.expiry=read_journal()['expiresAt']
            unit=self.tag+'.service'
            state=run([SYSTEMCTL,'show','--value','-p','LoadState',unit]).strip()
            require(state==b'not-found','API_DRAIN_WATCHDOG_UNIT_COLLISION')
            self.watcher=True
            run([SYSTEMD_RUN,'--quiet','--unit='+unit,'--slice=system.slice','--collect',
                 '--property=Restart=on-failure','--property=RestartSec=2s','--property=StartLimitIntervalSec=0',
                 '--property=RuntimeMaxSec=600s',PYTHON,str(Path(__file__).resolve()),'--watchdog',self.tag],timeout=15)
            deadline=time.monotonic()+15
            while True:
                try:self.verify_watcher();break
                except (DrainError,FileNotFoundError,json.JSONDecodeError):
                    require(time.monotonic()<deadline,'API_DRAIN_WATCHDOG_NOT_READY');self.check_cancel();time.sleep(.2)
            with guard_lock(self.tag):
                self.verify_watcher()
                for binary in IPTABLES:rule(binary,self.fd,'-I',self.tag)
            # Linux 6.8.0-124's checked headers bound cookie validity to 120s.
            # A pre-fence SYN cookie may not appear in /proc/net/tcp; its ACK
            # can become a connection later unless both gates age it out.
            self.idle(self.fenced_wait_seconds,require_rules=True,minimum_seconds=COOKIE_GRACE_SECONDS)
            self.verify()
            return self
        except BaseException:
            self.close();raise

    def close(self):
        error=None
        if self.uncertain:
            if self.fd is not None:os.close(self.fd);self.fd=None
            return
        if self.journal_created and self.watcher and JOURNAL.exists():
            try:
                release=marker(self.tag,'release')
                out=os.open(release,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(out,'w')as stream:json.dump({'tag':self.tag},stream);stream.flush();os.fsync(stream.fileno())
                deadline=time.monotonic()+90
                while JOURNAL.exists()and time.monotonic()<deadline:time.sleep(1)
                require(not JOURNAL.exists(),'API_DRAIN_RECOVERY_REQUIRED')
            except Exception as caught:error=caught
        elif self.journal_created and JOURNAL.exists():
            try:
                require(not any(marker(self.tag,suffix).exists()for suffix in ('pin','ready','release')),'API_DRAIN_RECOVERY_REQUIRED')
                clear_journal(self.tag);marker(self.tag,'lock').unlink(missing_ok=True)
            except Exception as caught:error=caught
        if self.journal_created and JOURNAL.exists():error=DrainError('API_DRAIN_RECOVERY_REQUIRED')
        if self.fd is not None:os.close(self.fd);self.fd=None
        if error:raise DrainError('API_DRAIN_CLEANUP_FAILED')from error

    def __exit__(self,kind,value,trace):self.close()

    @contextlib.contextmanager
    def stopping(self):
        with guard_lock(self.tag):
            self.verify()
            yield

    def stop(self,on_stopped=lambda:None):
        # The independent watcher is the sole executor. Once the journal says
        # stop_started, neither this caller nor a restarted watcher reissues it.
        try:
            with guard_lock(self.tag):
                self.verify();self.check_cancel();self.uncertain=True;update_journal(self.tag,'stop_requested')
            cancelled=None;deadline=time.monotonic()+90
            while True:
                if cancelled is None:
                    try:self.check_cancel()
                    except Exception as error:
                        cancelled=error
                        if not marker_exists(self.tag,'cancel'):write_marker(self.tag,'cancel')
                data=read_journal();require(data['tag']==self.tag,'API_DRAIN_JOURNAL_CHANGED');phase=data['phase']
                if phase=='stop_complete':
                    on_stopped();self.uncertain=False
                    if cancelled is not None:raise cancelled
                    return
                if phase=='stop_cancelled':
                    self.uncertain=False
                    if cancelled is not None:raise cancelled
                    raise DrainError('API_DRAIN_CANCELLED')
                if phase=='stop_uncertain'or time.monotonic()>=deadline:raise DrainError('API_DRAIN_STOP_UNCERTAIN')
                time.sleep(.5)
        except Exception as error:
            if self.uncertain:
                try:
                    if not marker_exists(self.tag,'cancel'):write_marker(self.tag,'cancel')
                except Exception:pass
                raise DrainError('API_DRAIN_STOP_UNCERTAIN')from error
            raise


if __name__=='__main__':
    require(len(sys.argv)==3 and sys.argv[1]=='--watchdog'and
            re.fullmatch('teaching-api-drain-[0-9a-f]{32}',sys.argv[2]),'API_DRAIN_WATCHDOG_ARGUMENTS')
    watch(sys.argv[2])
