#!/usr/bin/python3
"""Root operator's bounded, recoverable initial Codex helper installation.

Run only from a reviewed root-owned host-install-bundles/<manifest hash>/install.py.
No Docker mutations, database writes, model calls or remote installation action.
"""
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import time
import uuid

ROOT=Path('/opt/teaching-ai-production')
SCOPE='teaching-production-codex-host-v1'
STARTUP='operations/startup/production-ops.py'
RECEIVER='operations/release/receiver.py'
FILES={RECEIVER:'receiver.py','operations/release/cnb_release.py':'cnb_release.py',
       'operations/release/tencent_registry.py':'tencent_registry.py',
       'operations/release/codex_runtime.py':'codex_runtime.py',
       'operations/release/codex_configure.py':'codex_configure.py',STARTUP:'production-ops.py'}
DEPENDENCIES={'operations/release/queue-probe.mjs','operations/startup/compose-production'}
SETTINGS='environments/codex-runtime.json'
PENDING='operations/release/state/codex-host-install.pending.json'
RECEIPT='operations/release/state/codex-host-install.last.json'
SERVICES={'api','worker-general','worker-ppt','web','gateway','postgres','redis','storage'}
GATE=b'#!/usr/bin/python3\nimport json,sys\nprint(json.dumps({"status":"failed","code":"CODEX_HOST_INSTALLATION_RECOVERY_REQUIRED","modelCalls":0,"externalNotifications":0}))\nsys.exit(1)\n'


def require(ok,code):
    if not ok:raise ValueError(code)
def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
def error_code(error):
    value=str(error)
    return value if re.fullmatch('[A-Z][A-Z0-9_]{1,95}',value)else'CODEX_HOST_INSTALLATION_FAILED'
def decode(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:require(key not in result,'HOST_INSTALL_DUPLICATE_KEY');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)
def packed(raw):return None if raw is None else base64.b64encode(raw).decode()
def unpacked(value):
    if value is None:return None
    require(isinstance(value,str)and len(value)<=700000,'HOST_INSTALL_JOURNAL_BYTES')
    try:return base64.b64decode(value,validate=True)
    except Exception:raise ValueError('HOST_INSTALL_JOURNAL_BYTES')from None
