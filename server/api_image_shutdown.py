#!/usr/bin/python3
"""Accept the exact release API image in a disposable, offline Docker stack.

The API keeps its original entrypoint and command. Only upstream services are
deterministic fixtures. Helper mode executes an isolated copy of the installed
drain helper, adapting paths, unit names and its narrowly scoped Docker adapter;
no shutdown algorithm, timing or application module is replaced.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse


def require(value, code):
    if not value:
        raise ValueError(code)


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def wait_for(check, code, seconds=25):
    deadline = time.monotonic() + seconds
    while True:
        result = check()
        if result:
            return result
        require(time.monotonic() < deadline, code)
        time.sleep(.25)


UPSTREAM = r"""
const fs=require('fs'),http=require('http'),https=require('https');
const reqApi=require('module').createRequire('/app/apps/api/package.json');
const{WebSocketServer}=reqApi('ws');
let mode='hold',planRequests=0,planActive=0,chatRequests=0,chatActive=0,asrOpened=0,asrActive=0;
const stats=()=>({ok:true,mode,planRequests,planActive,chatRequests,chatActive,asrOpened,asrActive});
const server=http.createServer((req,res)=>{
 res.setHeader('content-type','application/json');
 if(req.url==='/health'||req.url==='/stats'){res.end(JSON.stringify(stats()));return}
 if(req.url==='/mode/hold'||req.url==='/mode/clarify'){mode=req.url.split('/').at(-1);res.end('{}');return}
 if(req.url==='/v1/models'){res.end(JSON.stringify({data:[{id:'gpt-6-luna',max_output_tokens:800}]}));return}
 if(req.url!=='/plan'&&req.url!=='/v1/chat/completions'&&req.url!=='/v1/responses'){res.writeHead(404);res.end('{}');return}
 req.resume();req.on('end',()=>{
  const planning=req.url==='/plan';if(planning){planRequests++;planActive++}else{chatRequests++;chatActive++}
  res.once('close',()=>{if(planning)planActive--;else chatActive--});
  if(!planning||mode==='hold')return;
  res.end(JSON.stringify({plan:{summary:'Synthetic confirmation',requirements:'',clarification:'Synthetic missing detail'},usage:null}));
 });
});server.listen(4986,'0.0.0.0');
const tls=https.createServer({key:fs.readFileSync('/fixture/key.pem'),cert:fs.readFileSync('/fixture/ca.pem')},(_q,r)=>r.end('{}'));
const wss=new WebSocketServer({server:tls});wss.on('connection',socket=>{asrOpened++;asrActive++;socket.on('error',()=>{});socket.on('close',()=>asrActive--)});
tls.listen(4987,'0.0.0.0');
"""


CLIENTS = r"""
const fs=require('fs'),http=require('http');
const reqApi=require('module').createRequire('/app/apps/api/package.json'),WS=reqApi('ws'),jwt=reqApi('jsonwebtoken');
const token=jwt.sign({sub:'teacher',email:'teacher@example.invalid',passwordVersion:0},process.env.JWT_SECRET,{expiresIn:'10m'});
let current=null;
const save=()=>fs.writeFileSync('/state/client.json',JSON.stringify(current));
http.createServer((req,res)=>{
 if(req.url==='/health'){res.end('{}');return}
 if(req.url==='/state'){res.setHeader('content-type','application/json');res.end(JSON.stringify(current));return}
 if(req.url!=='/start'){res.writeHead(404);res.end('{}');return}
 if(current&&!current.done){res.writeHead(409);res.end('{}');return}
 current={sseReady:false,sseEnded:false,sseText:'',wsReady:false,wsClosed:false,wsCode:null,httpStarted:false,httpEnded:false,httpInterrupted:false,done:false};save();
 const sse=http.get('http://api:4999/lesson-plans/ai-jobs/events',{headers:{authorization:'Bearer '+token,accept:'text/event-stream'}},r=>{
  current.sseStatus=r.statusCode;current.sseReady=r.statusCode===200&&String(r.headers['content-type']).includes('text/event-stream');save();
  r.on('data',b=>{current.sseText=(current.sseText+b.toString()).slice(-16000);save()});
  r.on('end',()=>{current.sseEnded=true;save()});r.on('error',e=>{current.sseError=e.code||e.name;save()});
 });sse.on('error',e=>{current.sseError=e.code||e.name;save()});
 const ws=new WS('ws://api:4999/account-model-settings/asr-stream?profileId=system:asr',{headers:{authorization:'Bearer '+token}});
 ws.on('open',()=>{current.wsReady=true;save()});ws.on('error',e=>{current.wsError=e.code||e.name;save()});
 ws.on('close',code=>{current.wsClosed=true;current.wsCode=code;save()});
 const body=JSON.stringify({provider:'openai-compatible',apiKey:'offline-fixture-only',model:'gpt-6-luna',baseUrl:'http://upstream:4986/v1',prompt:'synthetic hold',modelMaxOutputTokens:800});
 const request=http.request('http://api:4999/model-test/llm',{method:'POST',headers:{authorization:'Bearer '+token,'content-type':'application/json','content-length':Buffer.byteLength(body)}},r=>{
  current.httpStatus=r.statusCode;r.resume();r.on('end',()=>{current.httpEnded=true;save()});r.on('aborted',()=>{current.httpInterrupted=true;save()});
 });request.on('socket',()=>{current.httpStarted=true;save()});request.on('error',e=>{current.httpInterrupted=true;current.httpError=e.code||e.name;save()});request.end(body);
 res.end('{}');
}).listen(4988,'0.0.0.0');
"""


API_ENTRYPOINT = ['/app/docker/entrypoint.sh']
API_COMMAND = ['node', 'apps/api/dist/server.mjs']
API_REFERENCE = re.compile(r'docker\.cnb\.cool/shukezhinao/edu-agent-cloud-build/api@(sha256:[0-9a-f]{64})')
ADMISSION_RUNTIME_PROBE = r"""
const fs=require('fs'),crypto=require('crypto'),root='/app/apps/api/dist';
const flag=process.env.DEPLOYMENT_ADMISSION_ENABLED;
if(flag!==undefined&&flag!=='true'&&flag!=='false')throw Error('ADMISSION_FLAG_INVALID');
const result={flag:flag??null,manifest:null,entrySha256:null,controlSha256:null};
let stat;
try{stat=fs.lstatSync(root+'/deployment-admission-runtime.json')}catch(error){if(error.code!=='ENOENT')throw error}
if(stat){
 if(!stat.isFile()||stat.isSymbolicLink()||stat.size>65536)throw Error('ADMISSION_MARKER_INVALID');
 result.manifest=JSON.parse(fs.readFileSync(root+'/deployment-admission-runtime.json','utf8'));
 for(const [key,name]of [['entrySha256','server.mjs'],['controlSha256','deployment-admission-control.mjs']]){
  const file=root+'/'+name,info=fs.lstatSync(file);
  if(!info.isFile()||info.isSymbolicLink())throw Error('ADMISSION_FILE_INVALID');
  result[key]=crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
 }
}
console.log(JSON.stringify(result));
"""


def image_admission_flag(image):
    rows=image.get('Config',{}).get('Env',[])
    require(isinstance(rows,list) and all(isinstance(row,str) for row in rows),'API_ADMISSION_FLAG_INVALID')
    flags=[row.split('=',1)[1] for row in rows if row.startswith('DEPLOYMENT_ADMISSION_ENABLED=')]
    require(not flags or len(flags)==1 and flags[0] in ('true','false'),'API_ADMISSION_FLAG_INVALID')
    return flags[0] if flags else None


def validate_admission_runtime(value, flag):
    require(isinstance(value,dict) and set(value)=={'flag','manifest','entrySha256','controlSha256'} and
            value['flag']==flag,'API_ADMISSION_RUNTIME_INVALID')
    manifest=value['manifest']
    if manifest is None:
        require(flag!='true' and value['entrySha256'] is None and value['controlSha256'] is None,
                'API_ADMISSION_MARKER_REQUIRED')
        return False
    require(isinstance(manifest,dict) and set(manifest)=={'protocolVersion','role','entrySha256','controlSha256'} and
            type(manifest['protocolVersion']) is int and manifest['protocolVersion']==2 and manifest['role']=='api',
            'API_ADMISSION_MARKER_INVALID')
    for key in ('entrySha256','controlSha256'):
        require(isinstance(manifest[key],str) and re.fullmatch(r'[0-9a-f]{64}',manifest[key]) and
                value[key]==manifest[key],'API_ADMISSION_BUNDLE_MISMATCH')
    return True


class Harness:
    def __init__(self, root, docker, images, project):
        self.root, self.docker, self.images, self.project = root, docker, images, project
        self.cleanup_deadline = None
        self.compose = root / 'compose.json'
        self.fixture = root / 'fixture'; self.fixture.mkdir(mode=0o755)
        self.state = root / 'clients'; self.state.mkdir(mode=0o777); self.state.chmod(0o777)
        self.env = dict(NODE_ENV='production', HOST='0.0.0.0', PORT='4999',
            DATABASE_HOST='postgres', DATABASE_URL='postgresql://postgres:' + secrets.token_hex(24) + '@postgres:5432/shutdown_acceptance?schema=public',
            REDIS_URL='redis://redis:6379', JWT_SECRET=secrets.token_hex(32),
            MODEL_CREDENTIAL_ENCRYPTION_KEY=secrets.token_urlsafe(32), API_BACKGROUND_TASKS_ENABLED='true',
            AI_WORKER_ENABLED='false', MONITORING_ENABLED='false', PRISMA_MIGRATE_DEPLOY='false', PRISMA_DB_PUSH='false',
            AGENT_WORKFLOWS_ENABLED='true', AGENT_ROLLOUT_MODE='allowlist', AGENT_ROLLOUT_USER_IDS='teacher',
            CODEX_AGENT_RUNNER_URL='http://upstream:4986', CODEX_AGENT_RUNNER_TOKEN=secrets.token_hex(32),
            CODEX_AGENT_EGRESS_ENABLED='false', MODEL_ASR_PROXY_ALLOWED_HOSTS='upstream', NODE_EXTRA_CA_CERTS='/fixture/ca.pem')
        password = self.env['DATABASE_URL'].split(':', 2)[2].split('@')[0]
        def health(port):
            return dict(test=['CMD', 'node', '-e', "fetch('http://127.0.0.1:" + str(port) + "/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"], interval='1s', timeout='5s', retries=30, start_period='5s')
        common = dict(image=images['api']['Id'], platform='linux/amd64', networks=['offline'], restart='no',
                      environment=self.env, labels={'teaching.shutdown.acceptance':project},
                      volumes=[str(self.fixture)+':/fixture:ro'], pids_limit=256)
        api = dict(common, mem_limit='1g', cpus=.75, healthcheck=health(4999))
        client = dict(common, mem_limit='256m', cpus=.25, entrypoint=['node'], command=['/fixture/clients.cjs'],
                      volumes=common['volumes']+[str(self.state)+':/state'], healthcheck=health(4988))
        upstream = dict(common, mem_limit='256m', cpus=.25, entrypoint=['node'], command=['/fixture/upstream.cjs'], healthcheck=health(4986))
        services = dict(api=api, client=client, upstream=upstream,
            postgres=dict(image=images['postgres']['Id'], mem_limit='384m', cpus=.5, networks=['offline'], restart='no',
                labels={'teaching.shutdown.acceptance':project},
                environment=dict(POSTGRES_PASSWORD=password, POSTGRES_DB='shutdown_acceptance'),
                healthcheck=dict(test=['CMD-SHELL','pg_isready -U postgres'],interval='1s',timeout='3s',retries=30)),
            redis=dict(image=images['redis']['Id'], mem_limit='128m', cpus=.25, networks=['offline'], restart='no',
                labels={'teaching.shutdown.acceptance':project},
                healthcheck=dict(test=['CMD','redis-cli','ping'],interval='1s',timeout='3s',retries=30)))
        atomic(self.compose, dict(name=project, services=services, networks=dict(offline=dict(internal=True))))
        (self.fixture/'upstream.cjs').write_text(UPSTREAM)
        (self.fixture/'clients.cjs').write_text(CLIENTS)
        for path in self.fixture.iterdir(): path.chmod(0o644)

    def command(self, argv, *, data=None, timeout=45):
        require(argv[:len(self.docker)] == self.docker, 'DOCKER_SCOPE_REQUIRED')
        if self.cleanup_deadline is not None:
            require(time.monotonic()<self.cleanup_deadline,'CLEANUP_DEADLINE')
            timeout=min(timeout,max(.1,self.cleanup_deadline-time.monotonic()))
        env = {k:v for k,v in os.environ.items() if not k.startswith(('DOCKER_', 'COMPOSE_'))}
        try:
            return subprocess.check_output(argv, input=data, stderr=subprocess.PIPE, env=env, timeout=timeout)
        except subprocess.CalledProcessError as error:
            self.last_error = dict(command=argv[len(self.docker):], exitCode=error.returncode,
                                   stderr=self.redact(error.stderr.decode(errors='replace')[-6000:]))
            raise ValueError('ISOLATED_DOCKER_COMMAND_FAILED') from None

    def redact(self, value):
        for key, secret in self.env.items():
            if any(part in key for part in ('TOKEN','SECRET','KEY','PASSWORD','DATABASE_URL')):
                value=value.replace(secret,'[REDACTED]')
        return value

    def argv(self):
        return self.docker+['compose','--project-name',self.project,'--project-directory',str(self.root),'-f',str(self.compose)]

    def inspect(self):
        ids = self.command(self.docker+['ps','-aq','--no-trunc','--filter','label=com.docker.compose.project='+self.project]).decode().split()
        require(ids, 'ISOLATED_CONTAINERS_MISSING')
        rows = json.loads(self.command(self.docker+['inspect',*ids]))
        return {item['Config']['Labels']['com.docker.compose.service']:item for item in rows}

    def node(self, source, timeout=30):
        return self.command(self.argv()+['exec','-T','client','node','--input-type=commonjs'], data=source.encode(),timeout=timeout)

    def db(self, source):
        prefix="const r=require('module').createRequire('/app/apps/api/package.json');const{PrismaClient}=r('@prisma/client');const p=new PrismaClient();"
        return json.loads(self.node(prefix+"(async()=>{"+source+"})().finally(()=>p.$disconnect()).catch(e=>{console.error(e.code||e.name);process.exitCode=1})"))

    def prepare_admission(self):
        flag=image_admission_flag(self.images['api'])
        validated=validate_admission_runtime(json.loads(self.node(ADMISSION_RUNTIME_PROBE)),flag)
        result=dict(protocolVersion=2 if validated else 1,enabled=flag=='true',isolatedGateSeeded=False)
        if flag!='true':return result
        # This path may create exactly one synthetic row, never upsert/reset an
        # existing gate. Check both the host-generated isolation scope and the
        # database's own name before issuing any write.
        url=urlparse(self.env['DATABASE_URL'])
        config=json.loads(self.compose.read_text())
        postgres=config.get('services',{}).get('postgres',{})
        require(re.fullmatch(r'api-shutdown-[0-9a-f]{16}',self.project) and config.get('name')==self.project and
                self.env.get('DATABASE_HOST')=='postgres' and url.scheme=='postgresql' and url.hostname=='postgres' and
                url.port==5432 and url.path=='/shutdown_acceptance' and url.query=='schema=public' and not url.fragment and
                postgres.get('environment',{}).get('POSTGRES_DB')=='shutdown_acceptance' and
                postgres.get('image')==self.images['postgres']['Id'] and not postgres.get('volumes') and
                all(network.get('internal') and not network.get('external') for network in config.get('networks',{}).values()) and
                config.get('networks') and all(not service.get('ports') and not service.get('extra_hosts') and
                    not service.get('network_mode') for service in config.get('services',{}).values()),
                'ISOLATED_ADMISSION_DATABASE_REQUIRED')
        seeded=self.db("const names=await p.$queryRawUnsafe('SELECT current_database() AS name');"
            "if(names.length!==1||names[0].name!=='shutdown_acceptance')throw Error('ISOLATED_ADMISSION_DATABASE_REQUIRED');"
            "const gate=await p.deploymentGate.create({data:{id:'production',state:'accepting',generation:0n}});"
            "console.log(JSON.stringify({id:gate.id,state:gate.state,generation:String(gate.generation)}));")
        require(seeded==dict(id='production',state='accepting',generation='0'),'ISOLATED_ADMISSION_SEED_FAILED')
        result['isolatedGateSeeded']=True
        return result

    def request(self, url, body=None, method='GET'):
        pre="const jwt=require('module').createRequire('/app/apps/api/package.json')('jsonwebtoken');const token=jwt.sign({sub:'teacher',email:'teacher@example.invalid',passwordVersion:0},process.env.JWT_SECRET,{expiresIn:'10m'});"
        script=pre+"fetch("+json.dumps(url)+",{method:"+json.dumps(method)+",headers:{authorization:'Bearer '+token,'content-type':'application/json'},signal:AbortSignal.timeout(10000)"
        if body is not None:script+=",body:JSON.stringify("+json.dumps(body)+")"
        script+="}).then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()}))).catch(e=>{console.error(e.code||e.name);process.exitCode=1})"
        return json.loads(self.node(script))

    def stats(self):
        return self.request('http://upstream:4986/stats')['body']

    def client(self):
        return self.request('http://client:4988/state')['body']

    def run(self, classroom, request_id):
        body=dict(clientRequestId=request_id,workflowKind='lesson_series',context=dict(kind='classroom',id=classroom),request='Synthetic lifecycle task; ask for confirmation')
        pre="const jwt=require('module').createRequire('/app/apps/api/package.json')('jsonwebtoken');const token=jwt.sign({sub:'teacher',email:'teacher@example.invalid',passwordVersion:0},process.env.JWT_SECRET,{expiresIn:'10m'});"
        script=pre+"fetch('http://api:4999/agent/runs',{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+token,'X-User-Llm-Provider':'openai-compatible','X-User-Llm-Api-Key':'offline-fixture-only','X-User-Llm-Model':'gpt-6-luna','X-User-Llm-Base-Url':'https://model.example.invalid/v1'},body:JSON.stringify("+json.dumps(body)+"),signal:AbortSignal.timeout(10000)}).then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()})))"
        response=json.loads(self.node(script));require(response['status']==202,'CREATE_SYNTHETIC_RUN_FAILED')
        return response['body']['run']['id']

    def read(self, run_id):
        return self.db("const r=await p.agentRun.findUniqueOrThrow({where:{id:"+json.dumps(run_id)+"},include:{operations:true}});console.log(JSON.stringify({id:r.id,status:r.status,cancelRequested:r.cancelRequested,leaseToken:r.leaseToken,leaseUntil:r.leaseUntil,operationIds:r.operations.map(o=>o.id),attempts:r.operations.map(o=>o.attempts)}));")

    def verify_api(self, item):
        config, host = item['Config'], item['HostConfig']
        expected=self.images['api']['Config']
        require(item['Image']==self.images['api']['Id'] and config.get('Entrypoint')==expected.get('Entrypoint') and
                config.get('Cmd')==expected.get('Cmd') and config.get('Labels',{}).get('teaching.shutdown.acceptance')==self.project and
                config.get('Labels',{}).get('com.docker.compose.project')==self.project and
                not host.get('Privileged') and not host.get('PortBindings') and not host.get('CapAdd') and
                host.get('RestartPolicy',{}).get('Name')=='no' and len(item.get('Mounts',[]))==1 and
                item['Mounts'][0]['Source']==str(self.fixture) and item['Mounts'][0]['Destination']=='/fixture' and
                item['Mounts'][0]['RW'] is False, 'ORIGINAL_API_IDENTITY_REQUIRED')


class Helper:
    def __init__(self, app, source):
        self.app=app; self.root=app.root/'helper';self.root.mkdir(mode=0o700)
        self.source=source;self.module=None;self.tag=None
        original=source.read_text()
        substitutions={
            "DOCKER=['/usr/bin/docker','--host','unix:///var/run/docker.sock']":"DOCKER=['/usr/bin/python3',"+repr(str(self.root/'docker_adapter.py'))+"]",
            "JOURNAL=Path('/opt/teaching-ai-production/operations/release/state/api-drain.pending.json')":"JOURNAL=Path("+repr(str(self.root/'journal.json'))+")",
            "RUN=Path('/run/teaching-production')":"RUN=Path("+repr(str(self.root))+")",
            'teaching-api-drain-':'api-image-shutdown-'+app.project.split('-')[-1]+'-',
        }
        adapted=original
        for old,new in substitutions.items():
            require(adapted.count(old)==(5 if old=='teaching-api-drain-' else 1),'HELPER_ADAPTER_SOURCE_CHANGED')
            adapted=adapted.replace(old,new)
        require('/opt/teaching-ai-production' not in adapted and '/run/teaching-production' not in adapted and '/var/run/docker.sock' not in adapted,'HELPER_ADAPTER_UNSCOPED')
        self.path=self.root/'helper.py';self.path.write_text(adapted);self.path.chmod(0o600)
        self.adapted_sha256=hashlib.sha256(adapted.encode()).hexdigest()
        self.substitutions=[dict(before=old,after=new,count=original.count(old)) for old,new in substitutions.items()]
        spec=importlib.util.spec_from_file_location('isolated_shutdown_helper',self.path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)

    def stop(self, api, failure):
        self.app.verify_api(api)
        allowed=dict(docker=self.app.docker,containerId=api['Id'],image=api['Image'],startedAt=api['State']['StartedAt'],
                     pid=api['State']['Pid'],project=self.app.project,fixture=str(self.app.fixture),
                     entrypoint=self.app.images['api']['Config'].get('Entrypoint'),command=self.app.images['api']['Config'].get('Cmd'))
        atomic(self.root/'allowed.json',allowed)
        adapter=r'''import json,os,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parent;allowed=json.loads((root/'allowed.json').read_text());args=sys.argv[1:]
if args not in (['inspect',allowed['containerId']],['stop','--time','30',allowed['containerId']]):raise SystemExit(2)
env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'}
def call(arguments):return subprocess.check_output(allowed['docker']+arguments,stderr=subprocess.PIPE,env=env,timeout=45)
rows=json.loads(call(['inspect',allowed['containerId']]));assert len(rows)==1
item=rows[0];cfg=item['Config'];host=item['HostConfig'];mounts=item.get('Mounts',[])
assert item['Id']==allowed['containerId'] and item['Image']==allowed['image']
assert cfg.get('Entrypoint')==allowed['entrypoint'] and cfg.get('Cmd')==allowed['command']
assert cfg.get('Labels',{}).get('teaching.shutdown.acceptance')==allowed['project']
assert cfg.get('Labels',{}).get('com.docker.compose.project')==allowed['project']
assert not host.get('Privileged') and not host.get('PortBindings') and not host.get('CapAdd')
assert len(mounts)==1 and mounts[0]['Source']==allowed['fixture'] and mounts[0]['Destination']=='/fixture' and not mounts[0]['RW']
if args[0]=='inspect':print(json.dumps(rows));raise SystemExit(0)
assert item['State']['Running'] and item['State']['StartedAt']==allowed['startedAt'] and item['State']['Pid']==allowed['pid']
path=root/'stop.json';fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);started=time.monotonic()
with os.fdopen(fd,'w')as f:json.dump({'requestedAt':started,'containerId':allowed['containerId']},f);f.flush();os.fsync(f.fileno())
result=call(args);stopped=json.loads(call(['inspect',allowed['containerId']]))[0]
temp=root/'stop.complete.json';temp.write_text(json.dumps({'requestedAt':started,'seconds':time.monotonic()-started,'exitCode':stopped['State']['ExitCode'],'containerId':allowed['containerId']}));temp.chmod(0o600);os.replace(temp,path)
sys.stdout.buffer.write(result)
'''
        (self.root/'docker_adapter.py').write_text(adapter);(self.root/'docker_adapter.py').chmod(0o600)
        for name in ('stop.json','stop.complete.json'):(self.root/name).unlink(missing_ok=True)
        fence=self.module.Fence(api);self.tag=fence.tag
        started=time.monotonic();expected_error=False
        try:
            with fence:
                entered=time.monotonic()-started
                require(entered<25,'HELPER_ENTRY_NOT_BOUNDED')
                fence.stop()
        except self.module.DrainError as error:
            if failure and str(error)=='API_DRAIN_STOP_UNCERTAIN':expected_error=True
            else:raise
        require(expected_error==failure,'HELPER_BUSINESS_FAILURE_NOT_REJECTED')
        stop=json.loads((self.root/'stop.json').read_text())
        require('seconds' in stop and stop['containerId']==api['Id'],'HELPER_STOP_NOT_CONFIRMED')
        if failure:
            require(self.module.read_journal()['phase']=='stop_uncertain','HELPER_FAILURE_GUARD_MISSING')
        self.cleanup()
        require(not self.module.JOURNAL.exists(),'HELPER_RESOURCES_NOT_REMOVED')
        return dict(seconds=round(stop['seconds'],3),entrySeconds=round(entered,3),exitCode=stop['exitCode'])

    def cleanup(self):
        module=self.module
        if module is None or self.tag is None:return
        previous_deadline=self.app.cleanup_deadline
        if previous_deadline is None:self.app.cleanup_deadline=time.monotonic()+55
        original_run=module.run
        def bounded_run(args,*,fd=None,timeout=10):
            require(time.monotonic()<self.app.cleanup_deadline,'CLEANUP_DEADLINE')
            return original_run(args,fd=fd,timeout=min(timeout,5,max(.1,self.app.cleanup_deadline-time.monotonic())))
        module.run=bounded_run
        try:self._cleanup()
        finally:
            module.run=original_run
            self.app.cleanup_deadline=previous_deadline

    def _cleanup(self):
        module=self.module
        def system(arguments):
            require(time.monotonic()<self.app.cleanup_deadline,'CLEANUP_DEADLINE')
            return subprocess.run(arguments,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  timeout=min(5,max(.1,self.app.cleanup_deadline-time.monotonic())))
        require(self.tag.startswith('api-image-shutdown-'+self.app.project.split('-')[-1]+'-'),'HELPER_CLEANUP_SCOPE')
        unit=self.tag+'.service'
        system([module.SYSTEMCTL,'stop',unit])
        result=system([module.SYSTEMCTL,'show','--value','-p','ActiveState',unit])
        require(result.returncode==0 and result.stdout.strip() in (b'inactive',b'failed'),'HELPER_WATCHDOG_STILL_ACTIVE')
        if result.stdout.strip()==b'failed':
            require(system([module.SYSTEMCTL,'reset-failed',unit]).returncode==0,'HELPER_UNIT_RESET_FAILED')
        if not module.JOURNAL.exists():
            require(not list(self.root.glob(self.tag+'.*')),'HELPER_MARKERS_NOT_REMOVED')
            return
        journal=module.read_journal();tag=journal['tag']
        require(tag==self.tag and tag.startswith('api-image-shutdown-'+self.app.project.split('-')[-1]+'-'),'HELPER_CLEANUP_SCOPE')
        # Only this fixture's process and namespace can be reconciled. Never
        # operate on the installed helper's journal or production namespace.
        api=self.app.inspect()['api'];self.app.verify_api(api)
        require(api['Id']==journal['containerId'] and api['Image']==journal['image'],'HELPER_CLEANUP_IDENTITY')
        pin=module.marker(tag,'pin')
        if pin.exists() and pin.stat().st_ino==journal['namespaceInode']:
            fd=os.open(pin,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                for binary in module.IPTABLES:module.remove_if_present(binary,fd,tag)
            finally:os.close(fd)
        with module.guard_lock(tag):
            module.update_journal(tag,'rules_removed');module.finish_cleanup(module.read_journal())
        require(not module.JOURNAL.exists() and not pin.exists() and not list(self.root.glob(tag+'.*')),'HELPER_CLEANUP_INCOMPLETE')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-image',required=True)
    parser.add_argument('--source-sha',required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--postgres-image',required=True)
    parser.add_argument('--redis-image',required=True)
    parser.add_argument('--helper-source',type=Path,required=True)
    parser.add_argument('--mode',choices=('helper','signal'),default='helper')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--docker-host')
    args=parser.parse_args()
    require(re.fullmatch('[0-9a-f]{40}',args.source_sha),'SOURCE_SHA_REQUIRED')
    require(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?',args.version),'VERSION_REQUIRED')
    digest=API_REFERENCE.fullmatch(args.api_image)
    # Explicit legacy original-package recovery uses the already verified local
    # immutable image ID; mutable local tags never select a release image.
    require(digest or re.fullmatch(r'sha256:[0-9a-f]{64}',args.api_image),'IMMUTABLE_API_REQUIRED')
    require(all(re.fullmatch('sha256:[0-9a-f]{64}',x) for x in (args.postgres_image,args.redis_image)),'IMMUTABLE_DATA_IMAGES_REQUIRED')
    require(args.helper_source.is_file() and not args.helper_source.is_symlink(),'HELPER_SOURCE_REQUIRED')
    if args.mode=='helper':
        require(os.geteuid()==0 and sys.platform=='linux' and Path('/run').is_dir(),'ROOT_LINUX_HOST_REQUIRED')
        require(os.stat('/proc/self/ns/net').st_ino==os.stat('/proc/1/ns/net').st_ino,'HOST_NAMESPACE_REQUIRED')
        info=args.helper_source.lstat()
        require(args.helper_source.is_absolute() and args.helper_source.resolve()==args.helper_source and
                stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_gid==0 and info.st_nlink==1 and
                stat.S_IMODE(info.st_mode)==0o600,'TRUSTED_HELPER_SOURCE_REQUIRED')
        require(all(parent.stat().st_uid==0 and not parent.stat().st_mode&0o022 for parent in args.helper_source.parents),
                'TRUSTED_HELPER_DIRECTORY_REQUIRED')
        endpoint='unix:///var/run/docker.sock'
        require(not args.docker_host or args.docker_host==endpoint,'HOST_DOCKER_REQUIRED')
    else:
        endpoint=args.docker_host
        if not endpoint:
            endpoint=json.loads(subprocess.check_output(['docker','context','inspect'],timeout=10))[0]['Endpoints']['docker']['Host']
        require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED')
    docker=['/usr/bin/docker' if args.mode=='helper' else 'docker','--host',endpoint]
    leftovers=subprocess.check_output(docker+['ps','-aq','--filter','label=teaching.shutdown.acceptance'],stderr=subprocess.PIPE,timeout=15)
    require(not leftovers.strip(),'PREVIOUS_ACCEPTANCE_RESOURCES_REQUIRE_RECOVERY')
    if args.mode=='helper':
        stale=[path for root in Path('/run').glob('api-shutdown-*') if root.is_dir() for path in (root/'helper').glob('journal.json')]
        require(not stale,'PREVIOUS_ACCEPTANCE_HELPER_REQUIRES_RECOVERY')
    images={kind:json.loads(subprocess.check_output(docker+['image','inspect',reference],stderr=subprocess.PIPE,timeout=15))[0]
            for kind,reference in dict(api=args.api_image,postgres=args.postgres_image,redis=args.redis_image).items()}
    require(all(i['Os']=='linux' for i in images.values()) and images['api']['Architecture']=='amd64','LINUX_AMD64_API_REQUIRED')
    image_config=images['api']['Config']
    require(image_config.get('Entrypoint')==API_ENTRYPOINT and image_config.get('Cmd')==API_COMMAND,'RELEASE_API_ENTRYPOINT_REQUIRED')
    labels=image_config.get('Labels')or{}
    require(labels.get('org.opencontainers.image.revision')==args.source_sha,'API_SOURCE_REVISION_MISMATCH')
    if 'org.opencontainers.image.version' in labels:
        require(labels['org.opencontainers.image.version']==args.version,'API_SOURCE_VERSION_MISMATCH')
    if digest:require(args.api_image in (images['api'].get('RepoDigests')or[]),'API_DIGEST_NOT_LOADED')
    project='api-shutdown-'+uuid.uuid4().hex[:16]
    report=dict(schemaVersion=1,scope='teaching-production-api-shutdown-v1',status='incomplete',sourceSha=args.source_sha,version=args.version,
        apiImageDigest=digest.group(1) if digest else args.api_image,apiImageId=images['api']['Id'],
        helperSha256=hashlib.sha256(args.helper_source.read_bytes()).hexdigest(),kernel=platform.release(),executionMode=args.mode,
        scriptSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),realModelCalls=0,project=project,
        checks={},timings=[],limitations=['fresh synthetic schema, not a production migration test','local deterministic planner and model HTTP, no provider inference'])
    paused=None;app=None;helper=None;started=False;interrupted=False
    def cancel(_signal,_frame):
        nonlocal interrupted
        if interrupted:return
        interrupted=True
        raise KeyboardInterrupt('ACCEPTANCE_CANCELLED')
    old_handlers={sig:signal.signal(sig,cancel) for sig in (signal.SIGTERM,signal.SIGINT)}
    root=Path(tempfile.mkdtemp(prefix=project+'-',dir='/run' if args.mode=='helper' else None)).resolve();root.chmod(0o700)
    try:
        app=Harness(root,docker,images,project)
        # One self-signed offline CA with a fixture-only hostname. The real API
        # TLS implementation validates it via NODE_EXTRA_CA_CERTS.
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=upstream',
            '-addext','subjectAltName=DNS:upstream','-keyout',str(app.fixture/'key.pem'),'-out',str(app.fixture/'ca.pem')],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20,check=True)
        (app.fixture/'key.pem').chmod(0o644);(app.fixture/'ca.pem').chmod(0o644)
        config=json.loads(app.command(app.argv()+['config','--format','json']))
        require(all(n.get('internal') for n in config['networks'].values()) and
                all(not s.get('ports') and not s.get('privileged') for s in config['services'].values()),'OFFLINE_STACK_REQUIRED')
        started=True
        app.command(app.argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','90','postgres','redis','client','upstream'],timeout=120)
        require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n);")==0,'FRESH_DATABASE_REQUIRED')
        app.node("require('child_process').execFileSync(process.execPath,['/app/node_modules/prisma/build/index.js','db','push','--skip-generate'],{stdio:'inherit'})",timeout=120)
        report['admissionRuntime']=app.prepare_admission()
        classroom=app.db("await p.user.create({data:{id:'teacher',email:'teacher@example.invalid',role:'ADMIN'}});const c=await p.classroom.create({data:{userId:'teacher',name:'synthetic shutdown class',subject:'数学'}});console.log(JSON.stringify(c.id));")
        version=json.loads(app.node("console.log(JSON.stringify(require('/app/package.json').version))"))
        require(version==args.version,'API_PACKAGE_VERSION_MISMATCH')
        app.command(app.argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','120','api'],timeout=150)
        configured=app.request('http://api:4999/system-model-configs/asr',dict(config=dict(provider='doubao_streaming_2',endpoint='wss://upstream:4987/api/v3/sauc/bigmodel_async',apiKey='offline-fixture-only',resourceId='test-resource')),'PUT')
        require(configured['status']==200,'ASR_FIXTURE_CONFIGURATION_FAILED')
        if args.mode=='helper':helper=Helper(app,args.helper_source)
        for failure in (False,True):
            app.request('http://upstream:4986/mode/hold')
            request_id=str(uuid.uuid4());run_id=app.run(classroom,request_id)
            wait_for(lambda:app.stats()['planActive']==1,'PLANNER_NOT_ACTIVE')
            before=app.read(run_id);require(before['leaseToken'] and not before['cancelRequested'],'COORDINATOR_LEASE_MISSING')
            initial_requests=app.stats()['planRequests']
            api=app.inspect()['api'];app.verify_api(api)
            report['checks']['original_entrypoint']=True
            stable={name:item['Id'] for name,item in app.inspect().items() if name!='api'}
            if not failure:
                require(app.request('http://client:4988/start')['status']==200,'TRANSPORT_CLIENT_START_FAILED')
                wait_for(lambda:(lambda c:c['sseReady'] and c['wsReady'] and c['httpStarted'])(app.client()) and
                         (lambda s:s['asrActive']==1 and s['chatActive']==1 and s['planActive']==1)(app.stats()),'TRANSPORTS_NOT_CONCURRENT',35)
                report['checks']['transport_coexistence']=True
            else:
                paused=app.inspect()['postgres']['Id'];app.command(docker+['pause',paused])
            if helper:
                timing=helper.stop(api,failure)
            else:
                began=time.monotonic();app.command(docker+['stop','--time','30',api['Id']],timeout=40)
                timing=dict(seconds=round(time.monotonic()-began,3))
            stopped=json.loads(app.command(docker+['inspect',api['Id']]))[0]
            expected=1 if failure else 0
            require(not stopped['State']['Running'] and stopped['State']['ExitCode']==expected and not stopped['State'].get('OOMKilled') and
                    stopped['Id']==api['Id'] and stopped['Image']==api['Image'] and stopped['State']['StartedAt']==api['State']['StartedAt'],'API_EXIT_IDENTITY_MISMATCH')
            timing.update(databaseOutage=failure,exitCode=expected)
            require(timing['seconds']<28,'API_SHUTDOWN_NOT_BOUNDED')
            report['timings'].append(timing)
            if paused:app.command(docker+['unpause',paused]);paused=None
            after=app.read(run_id)
            require(after['status']=='running' and not after['cancelRequested'] and bool(after['leaseToken'])==failure,'DURABLE_SHUTDOWN_STATE_MISMATCH')
            require(app.stats()['planActive']==0,'PLANNER_CONNECTION_NOT_ABORTED')
            if not failure:
                final_client=wait_for(lambda:(lambda c:c if c['sseEnded'] and c['wsClosed'] and c['httpInterrupted'] else None)(app.client()),'TRANSPORTS_NOT_CLOSED',10)
                require(final_client['wsCode']==1012 and 'event: service_restarting' in final_client['sseText'],'RESTART_TRANSPORT_PROTOCOL_MISMATCH')
                wait_for(lambda:app.stats()['asrActive']==0,'ASR_UPSTREAM_NOT_RELEASED',10)
                report['checks']['graceful_exit']=True
                report['transportResult']={k:final_client[k] for k in ('sseEnded','wsClosed','wsCode','httpInterrupted')}
            else:report['checks']['business_failure_nonzero']=True
            app.request('http://upstream:4986/mode/clarify')
            app.command(app.argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','120','api'],timeout=150)
            final=wait_for(lambda:(lambda r:r if r['status']=='waiting_input' else None)(app.read(run_id)),'RESTART_NOT_RESUMED',80)
            require(final['operationIds']==before['operationIds'] and final['attempts']==[2] and not final['leaseToken'] and
                    app.stats()['planRequests']==initial_requests+1 and app.run(classroom,request_id)==run_id,'RESTART_DUPLICATED_WORK')
            require({name:item['Id'] for name,item in app.inspect().items() if name!='api'}==stable,'UNRELATED_FIXTURE_CONTAINER_REPLACED')
            report.setdefault('restartResults',[]).append(dict(databaseOutage=failure,sameOperation=True,attempts=final['attempts'],idempotentReplay=True))
            report['checks']['restart_recovery']=True
            print(json.dumps(dict(check='business-failure-and-restart' if failure else 'concurrent-transports-and-clean-stop',status='passed')),flush=True)
        require(app.db("console.log(await p.aiJob.count());")==0,'UNEXPECTED_DOMAIN_JOB')
        report['status']='passed'
        if helper:
            report['helperAdaptations']=helper.substitutions
            report['adaptedHelperSha256']=helper.adapted_sha256
    except BaseException as error:
        report['status']='failed';report['error']=str(error) if isinstance(error,ValueError) else type(error).__name__
        if app and getattr(app,'last_error',None):report['commandDiagnostic']=app.last_error
        if app and started:
            try:
                report['fixtureDiagnostics']=app.redact(app.command(app.argv()+['logs','--no-color','--tail','35','api','upstream','client'],timeout=10).decode(errors='replace')[-12000:])
            except Exception:pass
    finally:
        # Ignore additional cancellation signals while performing the bounded
        # cleanup; the caller gives this verifier its own cleanup grace period.
        for sig in old_handlers:signal.signal(sig,signal.SIG_IGN)
        cleanup_errors=[]
        if app:
            app.cleanup_deadline=time.monotonic()+75
            if paused:
                try:app.command(docker+['unpause',paused],timeout=10)
                except Exception:cleanup_errors.append('postgres_unpause')
            if helper:
                try:helper.cleanup()
                except Exception:cleanup_errors.append('helper_cleanup')
            if started:
                try:app.command(app.argv()+['down','--volumes','--remove-orphans','--timeout','5'],timeout=45)
                except Exception:cleanup_errors.append('compose_cleanup')
            try:
                remaining=app.command(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip()
                networks=app.command(docker+['network','ls','-q','--filter','label=com.docker.compose.project='+project]).strip()
                volumes=app.command(docker+['volume','ls','-q','--filter','label=com.docker.compose.project='+project]).strip()
                report['checks']['resources_removed']=not remaining and not networks and not volumes and not cleanup_errors
            except Exception:cleanup_errors.append('cleanup_inspection')
        if cleanup_errors or not report['checks'].get('resources_removed'):
            report['status']='failed';report['cleanupErrors']=cleanup_errors
            report['cleanupScope']=dict(project=project,root=str(root),helperTag=helper.tag if helper else None)
        else:shutil.rmtree(root)
        report['verifiedAt']=dt.datetime.now(dt.timezone.utc).isoformat()
        atomic(args.output,report)
        for sig,handler in old_handlers.items():signal.signal(sig,handler)
    print(json.dumps(dict(status=report['status'],checks=report['checks'],timings=report['timings'],error=report.get('error'))),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    sys.exit(main())
