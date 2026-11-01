#!/usr/bin/python3
"""Review candidate: bounded real Docker stop tests on labeled synthetic containers.

Runs only an explicitly supplied Node program, with no network, application
environment, mounts, privileges or application entrypoint. Never calls stop,
inspect, logs or rm for anything except the exact scoped synthetic ID.
"""
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

BASE=Path('/run/teaching-drain-daemon-synthetic')
DOCKER='/usr/bin/docker';SYSTEMCTL='/usr/bin/systemctl';SYSTEMD_RUN='/usr/bin/systemd-run';PYTHON='/usr/bin/python3'
IMAGE='sha256:78d0480ac131e31d0149d4dd11915fd9aab46afe4aa3bad4819a13b6f8008150'
SAFE_ENV={'PATH':'/usr/local/bin:/usr/bin:/bin','NODE_VERSION':'synthetic','YARN_VERSION':'synthetic',
          'PLAYWRIGHT_BROWSERS_PATH':'/nonexistent','NODE_ENV':'test','HOST':'0.0.0.0','PORT':'4999',
          'PPT_MASTER_PYTHON_BIN':'/nonexistent'}
NODE_PROGRAM="const h=require('http');const s=h.createServer((q,r)=>r.end('synthetic'));s.listen(4999,'0.0.0.0',()=>console.log('SYNTH_READY'));process.on('SIGTERM',()=>{console.log('SYNTH_SIGNAL');setTimeout(()=>s.close(()=>process.exit(0)),5000)});"
HERE=Path(__file__).resolve().parent
PHASE='bootstrap'


def trusted_bootstrap():
    if os.geteuid()!=0 or not HERE.is_relative_to(Path('/run')) or HERE.stat().st_uid!=0 or HERE.stat().st_gid!=0 or \
       stat.S_IMODE(HERE.stat().st_mode)!=0o700 or Path(__file__).resolve()!=Path(__file__):
        raise RuntimeError('DOCKER_FIXTURE_STAGING_UNSAFE')
    program=Path(__file__);manifest=HERE/'manifest.json'
    for path in (program,manifest):
        info=path.lstat()
        if path.is_symlink()or not stat.S_ISREG(info.st_mode)or info.st_uid!=0 or info.st_gid!=0 or \
           info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600:raise RuntimeError('DOCKER_FIXTURE_STAGING_UNSAFE')
    expected=json.loads(manifest.read_text())
    if expected!={program.name:hashlib.sha256(program.read_bytes()).hexdigest()}:
        raise RuntimeError('DOCKER_FIXTURE_MANIFEST_MISMATCH')
trusted_bootstrap()


def check(value,code):
    if not value:raise RuntimeError(code)


def command(args,timeout=20,allow_failure=False):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          timeout=timeout,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
    if not allow_failure:check(result.returncode==0,'DOCKER_FIXTURE_COMMAND_FAILED')
    return result


def context(identifier):
    check(re.fullmatch('[0-9a-f]{32}',identifier)and os.geteuid()==0,'DOCKER_FIXTURE_SCOPE_REFUSED')
    root=BASE/identifier
    check(BASE.resolve()==BASE and not BASE.is_symlink()and BASE.stat().st_uid==0 and
          stat.S_IMODE(BASE.stat().st_mode)==0o700 and root.resolve()==root and not root.is_symlink()and
          root.is_dir()and root.stat().st_uid==0 and stat.S_IMODE(root.stat().st_mode)==0o700,'DOCKER_FIXTURE_SCOPE_REFUSED')
    return root


def unit(identifier):
    check(re.fullmatch('[0-9a-f]{32}',identifier),'DOCKER_FIXTURE_SCOPE_REFUSED')
    return 'teaching-drain-daemon-synthetic-'+identifier+'.service'