def sync_directory(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
def load(path):
    spec=importlib.util.spec_from_file_location('host_install_'+uuid.uuid4().hex,path)
    # A check must neither create bytecode nor reuse stale same-second caches.
    module=importlib.util.module_from_spec(spec);exec(compile(path.read_bytes(),str(path),'exec'),module.__dict__);return module


class Installer:
    # Alternate paths/owners exist only for explicit in-process local tests.
    # The root CLI below never accepts them from argv, stdin or environment.
    def __init__(self,bundle,root=ROOT,uid=0,gid=0,lock_dir=Path('/run/lock')):
        self.root=root;self.bundle=bundle;self.uid=uid;self.gid=gid;self.lock_dir=lock_dir
        self.fault=None

    def private(self,path,limit=524288,missing=False):
        require(path.resolve()==path,'HOST_INSTALL_SYMLINK_REFUSED')
        parent=path.parent
        while parent!=self.root.parent:
            info=parent.lstat()
            require(stat.S_ISDIR(info.st_mode)and info.st_uid==self.uid and not stat.S_IMODE(info.st_mode)&0o022,'HOST_INSTALL_DIRECTORY_UNSAFE')
            if parent==self.root:break
            require(parent.is_relative_to(self.root),'HOST_INSTALL_PATH_SCOPE');parent=parent.parent
        if not path.exists():
            require(missing and not path.is_symlink(),'HOST_INSTALL_FILE_MISSING');return None
        info=path.lstat()
        require(stat.S_ISREG(info.st_mode)and info.st_uid==self.uid and info.st_gid==self.gid and info.st_nlink==1 and
                stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=limit,'HOST_INSTALL_FILE_UNSAFE')
        return path.read_bytes()

    def read(self,name,missing=False,limit=524288):
        require(name in set(FILES)|DEPENDENCIES|{SETTINGS,PENDING,RECEIPT,'operations/release/state/codex-configuration.pending.json'},'HOST_INSTALL_PATH_SCOPE')
        return self.private(self.root/name,limit,missing)

    def write(self,name,raw):
        require(name in set(FILES)|{SETTINGS,PENDING,RECEIPT},'HOST_INSTALL_WRITE_SCOPE')
        path=self.root/name;self.private(path,2097152,missing=True)
        if raw is None:
            path.unlink(missing_ok=True);sync_directory(path.parent)
        else:
            temporary=path.with_name(path.name+'.install-'+uuid.uuid4().hex)
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:
                with os.fdopen(fd,'wb')as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,path);sync_directory(path.parent)
            finally:temporary.unlink(missing_ok=True)
        if self.fault:self.fault(name,raw)

    def manifest(self):
        require(self.bundle.parent==self.root/'operations/release/host-install-bundles'and
                re.fullmatch('[0-9a-f]{64}',self.bundle.name),'HOST_INSTALL_BUNDLE_SCOPE')
        raw=self.private(self.bundle/'manifest.json',20000)
        require(digest(raw)==self.bundle.name,'HOST_INSTALL_MANIFEST_HASH')
        m=decode(raw)
        require(isinstance(m,dict)and set(m)=={'schemaVersion','scope','root','files','dependencies','settingsBeforeSha256',
                'installerSha256','sourceAuditSha256','startupPatchSha256'}and type(m['schemaVersion'])is int and
                m['schemaVersion']==1 and m['scope']==SCOPE and m['root']==str(ROOT),'HOST_INSTALL_MANIFEST')
        require(set(m['files'])==set(FILES)and set(m['dependencies'])==DEPENDENCIES,'HOST_INSTALL_MANIFEST_PATHS')
        for field in ('installerSha256','sourceAuditSha256','startupPatchSha256'):
            require(isinstance(m[field],str)and re.fullmatch('[0-9a-f]{64}',m[field]),'HOST_INSTALL_MANIFEST_HASH')
        require(m['settingsBeforeSha256']is None or re.fullmatch('[0-9a-f]{64}',m['settingsBeforeSha256']),'HOST_INSTALL_MANIFEST_HASH')
        program=self.private(self.bundle/'install.py')
        require(digest(program)==m['installerSha256']and program==Path(__file__).read_bytes(),'HOST_INSTALL_PROGRAM_CHANGED')
        for name,filename in FILES.items():
            row=m['files'][name]
            require(set(row)=={'beforeSha256','afterSha256','payload'}and row['payload']=='files/'+filename and
                    (row['beforeSha256']is None or re.fullmatch('[0-9a-f]{64}',row['beforeSha256']))and
                    re.fullmatch('[0-9a-f]{64}',row['afterSha256']),'HOST_INSTALL_FILE_CONTRACT')
            data=self.private(self.bundle/row['payload'])
            require(digest(data)==row['afterSha256'],'HOST_INSTALL_PAYLOAD_CHANGED')
            compile(data,name,'exec')
        require(all(isinstance(x,str)and re.fullmatch('[0-9a-f]{64}',x)for x in m['dependencies'].values()),'HOST_INSTALL_DEPENDENCY_CONTRACT')
        return m

    def expected(self,m):
        require(self.read(PENDING,True,2097152)is None,'HOST_INSTALL_RECOVERY_REQUIRED')
        require(self.read('operations/release/state/codex-configuration.pending.json',True)is None,'HOST_CONFIGURATION_RECOVERY_REQUIRED')
        for name,row in m['files'].items():
            data=self.read(name,True)
            require((digest(data)if data is not None else None)==row['beforeSha256'],'HOST_INSTALL_STALE_SOURCE')
        raw=self.read(SETTINGS,True,16384)
        require((digest(raw)if raw is not None else None)==m['settingsBeforeSha256'],'HOST_INSTALL_STALE_SETTINGS')
        self.dependencies(m)

    def dependencies(self,m):
        for name,value in m['dependencies'].items():require(digest(self.read(name))==value,'HOST_INSTALL_DEPENDENCY_CHANGED')

    def runtime(self):
        self.read(STARTUP);ops=load(self.root/STARTUP);ops.preflight();sample=ops.snapshot();containers=ops.inspect()
        failures=set(sample.get('failures',[]))
        require((sample.get('healthy')is True or failures=={'CODEX_HOST_INSTALLATION_PENDING'})and
                failures<= {'CODEX_HOST_INSTALLATION_PENDING'}and set(containers)==SERVICES,'HOST_INSTALL_RUNTIME_UNHEALTHY')
        return dict(configSha256=digest(canonical(ops.config())),containers={name:dict(id=c['Id'],image=c['Image'],startedAt=c['State']['StartedAt'])
                                                                       for name,c in containers.items()})

    @contextlib.contextmanager
    def locks(self):
        handles=[]
        try:
            for filename in ('teaching-production-release.lock','teaching-production-maintenance.lock'):
                path=self.lock_dir/filename
                fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);handles.append(fd);info=os.fstat(fd)
                require(stat.S_ISREG(info.st_mode)and info.st_uid==self.uid and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,'HOST_INSTALL_LOCK_UNSAFE')
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('HOST_INSTALL_CONTROL_BUSY')from None
            yield
        finally:
            for fd in reversed(handles):os.close(fd)

    def control_command(self,args):
        if any(str(self.root/name)in args for name in (STARTUP,RECEIVER,'operations/release/codex_configure.py')):return True
        if not any(Path(x).name in('docker','docker-compose')for x in args[:2]):return False
        return any(args[i]in('--project-name','-p')and args[i+1]=='teaching-production'or
                   args[i]=='--project-directory'and args[i+1]==str(self.root)for i in range(len(args)-1))

    def fence(self):
        # Entry stubs have been atomically installed. Drain older Python/Compose
        # invocations that may already have loaded the previous control code.
        deadline=time.monotonic()+150
        while True:
            active=False
            for path in Path('/proc').glob('[0-9]*/cmdline'):
                if int(path.parent.name)==os.getpid():continue
                try:args=[x.decode(errors='replace')for x in path.read_bytes().split(b'\0')if x]
                except (FileNotFoundError,ProcessLookupError):continue
                if self.control_command(args):active=True;break
            if not active:return
            require(time.monotonic()<deadline,'HOST_INSTALL_OLD_CONTROL_BUSY');time.sleep(1)

    def setting_bytes(self,urls):
        require(isinstance(urls,list),'HOST_INSTALL_MODEL_ENDPOINTS')
        policy=load(self.bundle/'files/codex_runtime.py')
        raw=self.read(SETTINGS,True,16384)
        if raw is not None:
            value=policy.settings(decode(raw))
            require(value['rolloutMode']=='closed'and value['userIds']==[]and value['modelBaseUrls']==urls,'HOST_INSTALL_EXISTING_SETTINGS_REFUSED')
            return raw
        value=policy.settings(dict(schemaVersion=1,runnerToken=secrets.token_hex(32),egressToken=secrets.token_hex(32),
                                  modelBaseUrls=urls,rolloutMode='closed',userIds=[]))
        return canonical(value)

    def journal(self,m):
        r=decode(self.read(PENDING,limit=2097152))
        require(set(r)=={'schemaVersion','bundle','operationId','phase','files','runtime'}and r['schemaVersion']==1 and
                r['bundle']==self.bundle.name and re.fullmatch('[0-9a-f]{32}',r['operationId'])and
                r['phase']in('prepared','committed','rolling_back','rolled_back')and set(r['files'])==set(FILES)|{SETTINGS},'HOST_INSTALL_JOURNAL')
        for name,item in r['files'].items():
            require(set(item)=={'before','after'},'HOST_INSTALL_JOURNAL')
            before,after=unpacked(item['before']),unpacked(item['after'])
            expected=m['settingsBeforeSha256']if name==SETTINGS else m['files'][name]['beforeSha256']
            require((digest(before)if before is not None else None)==expected and after is not None,'HOST_INSTALL_JOURNAL_BYTES')
            if name!=SETTINGS:require(digest(after)==m['files'][name]['afterSha256'],'HOST_INSTALL_JOURNAL_BYTES')
            else:
                value=load(self.bundle/'files/codex_runtime.py').settings(decode(after))
                require(value['rolloutMode']=='closed'and value['userIds']==[],'HOST_INSTALL_JOURNAL_SETTINGS')
        require(set(r['runtime'])=={'configSha256','containers'}and re.fullmatch('[0-9a-f]{64}',r['runtime']['configSha256'])and
                set(r['runtime']['containers'])==SERVICES,'HOST_INSTALL_JOURNAL_RUNTIME')
        for item in r['runtime']['containers'].values():
            require(set(item)=={'id','image','startedAt'}and re.fullmatch('[0-9a-f]{64}',item['id'])and
                    re.fullmatch('sha256:[0-9a-f]{64}',item['image'])and isinstance(item['startedAt'],str),'HOST_INSTALL_JOURNAL_RUNTIME')
        return r

    def known_files(self,r):
        for name,item in r['files'].items():
            allowed=[unpacked(item['before']),unpacked(item['after'])]
            if name in(STARTUP,RECEIVER):allowed.append(GATE)
            require(self.read(name,True)in allowed,'HOST_INSTALL_EXTERNAL_CHANGE')

    def verify(self,r,side,recovery=False):
        for name,item in r['files'].items():require(self.read(name,True)==unpacked(item[side]),'HOST_INSTALL_FILE_NOT_RESTORED'if side=='before'else'HOST_INSTALL_FILE_NOT_INSTALLED')
        actual=self.runtime();previous=r['runtime']
        require(actual['configSha256']==previous['configSha256'],'HOST_INSTALL_APPLICATION_CONFIG_CHANGED')
        restarted=[]
        for name,before in previous['containers'].items():
            after=actual['containers'][name]
            require(after['id']==before['id']and after['image']==before['image'],'HOST_INSTALL_APPLICATION_IDENTITY_CHANGED')
            if after['startedAt']!=before['startedAt']:restarted.append(name)
        require(recovery or not restarted,'HOST_INSTALL_APPLICATION_RESTARTED')
        return restarted

    def finish(self,r,status,restarted=None):
        result=dict(status=status,bundle=r['bundle'],operationId=r['operationId'],modelCalls=0,servicesRestartedByInstaller=0,
                    observedExternalRestarts=restarted or [],settingsSha256=digest(self.read(SETTINGS))if self.read(SETTINGS,True)is not None else None)
        self.write(RECEIPT,canonical(result));self.write(PENDING,None);return result

    def restore(self,m,r,recovery=False):
        self.dependencies(m);self.known_files(r)
        r['phase']='rolling_back';self.write(PENDING,canonical(r))
        for name in(RECEIVER,STARTUP):self.write(name,GATE)
        self.fence()
        for name in [*FILES,SETTINGS]:
            if name not in(STARTUP,RECEIVER):self.write(name,unpacked(r['files'][name]['before']))
        r['phase']='rolled_back';self.write(PENDING,canonical(r))
        for name in(STARTUP,RECEIVER):self.write(name,unpacked(r['files'][name]['before']))
        restarted=self.verify(r,'before',recovery)
        return self.finish(r,'restored',restarted)

    def check(self):
        m=self.manifest();self.expected(m);self.runtime()
        return dict(status='verified',bundle=self.bundle.name,files=len(FILES),settingsPresent=m['settingsBeforeSha256']is not None,modelCalls=0)

    def apply(self,request):
        require(isinstance(request,dict)and set(request)=={'modelBaseUrls'},'HOST_INSTALL_REQUEST')
        m=self.manifest()
        with self.locks():
            self.expected(m);baseline=self.runtime();settings=self.setting_bytes(request['modelBaseUrls'])
            r=dict(schemaVersion=1,bundle=self.bundle.name,operationId=uuid.uuid4().hex,phase='prepared',runtime=baseline,
                   files={name:dict(before=packed(self.read(name,True)),after=packed(self.private(self.bundle/m['files'][name]['payload'])))for name in FILES})
            r['files'][SETTINGS]=dict(before=packed(self.read(SETTINGS,True)),after=packed(settings))
            self.write(PENDING,canonical(r))
            try:
                for name in(RECEIVER,STARTUP):self.write(name,GATE)
                self.fence()
                for name in FILES:
                    if name not in(STARTUP,RECEIVER):self.write(name,unpacked(r['files'][name]['after']))
                self.write(SETTINGS,settings)
                for name in(STARTUP,RECEIVER):self.write(name,unpacked(r['files'][name]['after']))
                self.dependencies(m);self.verify(r,'after')
                r['phase']='committed';self.write(PENDING,canonical(r))
                return self.finish(r,'installed')
            except Exception as error:
                if r['phase']=='committed':return dict(status='recovery_required',code='HOST_INSTALL_COMMIT_CLEANUP_REQUIRED',causeCode=error_code(error))
                try:self.restore(m,r)
                except Exception as restore_error:return dict(status='recovery_required',code='HOST_INSTALL_RESTORE_INCOMPLETE',causeCode=error_code(error),restoreCode=error_code(restore_error))
                return dict(status='failed',code='HOST_INSTALL_FAILED_RESTORED',causeCode=error_code(error))

    def recover(self):
        m=self.manifest()
        with self.locks():
            if self.read(PENDING,True,2097152)is None:return dict(status='not_pending',bundle=self.bundle.name,modelCalls=0)
            r=self.journal(m);self.dependencies(m);self.known_files(r)
            if r['phase']=='committed':return self.finish(r,'installed',self.verify(r,'after',True))
            return self.restore(m,r,True)


def main():
    require(os.geteuid()==0 and len(sys.argv)==2 and sys.argv[1]in('check','apply','recover'),'HOST_INSTALL_OPERATOR_SCOPE')
    require(Path(__file__).resolve()==Path(__file__)and Path(__file__).name=='install.py','HOST_INSTALL_OPERATOR_SCOPE')
    host=Installer(Path(__file__).parent);signal.alarm(600)
    if sys.argv[1]=='check':result=host.check()
    elif sys.argv[1]=='recover':result=host.recover()
    else:
        raw=sys.stdin.buffer.read(8193);require(0<len(raw)<=8192,'HOST_INSTALL_REQUEST_SIZE');result=host.apply(decode(raw))
    print(json.dumps(result));return 0 if result['status']in('verified','installed','restored','not_pending')else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:print(json.dumps(dict(status='failed',code=error_code(error))));raise SystemExit(1)
