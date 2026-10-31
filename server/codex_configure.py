#!/usr/bin/python3
"""Root-only, journalled admission/token changes. No release or model calls.

Install alongside receiver.py after review. `apply` reads a bounded JSON request
from stdin; secrets never belong in argv. `recover` resolves an interrupted
transaction before ordinary release/startup operations may proceed.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time
import uuid

SETTINGS = 'environments/codex-runtime.json'
PROJECT = 'teaching-production'
PENDING = 'codex-configuration.pending.json'
RECEIPT = 'codex-configuration.last.json'
CHANGED = {'api', 'codex-agent-runtime'}
SERVICES = {'api','worker-general','worker-ppt','web','gateway','postgres','redis','storage','codex-agent-runtime'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'))+'\n').encode()


def read(c, path, limit):
    c.private(path)
    c.require(path.stat().st_size <= limit, 'CODEX_CONFIGURATION_SIZE')
    return path.read_bytes()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def write(c, path, value):
    c.atomic(path, value)
    sync_directory(path.parent)


def remove(c, path):
    if path.exists():
        c.private(path); path.unlink(); sync_directory(path.parent)


def error_code(error):
    text = str(error)
    return text if re.fullmatch('[A-Z][A-Z0-9_]{1,79}', text) else 'CODEX_CONFIGURATION_FAILED'


def status(c):
    config = read(c, c.ROOT/SETTINGS, 16384)
    override = read(c, c.OVERRIDE, 131072)
    policy = c.ops.codex_module(); value = policy.settings(c.decode(config))
    pending = c.STATE/PENDING
    phase = journal(c)['phase'] if pending.exists() else None
    return dict(status='recovery_required' if phase else 'configured', settingsSha256=digest(config),
                overrideSha256=digest(override), rolloutMode=value['rolloutMode'], userCount=len(value['userIds']),
                pendingPhase=phase, modelCalls=0)


def identities(c, containers):
    c.require(set(containers) == SERVICES, 'CODEX_CONFIGURATION_SERVICE_SET')
    return {name: dict(id=value['Id'], image=value['Image'], startedAt=value['State']['StartedAt'])
            for name, value in containers.items()}


def journal(c):
    value = c.decode(read(c, c.STATE/PENDING, 524288))
    expected = {'schemaVersion','operationId','phase','beforeSettings','afterSettings','beforeOverride',
                'afterOverride','version','containers','candidateName'}
    c.require(isinstance(value, dict) and set(value)==expected and type(value['schemaVersion']) is int and
              value['schemaVersion']==1 and value['phase'] in ('prepared','rolling_back','committed','rolled_back') and
              isinstance(value['operationId'], str) and re.fullmatch('[0-9a-f]{32}',value['operationId']), 'CODEX_CONFIGURATION_JOURNAL')
    c.require(value['candidateName']=='codex-candidate-'+value['operationId']+'.json', 'CODEX_CONFIGURATION_JOURNAL')
    policy=c.ops.codex_module()
    for side in ('before','after'):
        raw=value[side+'Settings']; override=value[side+'Override']
        c.require(isinstance(raw,str) and len(raw.encode())<=16384 and isinstance(override,str) and
                  len(override.encode())<=131072, 'CODEX_CONFIGURATION_JOURNAL')
        policy.settings(c.decode(raw)); c.require(isinstance(c.decode(override),dict), 'CODEX_CONFIGURATION_JOURNAL')
    c.require(isinstance(value['version'],dict) and isinstance(value['version'].get('version'),str) and
              re.fullmatch('[0-9a-f]{40}',value['version'].get('buildSha','')), 'CODEX_CONFIGURATION_JOURNAL')
    c.require(isinstance(value['containers'],dict) and set(value['containers'])==SERVICES, 'CODEX_CONFIGURATION_JOURNAL')
    for item in value['containers'].values():
        c.require(isinstance(item,dict) and set(item)=={'id','image','startedAt'} and
                  re.fullmatch('[0-9a-f]{64}',item.get('id','')) and
                  re.fullmatch('sha256:[0-9a-f]{64}',item.get('image','')) and
                  isinstance(item['startedAt'],str), 'CODEX_CONFIGURATION_JOURNAL')
    return value


def unchanged_files(c, record):
    # A third-party edit is not ours to overwrite during recovery.
    for path,key,limit in ((c.ROOT/SETTINGS,'Settings',16384),(c.OVERRIDE,'Override',131072)):
        raw=read(c,path,limit)
        c.require(raw in (record['before'+key].encode(),record['after'+key].encode()), 'CODEX_CONFIGURATION_EXTERNAL_CHANGE')


def stop_api(c, record):
    ids=c.command(c.DOCKER+['ps','-aq','--filter','label=com.docker.compose.project='+PROJECT,
                           '--filter','label=com.docker.compose.service=api']).decode().split()
    c.require(len(ids)<=1 and all(re.fullmatch('[0-9a-f]{12,64}',x)for x in ids), 'CODEX_CONFIGURATION_API_SCOPE')
    if not ids: return
    records=c.decode(c.command(c.DOCKER+['inspect',*ids])); c.require(len(records)==1,'CODEX_CONFIGURATION_API_SCOPE')
    api=records[0]; labels=api['Config'].get('Labels',{})
    c.require(api['Id'].startswith(ids[0]) and api['Image']==record['containers']['api']['image'] and
              labels.get('com.docker.compose.project')==PROJECT and
              labels.get('com.docker.compose.service')=='api' and labels.get('com.docker.compose.oneoff')=='False',
              'CODEX_CONFIGURATION_API_SCOPE')
    if not api['State']['Running']: return  # Recovery may find the previous failed stop.
    c.command(c.DOCKER+['stop','--time','30',api['Id']],timeout=45)
    stopped=c.decode(c.command(c.DOCKER+['inspect',api['Id']]))
    c.require(len(stopped)==1 and stopped[0]['Id']==api['Id'] and stopped[0]['Image']==api['Image'] and
              stopped[0]['State'].get('Running') is False and stopped[0]['State'].get('ExitCode')==0 and
              not stopped[0]['State'].get('OOMKilled') and not stopped[0]['State'].get('Restarting'),
              'CODEX_CONFIGURATION_API_SHUTDOWN_INCOMPLETE')


def start_pair(c):
    c.ops.preflight()
    for service,seconds in (('codex-agent-runtime',90),('api',180)):
        c.command(c.ops.compose_argv()+['up','-d','--no-deps','--no-build','--pull','never','--force-recreate',
                                      '--wait','--wait-timeout',str(seconds),service],timeout=seconds+30)


def verify(c, record, side):
    c.require(read(c,c.ROOT/SETTINGS,16384)==record[side+'Settings'].encode() and
              read(c,c.OVERRIDE,131072)==record[side+'Override'].encode(), 'CODEX_CONFIGURATION_BYTES_MISMATCH')
    deadline=time.monotonic()+180
    while True:
        sample=c.ops.snapshot()
        # Only this transaction's pending marker is expected while holding locks.
        if sample['healthy'] or sample.get('failures')==['CODEX_CONFIGURATION_PENDING']: break
        c.require(time.monotonic()<deadline,'CODEX_CONFIGURATION_HEALTH_TIMEOUT');time.sleep(4)
    value,containers=c.current()
    c.require(value==record['version'] and set(containers)==SERVICES,'CODEX_CONFIGURATION_RELEASE_CHANGED')
    actual=identities(c,containers)
    for name,before in record['containers'].items():
        c.require(actual[name]['image']==before['image'],'CODEX_CONFIGURATION_IMAGE_CHANGED')
        if name not in CHANGED:c.require(actual[name]==before,'CODEX_CONFIGURATION_UNRELATED_SERVICE_CHANGED')
    return dict(healthy=True,unchangedServices=len(SERVICES-CHANGED),modelCalls=0)


def finish(c, record, result):
    write(c,c.STATE/RECEIPT,{**result,'operationId':record['operationId'],
        'settingsSha256':digest(read(c,c.ROOT/SETTINGS,16384)),
        'overrideSha256':digest(read(c,c.OVERRIDE,131072))})
    remove(c,c.STATE/record['candidateName'])
    remove(c,c.STATE/PENDING)
    return result


def restore(c, record):
    unchanged_files(c,record)
    record['phase']='rolling_back';write(c,c.STATE/PENDING,record)
    stop_api(c,record)
    write(c,c.ROOT/SETTINGS,record['beforeSettings'].encode())
    write(c,c.OVERRIDE,record['beforeOverride'].encode())
    start_pair(c);verify(c,record,'before')
    record['phase']='rolled_back';write(c,c.STATE/PENDING,record)


def apply(c, request):
    marker=c.STATE/'codex-host-install.pending.json'
    c.require(not(marker.exists()or marker.is_symlink()),'CODEX_HOST_INSTALLATION_RECOVERY_REQUIRED')
    c.require(isinstance(request,dict) and set(request)=={'settingsSha256','overrideSha256','settings'},'CODEX_CONFIGURATION_REQUEST')
    for key in ('settingsSha256','overrideSha256'):
        c.require(isinstance(request[key],str) and re.fullmatch('[0-9a-f]{64}',request[key]),'CODEX_CONFIGURATION_REQUEST')
    policy=c.ops.codex_module(); desired=policy.settings(request['settings'])
    with c.release_lock(), c.ops.maintenance('codex-configuration',minutes=20):
        c.require(not(c.STATE/PENDING).exists(),'CODEX_CONFIGURATION_RECOVERY_REQUIRED')
        current_settings=read(c,c.ROOT/SETTINGS,16384);current_override=read(c,c.OVERRIDE,131072)
        c.require(digest(current_settings)==request['settingsSha256'] and digest(current_override)==request['overrideSha256'],
                  'CODEX_CONFIGURATION_STALE_REQUEST')
        previous=policy.settings(c.decode(current_settings))
        c.require(previous['modelBaseUrls']==desired['modelBaseUrls'],'CODEX_CONFIGURATION_ENDPOINT_CHANGE_REFUSED')
        version,containers=c.current(); before=identities(c,containers)
        c.require(c.ops.snapshot()['healthy'],'CODEX_CONFIGURATION_UNHEALTHY_SOURCE')
        if previous==desired:return dict(status='unchanged',modelCalls=0)
        raw=c.decode(current_override); runtime=raw.get('services',{}).get('codex-agent-runtime',{})
        c.require('image'in runtime,'CODEX_CONFIGURATION_RUNTIME_REQUIRED')
        extra=policy.trusted_extension(c.ops.codex_context(),runtime['image'],desired)
        candidate=copy.deepcopy(raw)
        for name in policy.SERVICES:candidate['services'][name]=extra['services'][name]
        candidate['services']['api']['environment'].update(extra['services']['api']['environment'])
        candidate_name='codex-candidate-'+uuid.uuid4().hex+'.json';candidate_path=c.STATE/candidate_name
        try:
            write(c,candidate_path,candidate)
            resolved=c.decode(c.command(c.ops.compose_argv(include_release=False)+['-f',str(candidate_path),'config','--format','json']))
            c.ops.validate_release(c.ops.config(include_release=False),resolved,approved_codex_settings=desired)
            c.require(read(c,c.ROOT/SETTINGS,16384)==current_settings and read(c,c.OVERRIDE,131072)==current_override,
                      'CODEX_CONFIGURATION_STALE_REQUEST')
            record=dict(schemaVersion=1,operationId=candidate_name.removeprefix('codex-candidate-').removesuffix('.json'),
                phase='prepared',beforeSettings=current_settings.decode(),afterSettings=canonical(desired).decode(),
                beforeOverride=current_override.decode(),afterOverride=read(c,candidate_path,131072).decode(),
                version=version,containers=before,candidateName=candidate_name)
            write(c,c.STATE/PENDING,record)
        except Exception:
            remove(c,candidate_path);raise
        try:
            stop_api(c,record)
            write(c,c.ROOT/SETTINGS,record['afterSettings'].encode())
            write(c,c.OVERRIDE,record['afterOverride'].encode())
            start_pair(c);proof=verify(c,record,'after')
            record['phase']='committed';write(c,c.STATE/PENDING,record)
        except Exception as error:
            c.rolling_back=True
            try:restore(c,record)
            except Exception:
                return dict(status='recovery_required',code='CODEX_CONFIGURATION_ROLLBACK_INCOMPLETE',causeCode=error_code(error),modelCalls=0)
            finally:c.rolling_back=False
            return finish(c,record,dict(status='failed',code='CODEX_CONFIGURATION_ROLLBACK_RESTORED',causeCode=error_code(error),modelCalls=0))
        # A crash after this durable commit finalizes the new state; it must not
        # replay the change or silently revert a successfully revoked account.
        return finish(c,record,dict(status='configured',rolloutMode=desired['rolloutMode'],userCount=len(desired['userIds']),**proof))


def recover(c):
    marker=c.STATE/'codex-host-install.pending.json'
    c.require(not(marker.exists()or marker.is_symlink()),'CODEX_HOST_INSTALLATION_RECOVERY_REQUIRED')
    with c.release_lock(), c.ops.maintenance('codex-configuration-recovery',minutes=20):
        c.require((c.STATE/PENDING).exists(),'CODEX_CONFIGURATION_RECOVERY_NOT_REQUIRED')
        record=journal(c);unchanged_files(c,record)
        c.rolling_back=True
        try:
            if record['phase']=='committed':
                proof=verify(c,record,'after')
                return finish(c,record,dict(status='configured',recoveredCommit=True,**proof))
            if record['phase']!='rolled_back':restore(c,record)
            else:verify(c,record,'before')
            return finish(c,record,dict(status='restored',modelCalls=0))
        finally:c.rolling_back=False


def main():
    # Root operator entry only; never added to the remotely accepted SSH actions.
    root=Path('/opt/teaching-ai-production/operations/release')
    if os.geteuid()!=0:raise ValueError('CODEX_CONFIGURATION_OPERATOR_SCOPE')
    for path in (root/'codex_configure.py',root/'receiver.py'):
        info=path.lstat()
        if os.geteuid()!=0 or path.resolve()!=path or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_gid!=0 or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('CODEX_CONFIGURATION_OPERATOR_SCOPE')
    if Path(__file__).resolve()!=root/'codex_configure.py':raise ValueError('CODEX_CONFIGURATION_OPERATOR_SCOPE')
    spec=importlib.util.spec_from_file_location('configuration_receiver',root/'receiver.py')
    c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c);c.initialize()
    c.require(len(sys.argv)==2 and sys.argv[1]in('status','apply','recover'),'CODEX_CONFIGURATION_ACTION')
    signal.alarm(1200)
    if sys.argv[1]=='apply':
        data=sys.stdin.buffer.read(20001);c.require(0<len(data)<=20000,'CODEX_CONFIGURATION_REQUEST_SIZE')
        result=apply(c,c.decode(data))
    elif sys.argv[1]=='recover':result=recover(c)
    else:result=status(c)
    print(json.dumps(result));return 0 if result['status']in('configured','unchanged','restored')else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps(dict(status='failed',code=error_code(error))));raise SystemExit(1)
