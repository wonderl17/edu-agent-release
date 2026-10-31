"""Real Worker image, BullMQ and database lifecycle, with local model HTTP fixtures.

No provider access, existing database, exposed ports, migration or deployment.
Exercises homework proposals and legacy slide edits in the two production Worker
roles. Does not claim initial full-deck generation or all domain Jobs are covered.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from verify_codex_application import Application, load, require


MODEL = r"""
const http=require('http');let hold=true,requests=[],pending=new Set();
http.createServer((req,res)=>{
 if(req.url==='/stats'){res.setHeader('content-type','application/json');res.end(JSON.stringify({requests,active:pending.size}));return}
 if(req.url==='/hold'){hold=true;res.end('{}');return}
 if(req.url==='/release'){hold=false;for(const finish of [...pending])finish();res.end('{}');return}
 if(req.url!=='/v1/chat/completions'){res.writeHead(404);res.end('{}');return}
 let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{
  const body=JSON.parse(raw),input=JSON.parse(body.messages.at(-1).content);
  if(body.model!=='gpt-6-luna'||!/^synthetic-/.test(input.prompt)){res.writeHead(400);res.end('{}');return}
  requests.push(input.prompt);
  const output=input.slide?{slide:{...input.slide,elements:[]},title:input.prompt,remark:'synthetic saved remark'}:
    {studentMarkdown:'1. '+input.prompt,teacherMarkdown:'1. '+input.prompt+' answer'};
  const finish=()=>{pending.delete(finish);if(res.destroyed)return;res.setHeader('content-type','application/json');res.end(JSON.stringify({
    id:'offline-fixture',object:'chat.completion',model:body.model,choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:JSON.stringify(output)}}],
    usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}}));};
  pending.add(finish);res.once('close',()=>pending.delete(finish));if(!hold)finish();
 });
}).listen(4988,'0.0.0.0');
"""


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--api-image',required=True)
    parser.add_argument('--runner-image',required=True)
    parser.add_argument('--worker-image',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    for name,value in [('api',args.api_image),('runtime',args.runner_image),('worker',args.worker_image)]:
        require(re.fullmatch(r'teaching-codex-'+name+r':[a-z0-9-]+',value),'LOCAL_IMAGE_REQUIRED')
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED')
    docker=['docker','--host',endpoint]
    images={key:json.loads(subprocess.check_output(docker+['image','inspect',tag]))[0]for key,tag in
            dict(api=args.api_image,runner=args.runner_image,worker=args.worker_image,postgres='postgres:17-alpine',redis='redis:7.4-alpine').items()}
    require(all(image['Os']=='linux'for image in images.values()),'LINUX_REQUIRED')
    require(all(images[key]['Architecture']=='amd64'for key in ('api','runner','worker')),'AMD64_REQUIRED')
    configure,policy=load('codex_configure'),load('codex_runtime')
    receiver=load('tencent_receiver')
    project='codex-workers-'+uuid.uuid4().hex[:12]
    configure.PROJECT=policy.PROJECT=project
    require(not subprocess.check_output(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip(),'PROJECT_EXISTS')
    report=dict(status='incomplete',checks=[],project=project,workerImage=images['worker']['Id'],apiImage=images['api']['Id'],
                modelCalls=0,productionChanged=False,scope='original Worker image entrypoint, real PostgreSQL/Redis/BullMQ, deterministic local HTTP generation',
                limitations=['synthetic homework proposals and legacy slide edits only; not initial lesson/full-deck generation',
                             'API image is used as a DB/queue control fixture; no assistant SDK planning in this suite',
                             'fresh schema push; not production migration, release activation or old-package recovery'])
    report['scriptSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['receiverSha256']=hashlib.sha256((Path(__file__).resolve().parents[1]/'server/tencent_receiver.py').read_bytes()).hexdigest()
    def passed(name):
        report['checks'].append(name);print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-workers-') as temporary:
        root=Path(temporary).resolve();root.chmod(0o700)
        app=Application(root,docker,images,project,policy,configure)
        base=json.loads(app.base_path.read_bytes())
        for role in ('general','ppt'):
            env=dict(base['services']['api']['environment'],AI_WORKER_ENABLED='true',AI_WORKER_ROLE=role,AI_JOB_WORKER_CONCURRENCY='1',
                     AI_JOB_LOCK_DURATION_MS='60000')
            base['services']['worker-'+role]=dict(image=images['worker']['Id'],platform='linux/amd64',networks={'app':None},
                                                environment=env,healthcheck={'disable':True})
        fixture=root/'model.cjs';fixture.write_text(MODEL);fixture.chmod(0o644)
        base['services']['model']=dict(image=images['api']['Id'],platform='linux/amd64',entrypoint=['node'],command=['/fixture/model.cjs'],
                                     networks={'app':None},volumes=[str(fixture)+':/fixture/model.cjs:ro'],healthcheck={'disable':True})
        app.atomic(app.base_path,base)
        receiver.DOCKER=docker;receiver.command=app.command
        services=['worker-general','worker-ppt']
        def model(path='/stats'):
            return json.loads(app.node("fetch('http://model:4988"+path+"').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))",'web'))
        def wait_for(check,code,seconds=60):
            until=time.monotonic()+seconds
            while time.monotonic()<until:
                value=check()
                if value:return value
                time.sleep(.5)
            raise ValueError(code)
        def workers():
            return {name:row for name,row in app.inspect().items()if name in services}
        def state():
            ids=[workers()[name]['Id']for name in services]
            return json.loads(app.command(docker+['inspect']+ids))
        def start():
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never']+services,timeout=60)
        def queue_code(code):
            return "const api=require('module').createRequire('/app/apps/api/package.json');const{Queue}=api('bullmq');const{Redis}=api('ioredis');const redis=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});const qs={general:new Queue('teaching-ai-agent-ai-jobs',{connection:redis}),ppt:new Queue('teaching-ai-agent-ppt-jobs',{connection:redis})};try{"+code+"}finally{await Promise.all(Object.values(qs).map(q=>q.close()));await redis.quit();}"
        def seed(label):
            return app.db("const label="+json.dumps(label)+";const output=[];"+queue_code(r"""
