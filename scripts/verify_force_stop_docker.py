"""Verify one force-stop against an isolated synthetic Docker container.

This is a local test only. It never accepts a production container ID or a
remote Docker context and does not read credentials or execute application code.
"""
import contextlib
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import time
from unittest.mock import patch


def run(args):
    return subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30).stdout


def main():
    endpoint=json.loads(run(['docker','context','inspect']))
    host=endpoint[0]['Endpoints']['docker']['Host']
    if not host.startswith('unix://'):raise RuntimeError('LOCAL_DOCKER_REQUIRED')
    image='teaching-codex-runtime:local-current';tag=secrets.token_hex(16)
    create=['docker','create','--name','teaching-force-fixture-'+tag,'--label','teaching.force.fixture='+tag,
            '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--pids-limit','32','--memory','128m','--restart','unless-stopped','--entrypoint','node',image,
            '-e','setInterval(()=>{},1000)']
    identity=run(create).decode().strip()
    try:
        item=json.loads(run(['docker','inspect',identity]))[0]
        if (item['Id']!=identity or item['Config']['Labels'].get('teaching.force.fixture')!=tag or
            item['HostConfig']['NetworkMode']!='none' or item.get('Mounts') or item['Config']['Entrypoint']!=['node'] or
            item['HostConfig']['RestartPolicy']['Name']!='unless-stopped'):
            raise RuntimeError('SYNTHETIC_SCOPE_REFUSED')
        run(['docker','start',identity]);item=json.loads(run(['docker','inspect',identity]))[0]
        path=Path(__file__).resolve().parents[1]/'server/api_drain.py'
        spec=importlib.util.spec_from_file_location('force_drain_fixture',path);drain=importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(),str(path),'exec'),drain.__dict__);drain.DOCKER=['docker']
        record={'tag':'teaching-api-drain-'+'a'*32,'phase':'stop_requested','parentPid':10,'parentStart':11,
                'expiresAt':time.monotonic()+300,'containerId':identity,'image':item['Image'],
                'startedAt':item['State']['StartedAt'],'pid':item['State']['Pid'],'namespaceInode':888,
                'forceExpiresAt':int(time.time())+120}
        requested=[]
        @contextlib.contextmanager
        def locked(_):yield
        def command(args,**_):
            if args==['docker','kill','--signal','KILL',identity]:requested.append('kill');return run(args)
            if args==['docker','inspect',identity]:return run(args)
            raise RuntimeError('UNEXPECTED_DOCKER_REQUEST')
        with patch.object(drain,'guard_lock',locked),patch.object(drain,'read_journal',side_effect=lambda:dict(record)),\
             patch.object(drain,'update_journal',side_effect=lambda _,phase:record.update(phase=phase)),\
             patch.object(drain,'marker_exists',return_value=False),patch.object(drain,'process_alive',return_value=True),\
             patch.object(drain,'namespace_inode',return_value=888),\
             patch.object(drain,'run',side_effect=command):
            drain.advance_stop(record['tag']);drain.advance_stop(record['tag'])
        # Restart policies may act asynchronously; verify the exact container
        # remains stopped, without restarts or a changed start timestamp.
        for _ in range(12):
            sample=json.loads(run(['docker','inspect',identity]))[0]
            final=sample['State']
            if (final['Running'] or final['Restarting'] or final['StartedAt']!=record['startedAt'] or
                sample.get('RestartCount')!=0):raise RuntimeError('FORCE_STOP_RESTARTED')
            time.sleep(1)
        if record['phase']!='stop_complete' or requested!=['kill'] or final['ExitCode']!=137 or final['OOMKilled']:
            raise RuntimeError('FORCE_STOP_NOT_PROVEN')
        print(json.dumps({'status':'passed','syntheticContainers':1,'killRequests':len(requested),
                          'exitCode':final['ExitCode'],'restartCount':sample['RestartCount'],
                          'restartPolicy':'unless-stopped','modelCalls':0,'businessTasks':0}))
    finally:
        # Only the exact created ID is ever removed, after rechecking its label.
        item=json.loads(run(['docker','inspect',identity]))[0]
        if item['Id']==identity and item['Config']['Labels'].get('teaching.force.fixture')==tag:
            run(['docker','rm','--force',identity])


if __name__=='__main__':main()