def inspect_synthetic(identifier,case,container_id):
    check(case in ('normal','lost')and re.fullmatch('[0-9a-f]{64}',container_id),'DOCKER_FIXTURE_ID_REFUSED')
    rows=json.loads(command([DOCKER,'inspect',container_id]).stdout)
    check(len(rows)==1,'DOCKER_FIXTURE_ID_REFUSED');item=rows[0];cfg=item['Config'];host=item['HostConfig']
    check(item['Id']==container_id and item['Image']==IMAGE and cfg.get('Labels',{}).get('teaching.drain.synthetic')==identifier and
          cfg.get('Labels',{}).get('teaching.drain.case')==case and cfg.get('User')=='65534:65534'and
          cfg.get('Entrypoint')==['/usr/local/bin/node']and cfg.get('Cmd')==['-e',NODE_PROGRAM]and
          cfg.get('Healthcheck')in (None,{'Test':['NONE']})and cfg.get('WorkingDir')=='/'and
          host.get('NetworkMode')=='none'and host.get('ReadonlyRootfs')is True and not host.get('Privileged')and
          host.get('RestartPolicy',{}).get('Name')=='no'and not item.get('Mounts')and
          not host.get('PortBindings')and not host.get('CapAdd')and 'ALL'in(host.get('CapDrop')or[])and
          any(value in ('no-new-privileges','no-new-privileges=true')for value in(host.get('SecurityOpt')or[])),
          'DOCKER_FIXTURE_CONTAINER_UNSAFE')
    env=cfg.get('Env')or[]
    check(all(isinstance(item,str)and '='in item for item in env)and
          dict(item.split('=',1)for item in env)==SAFE_ENV and len(env)==len(SAFE_ENV),
          'DOCKER_FIXTURE_IMAGE_ENV_REFUSED')
    return item