for(const role of ['general','ppt']){
 const prompt='synthetic-'+label+'-'+role;
 let resource,lesson,payload,type;
 if(role==='general'){
  resource=await p.homework.create({data:{userId:'teacher',questionsJson:{},answersJson:{},displayStudent:'1. original',displayAnswer:'1. original answer'}});
  payload={prompt,target:'both',reviewMode:'proposal',clientRequestId:crypto.randomUUID(),baseUpdatedAt:resource.updatedAt.toISOString()};type='HOMEWORK_ADJUST';
 }else{
  lesson=await p.lessonPlan.create({data:{userId:'teacher',topic:prompt,subject:'数学',gradeLevel:'初一',objectives:'计算',structuredContent:{},status:'READY'}});
  resource=await p.pptProject.create({data:{userId:'teacher',lessonPlanId:lesson.id,configJson:{},slidesJson:{version:1,slides:[{id:'slide',viewportSize:1000,viewportRatio:16/9,elements:[]}],slideScenes:[{title:'original',actions:[]}]}}});
  payload={prompt,slideIndex:0};type='PPT_SLIDE_ADJUST';
 }
 const job=await p.aiJob.create({data:{userId:'teacher',type,payload,relatedEntityId:resource.id}});
 const data={aiJobId:job.id,userId:'teacher',lessonPlanId:lesson?.id??null,homeworkId:role==='general'?resource.id:undefined,pptProjectId:role==='ppt'?resource.id:undefined,
  llmOverrides:{provider:'openai-compatible',model:'gpt-6-luna',apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'}};
 await qs[role].add(type,data,{jobId:job.id,attempts:2,backoff:{type:'fixed',delay:1000},removeOnComplete:false,removeOnFail:false});
 output.push({id:job.id,role,resourceId:resource.id,prompt});
}
console.log(JSON.stringify(output));
"""),'web')
        def read(jobs):
            return app.db("const jobs="+json.dumps(jobs)+";const output=[];"+queue_code(r"""
for(const item of jobs){
 const db=await p.aiJob.findUniqueOrThrow({where:{id:item.id}}),q=await qs[item.role].getJob(item.id);
 const proposals=await p.contentRevisionProposal.count({where:{sourceJobId:item.id}});
 const resource=item.role==='ppt'?await p.pptProject.findUniqueOrThrow({where:{id:item.resourceId}}):await p.homework.findUniqueOrThrow({where:{id:item.resourceId}});
 output.push({...item,status:db.status,error:db.errorMessage,queueState:await q.getState(),attemptsStarted:q.attemptsStarted,stalledCounter:q.stalledCounter,
  proposals,resource:item.role==='ppt'?resource.slidesJson:resource.displayStudent});
}
console.log(JSON.stringify(output));
"""),'web')
        def complete(jobs):
            rows=read(jobs)
            require(not any(row['status']in ('FAILED','INTERRUPTED','CANCEL_REQUESTED')for row in rows),'JOB_FAILED_OR_CANCELLED')
            if all(not row['State']['Running']for row in state()):
                require(all(row['status']=='SUCCESS'for row in rows),'GENERATION_NOT_SAVED_BEFORE_CLEAN_EXIT')
            return rows if all(row['status']=='SUCCESS'and row['queueState']=='completed'for row in rows)else None
        def assert_saved(jobs):
            rows=wait_for(lambda:complete(jobs),'GENERATION_NOT_SAVED',150)
            for row in rows:
                if row['role']=='general':require(row['proposals']==1 and row['resource']=='1. original','PROPOSAL_DUPLICATED_OR_SOURCE_CHANGED')
                else:require(row['resource']['slideScenes'][0]['title']==row['prompt'],'SLIDE_NOT_SAVED')
            return rows
        started=False
        try:
            started=True
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60','postgres','redis','web','model'],timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS count FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].count);",'web')==0,'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','db','push','--skip-generate'],{stdio:'inherit'});",'web',timeout=120)
            app.db("await p.user.create({data:{id:'teacher',email:'synthetic-worker@example.invalid',role:'ADMIN'}});console.log('true');",'web')
            initial=seed('drain');start()
            wait_for(lambda:model()['active']==2,'BOTH_WORKERS_NOT_GENERATING')
            report['workerSourceHashes']=json.loads(app.node("const fs=require('fs'),crypto=require('crypto');const paths=['src/ai-jobs-worker.ts','src/lib/worker-shutdown.ts','src/queue/ai-jobs-worker-factory.ts','src/queue/lesson-generation-dispatch.ts','src/modules/student/student-profile.phone-recording-staging.ts','src/modules/feedback/feedback.sync.ts'];console.log(JSON.stringify(Object.fromEntries(paths.map(path=>[path,crypto.createHash('sha256').update(fs.readFileSync('/app/apps/api/'+path)).digest('hex')]))));",'worker-general'))
            ids=[workers()[name]['Id']for name in services]
            app.command(docker+['kill','--signal','SIGTERM']+ids)
            # The process logs only after its installed signal handler is called.
            wait_for(lambda:all(b'SIGTERM, closing' in app.command(docker+['logs',identity])for identity in ids),'SIGNAL_NOT_HANDLED')
            queued=seed('queued-after-stop')
            model('/release')
            wait_for(lambda:all(not row['State']['Running']for row in state()),'WORKERS_NOT_STOPPED')
            require(all(row['State']['ExitCode']==0 for row in state()),'CLEAN_DRAIN_FAILED')
            assert_saved(initial)
            require(all(row['status']=='PENDING'and row['queueState']=='waiting'for row in read(queued)),'WORK_TAKEN_AFTER_STOP')
            require(len(model()['requests'])==2,'EXTRA_GENERATION_DURING_DRAIN')
            passed('both-worker-roles-close-consumption-and-save-in-flight-generation-before-exit-zero')
            start();assert_saved(queued)
            require(len(model()['requests'])==4,'RESTART_REPLAYED_FINISHED_GENERATION')
            passed('restart-processes-queued-work-without-replaying-saved-artifacts')
            model('/hold');interrupted=seed('timeout')
            wait_for(lambda:model()['active']==2,'TIMEOUT_JOBS_NOT_GENERATING')
            before=workers();began=time.monotonic()
            try:receiver.stop_workers_for_release(before['worker-general'],before['worker-ppt'])
            except receiver.Refused as error:require(str(error)=='WORKER_SHUTDOWN_INCOMPLETE','WRONG_STOP_FAILURE')
            else:raise ValueError('RELEASE_ACCEPTED_INCOMPLETE_DRAIN')
            report['timeoutSeconds']=round(time.monotonic()-began,3)
            require(all(row['State']['ExitCode']==1 and not row['State']['OOMKilled']for row in state()),'TIMEOUT_NOT_EXPLICIT')
            require(all(row['status']=='PROCESSING'and row['queueState']=='active'for row in read(interrupted)),'TIMEOUT_CHANGED_USER_INTENT')
            passed('generation-deadline-exits-nonzero-and-release-refuses-before-migration')
            model('/release');start()
            resumed=assert_saved(interrupted)
            require(all(row['attemptsStarted']==2 and row['stalledCounter']==1 for row in resumed),'STALLED_RECOVERY_NOT_VERIFIED')
            require(len(model()['requests'])==8,'UNEXPECTED_GENERATION_REPLAY_COUNT')
            report['fixtureGenerationRequests']=len(model()['requests'])
            report['recoveredJobs']=resumed
            passed('real-bullmq-lock-expiry-resumes-same-jobs-and-saves-once-in-both-roles')
            before=workers();receiver.stop_workers_for_release(before['worker-general'],before['worker-ppt'])
            passed('release-stop-accepts-both-actual-clean-worker-exits')
            report['status']='passed'
        except BaseException as error:
            report['status']='failed';report['error']=configure.error_code(error);report['errorType']=type(error).__name__
            if getattr(app,'last_error',None):report['commandDiagnostic']=app.last_error
            # Only this isolated synthetic stack. Container environment is never logged.
            for name,item in workers().items():
                output=subprocess.run(docker+['logs',item['Id']],capture_output=True,timeout=15)
                report.setdefault('workerLogs',{})[name]=(output.stdout+output.stderr).decode(errors='replace')[-12000:]
            raise
        finally:
            if started:app.command(app.compose_argv()+['down','--volumes','--remove-orphans','--timeout','45'],timeout=110)
            report['testResourcesRemoved']=not app.command(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip()and not app.command(docker+['network','ls','-q','--filter','label=com.docker.compose.project='+project]).strip()
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({key:value for key,value in report.items()if key!='recoveredJobs'}),flush=True)


if __name__=='__main__':main()
