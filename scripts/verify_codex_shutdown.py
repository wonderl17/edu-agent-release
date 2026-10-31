"""Actual full API SIGTERM/restart; synthetic planner HTTP, no provider calls.

Uses the isolated application harness, replacing only the planning URL with a
local deterministic HTTP fixture. This is lifecycle evidence, not SDK/model or
production release/migration acceptance. All containers/volumes are disposable.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from verify_codex_application import Application, load, require


PLANNER = r"""
const http=require('http');let mode='hold',requests=0,disconnected=0,active=0;
http.createServer((req,res)=>{
 if(req.url==='/stats'||req.url==='/health'){res.setHeader('content-type','application/json');res.end(JSON.stringify({ok:true,requests,disconnected,active}));return}
 if(req.url==='/mode/hold'||req.url==='/mode/clarify'){mode=req.url.split('/').at(-1);res.end('{}');return}
 if(req.url!=='/plan'){res.writeHead(404);res.end('{}');return}
 req.resume();req.on('end',()=>{
  requests++;active++;res.once('close',()=>{active--;if(!res.writableEnded)disconnected++});
  if(mode==='hold')return;
  res.setHeader('content-type','application/json');res.end(JSON.stringify({plan:{summary:'Synthetic confirmation',requirements:'',clarification:'Synthetic missing detail'},usage:null}));
 });
}).listen(4986,'0.0.0.0');
"""


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--api-image',required=True)
    parser.add_argument('--runner-image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    require(re.fullmatch(r'teaching-codex-api:[a-z0-9-]+',args.api_image),'LOCAL_API_REQUIRED')
    require(re.fullmatch(r'teaching-codex-runtime:[a-z0-9-]+',args.runner_image),'LOCAL_RUNTIME_REQUIRED')
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED')
    docker=['docker','--host',endpoint]
    images={key:json.loads(subprocess.check_output(docker+['image','inspect',tag]))[0]
            for key,tag in dict(api=args.api_image,runner=args.runner_image,postgres='postgres:17-alpine',redis='redis:7.4-alpine').items()}
    require(all(image['Os']=='linux'for image in images.values()),'LINUX_REQUIRED')
    require(all(images[key]['Architecture']=='amd64'for key in ('api','runner')),'AMD64_APPLICATION_REQUIRED')
    configure,policy=load('codex_configure'),load('codex_runtime')
    project='codex-shutdown-'+uuid.uuid4().hex[:12]
    configure.PROJECT=policy.PROJECT=project
    require(not subprocess.check_output(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip(),'PROJECT_EXISTS')
    report=dict(status='incomplete',checks=[],project=project,apiImage=images['api']['Id'],modelCalls=0,productionChanged=False,
                scope='full API image original entrypoint + real PG/Redis; deterministic local planner HTTP',
                limitations=['planner fixture replaces SDK calls only for lifecycle control','five other application services are inert identity fixtures',
                             'fresh DB schema push, not production migration or old-release recovery','no domain Worker generation/drain in this suite'])
    report['scriptSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    def passed(name):
        report['checks'].append(name);print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-shutdown-') as temp:
        root=Path(temp).resolve();root.chmod(0o700)
        app=Application(root,docker,images,project,policy,configure)
        base=json.loads(app.base_path.read_bytes())
        fixture=root/'planner.cjs';fixture.write_text(PLANNER);fixture.chmod(0o644)
        base['services']['planner']=dict(image=images['runner']['Id'],platform='linux/amd64',
            command=['node','/fixture/planner.cjs'],networks={'app':None},volumes=[str(fixture)+':/fixture/planner.cjs:ro'],
            healthcheck=dict(test=['CMD','node','-e',"fetch('http://127.0.0.1:4986/health').then(r=>{if(!r.ok)process.exit(1)})"],interval='1s',timeout='3s',retries=10))
        app.atomic(app.base_path,base)
        override=app.extension(dict(app.initial,rolloutMode='allowlist',userIds=['teacher']))
        override['services']['api']['environment']['CODEX_AGENT_RUNNER_URL']='http://planner:4986'
        app.atomic(app.OVERRIDE,override)
        def planner(path):
            return json.loads(app.node("fetch('http://planner:4986'+"+json.dumps(path)+").then(async r=>console.log(JSON.stringify(await r.json())))",'web'))
        def wait_for(check,code,seconds=20):
            deadline=time.monotonic()+seconds
            while True:
                value=check()
                if value:return value
                require(time.monotonic()<deadline,code);time.sleep(0.5)
        def read(run_id):
            return app.db("const r=await p.agentRun.findUniqueOrThrow({where:{id:"+json.dumps(run_id)+"},include:{operations:true}});console.log(JSON.stringify({id:r.id,status:r.status,cancelRequested:r.cancelRequested,leaseToken:r.leaseToken,leaseUntil:r.leaseUntil,operationIds:r.operations.map(o=>o.id),attempts:r.operations.map(o=>o.attempts)}));",'web')
        def post(body):
            script="const jwt=require('module').createRequire('/app/apps/api/package.json')('jsonwebtoken');const token=jwt.sign({sub:'teacher',email:'teacher@example.invalid',passwordVersion:0},process.env.JWT_SECRET,{expiresIn:'5m'});"
            script+="fetch('http://api:4999/agent/runs',{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+token,'X-User-Llm-Provider':'openai-compatible','X-User-Llm-Api-Key':'offline-fixture-only','X-User-Llm-Model':'gpt-6-luna','X-User-Llm-Base-Url':'https://model.example.invalid/v1'},body:JSON.stringify("+json.dumps(body)+"),signal:AbortSignal.timeout(10000)}).then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()})))"
            response=json.loads(app.node(script,'web'))
            require(response['status']==202,'CREATE_SYNTHETIC_RUN_FAILED')
            return response['body']['run']['id']
        paused=None;started=False
        try:
            started=True
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60',
                'postgres','redis','web','worker-general','worker-ppt','storage','gateway','planner','codex-agent-runtime'],timeout=90)
            count=app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n);",'web')
            require(count==0,'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','db','push','--skip-generate'],{stdio:'inherit'})",'web',timeout=120)
            classroom=app.db("await p.user.create({data:{id:'teacher',email:'teacher@example.invalid',role:'ADMIN'}});const c=await p.classroom.create({data:{userId:'teacher',name:'synthetic shutdown class',subject:'数学'}});console.log(JSON.stringify(c.id));",'web')
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','120','api'],timeout=150)
            for failure in (False,True):
                planner('/mode/hold')
                input=dict(clientRequestId=str(uuid.uuid4()),workflowKind='lesson_series',context=dict(kind='classroom',id=classroom),request='Synthetic lifecycle task; ask for confirmation')
                run_id=post(input)
                wait_for(lambda:planner('/stats')['active']==1,'PLAN_NOT_STARTED')
                before=read(run_id);require(before['leaseToken']and not before['cancelRequested'],'PLAN_LEASE_MISSING')
                initial_requests=planner('/stats')['requests']
                api=app.inspect()['api'];data_ids={name:item['Id'] for name,item in app.inspect().items()if name!='api'}
                if failure:
                    paused=app.inspect()['postgres']['Id'];app.command(docker+['pause',paused])
                began=time.monotonic()
                app.command(docker+['stop','--time','30',api['Id']],timeout=40)
                elapsed=time.monotonic()-began
                stopped=json.loads(app.command(docker+['inspect',api['Id']]))[0]
                expected=1 if failure else 0
                require(not stopped['State']['Running']and stopped['State']['ExitCode']==expected,'SHUTDOWN_EXIT_MISMATCH')
                require(elapsed<28,'SHUTDOWN_NOT_BOUNDED')
                if paused:app.command(docker+['unpause',paused]);paused=None
                row=read(run_id)
                require(row['status']=='running'and not row['cancelRequested'],'SHUTDOWN_REVOKED_TASK')
                require(bool(row['leaseToken'])==failure,'SHUTDOWN_LEASE_STATE')
                require(planner('/stats')['active']==0,'PLANNING_CONNECTION_NOT_ABORTED')
                passed('database-outage-exits-nonzero-and-retains-lease'if failure else'actual-sigterm-aborts-http-and-releases-lease-before-clean-exit')
                planner('/mode/clarify')
                app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','120','api'],timeout=150)
                final=wait_for(lambda:(lambda r:r if r['status']=='waiting_input'else None)(read(run_id)),'RESTART_NOT_RESUMED',75)
                require(final['operationIds']==before['operationIds']and final['attempts']==[2],'RESTART_DUPLICATED_OPERATION')
                require(final['leaseToken']is None and planner('/stats')['requests']==initial_requests+1,'RESTART_EXECUTION_COUNT')
                require(post(input)==run_id,'REPLAY_DUPLICATED_TASK')
                require({name:item['Id']for name,item in app.inspect().items()if name!='api'}==data_ids,'UNRELATED_CONTAINER_REPLACED')
                report.setdefault('shutdownTimings',[]).append(dict(databaseOutage=failure,seconds=round(elapsed,3),exitCode=expected))
                passed('restart-after-timeout-resumes-same-operation-after-lease-expiry'if failure else'actual-api-restart-resumes-same-task-and-replay-does-not-duplicate')
            require(app.db("console.log(await p.aiJob.count());",'web')==0,'UNEXPECTED_DOMAIN_JOB')
            report['status']='passed'
        except BaseException as error:
            report['status']='failed';report['error']=configure.error_code(error)
            if getattr(app,'last_error',None):report['commandDiagnostic']=app.last_error
            raise
        finally:
            if paused:app.command(docker+['unpause',paused])
            if started:app.command(app.compose_argv()+['down','--volumes','--remove-orphans','--timeout','30'],timeout=90)
            report['testResourcesRemoved']=not app.command(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip()and not app.command(docker+['network','ls','-q','--filter','label=com.docker.compose.project='+project]).strip()
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