def run_test(root,identifier):
    global PHASE
    PHASE='image-inspect'
    image=json.loads(command([DOCKER,'image','inspect',IMAGE]).stdout)
    check(len(image)==1 and image[0]['Id']==IMAGE and not(image[0].get('Config',{}).get('Volumes')),
          'DOCKER_FIXTURE_IMAGE_REFUSED')
    results=[]
    for case in ('normal','lost'):
        PHASE=case+'-create'
        create=command([DOCKER,'create','--name','teaching-drain-synthetic-'+identifier+'-'+case,
                        '--label','teaching.drain.synthetic='+identifier,'--label','teaching.drain.case='+case,
                        '--network','none','--read-only','--user','65534:65534','--workdir','/',
                        '--cap-drop','ALL','--security-opt','no-new-privileges','--restart','no',
                        '--memory','128m','--pids-limit','32',
                        *[part for key,value in SAFE_ENV.items()for part in ('--env',key+'='+value)],
                        '--no-healthcheck','--entrypoint','/usr/local/bin/node',IMAGE,'-e',NODE_PROGRAM],timeout=30)
        PHASE=case+'-scope-check';container_id=create.stdout.decode().strip();inspect_synthetic(identifier,case,container_id)
        PHASE=case+'-start'
        command([DOCKER,'start',container_id],timeout=30)
        deadline=time.monotonic()+20
        while b'SYNTH_READY'not in command([DOCKER,'logs',container_id],allow_failure=True).stdout:
            check(time.monotonic()<deadline,'DOCKER_FIXTURE_START_TIMEOUT');time.sleep(.2)
        check(inspect_synthetic(identifier,case,container_id)['State']['Running']is True,'DOCKER_FIXTURE_START_FAILED')
        attempts=root/(case+'.stop-count');fd=os.open(attempts,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w')as stream:stream.write('1\n');stream.flush();os.fsync(stream.fileno())
        PHASE=case+'-stop-once'
        process=subprocess.Popen([DOCKER,'stop','--time','20',container_id],stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,
                                 env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
        deadline=time.monotonic()+15
        while b'SYNTH_SIGNAL'not in command([DOCKER,'logs',container_id],allow_failure=True).stdout:
            check(process.poll()is None and time.monotonic()<deadline,'DOCKER_FIXTURE_SIGNAL_TIMEOUT');time.sleep(.1)
        if case=='lost':
            check(process.poll()is None,'DOCKER_FIXTURE_RESPONSE_NOT_LOST');process.kill();process.wait(timeout=5)
        else:
            process.communicate(timeout=30);check(process.returncode==0,'DOCKER_FIXTURE_STOP_FAILED')
        PHASE=case+'-confirm'
        deadline=time.monotonic()+30
        while True:
            item=inspect_synthetic(identifier,case,container_id);state=item['State']
            if state.get('Running')is False:break
            check(time.monotonic()<deadline,'DOCKER_FIXTURE_DAEMON_TIMEOUT');time.sleep(.2)
        check(state.get('ExitCode')==0 and not state.get('OOMKilled')and not state.get('Restarting')and
              attempts.read_text().splitlines()==['1'],'DOCKER_FIXTURE_STOP_UNCONFIRMED')
        PHASE=case+'-remove';inspect_synthetic(identifier,case,container_id);command([DOCKER,'rm',container_id],timeout=20)
        results.append(case)
    print(json.dumps({'status':'passed','cases':results,'realDockerDaemon':True,'noNetwork':True,
                      'responseLostAfterSyntheticSigterm':True,'stopDispatchesPerContainer':1,
                      'actualHelperTested':False,'businessTasksRun':0,'modelCalls':0}),flush=True)


def cleanup(root,identifier):
    global PHASE
    PHASE='cleanup'
    for case in ('normal','lost'):
        result=command([DOCKER,'ps','-aq','--filter','label=teaching.drain.synthetic='+identifier,
                        '--filter','label=teaching.drain.case='+case,'--no-trunc'],allow_failure=True)
        check(result.returncode==0,'DOCKER_FIXTURE_CLEANUP_QUERY_FAILED')
        ids=result.stdout.decode().split();check(len(ids)<=1,'DOCKER_FIXTURE_CLEANUP_SCOPE')
        for container_id in ids:
            inspect_synthetic(identifier,case,container_id);command([DOCKER,'rm','--force',container_id],timeout=30)
    check(root.parent==BASE and root.name==identifier and not root.is_symlink(),'DOCKER_FIXTURE_CLEANUP_SCOPE')
    shutil.rmtree(root)


def launch():
    check(os.stat('/proc/self/ns/net').st_ino==os.stat('/proc/1/ns/net').st_ino,'DOCKER_FIXTURE_HOST_NAMESPACE_REQUIRED')
    BASE.mkdir(mode=0o700,exist_ok=True)
    check(BASE.resolve()==BASE and not BASE.is_symlink()and BASE.stat().st_uid==0 and
          stat.S_IMODE(BASE.stat().st_mode)==0o700,'DOCKER_FIXTURE_SCOPE_REFUSED')
    identifier=secrets.token_hex(16);target_unit=unit(identifier)
    state=command([SYSTEMCTL,'show','--value','-p','LoadState',target_unit],allow_failure=True)
    check(state.returncode==0 and state.stdout.strip()==b'not-found','DOCKER_FIXTURE_UNIT_EXISTS')
    root=BASE/identifier;root.mkdir(mode=0o700);program=root/'experiment.py'
    fd=os.open(program,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb')as stream:stream.write(Path(__file__).read_bytes());stream.flush();os.fsync(stream.fileno())
    manifest=root/'manifest.json';fd=os.open(manifest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as stream:json.dump({'experiment.py':hashlib.sha256(program.read_bytes()).hexdigest()},stream);stream.flush();os.fsync(stream.fileno())
    try:
        command([SYSTEMD_RUN,'--quiet','--unit='+target_unit,'--slice=system.slice','--property=RuntimeMaxSec=180s',
                 '--property=ExecStopPost='+PYTHON+' '+str(program)+' cleanup '+identifier,PYTHON,str(program),'run',identifier])
    except BaseException:
        state=command([SYSTEMCTL,'show','--value','-p','LoadState',target_unit],allow_failure=True)
        if state.returncode==0 and state.stdout.strip()==b'not-found':shutil.rmtree(root)
        raise
    print(json.dumps({'unit':target_unit,'id':identifier}))


def main():
    if len(sys.argv)==2 and sys.argv[1]=='launch':launch();return
    check(len(sys.argv)==3 and sys.argv[1]in ('run','cleanup'),'DOCKER_FIXTURE_ARGUMENTS')
    identifier=sys.argv[2];root=context(identifier)
    if sys.argv[1]=='run':run_test(root,identifier)
    else:cleanup(root,identifier)


if __name__=='__main__':
    try:main()
    except Exception as error:
        code=str(error)if re.fullmatch('DOCKER_FIXTURE_[A-Z_]+',str(error))else type(error).__name__
        print(json.dumps({'status':'failed','phase':PHASE,'code':code,'syntheticOnly':True}),flush=True);sys.exit(1)
