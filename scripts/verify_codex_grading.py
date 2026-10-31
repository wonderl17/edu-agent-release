"""Original grading Worker with source images, real PG/BullMQ and private MinIO.
Synthetic HTTP model only; no provider, production mutation or image substitution.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid
from verify_codex_application import Application, load, require
from verify_codex_package_recovery import cleanup

MODEL = r'''
const http=require('http'),responses=require('/fixture/responses.json');
let hold='conclusion',requests=[],unknown=[],pending=new Set();
http.createServer((req,res)=>{
 if(req.url==='/stats'){res.end(JSON.stringify({requests,unknown,active:pending.size}));return;}
 if(req.url==='/next'||req.url==='/next-grading'){if(pending.size){res.writeHead(409);res.end('{}');return;}hold=req.url==='/next'?'conclusion':'grading';res.end('{}');return;}
 if(req.url==='/release'){hold=null;for(const finish of [...pending])finish();res.end('{}');return;}
 let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{try{
  const b=JSON.parse(raw),system=b.messages?.[0]?.content;
  if(req.url!=='/v1/chat/completions'||b.model!=='gpt-6-luna'||typeof system!=='string')throw Error('Unexpected endpoint or model');
  let kind;
  if(system.includes('答卷归属检查员'))kind='identity';
  else if(system.includes('根据原始材料整理完整、可执行的批改规则'))kind='teacher';
  else if(system.includes('参考答案原图摘录员'))kind='answer';
  else if(system.includes('学生答卷逐页精确定位员'))kind='locate';
  else if(system.includes('学生作答区域局部精定位员'))kind='fine';
  else if(system.includes('老师端答卷画像员'))kind='profile';
  else if(system.includes('老师端学生作答区域定位员'))kind='regions';
  else if(system.includes('老师端学生作答摘录员'))kind='excerpt';
  else if(system.includes('老师端学生答卷读取员'))kind='fast';
  else if(system.includes('一次完成可直接落库的逐题判分'))kind='grading';
  else if(system.includes('正在给家长写可以直接转发的学习反馈'))kind='conclusion';
  else if(system.includes('标题')&&b.response_format?.type==='json_object')kind='title';
  else {unknown.push({url:req.url,system:system.slice(0,1200)});throw Error('Unexpected prompt');}
  if(kind==='fast'){requests.push({kind,model:b.model,fixtureError:'normal-recognition-fallback'});throw Error('Fixture intentionally uses normal recognition path');}
  if(typeof responses[kind]!=='string')throw Error('Missing fixture response '+kind);
  requests.push({kind,model:b.model});
  const finish=()=>{pending.delete(finish);if(res.destroyed)return;if(b.stream){res.setHeader('content-type','text/event-stream');const chunk={id:'synthetic',created:0,object:'chat.completion.chunk',model:b.model,choices:[{index:0,finish_reason:null,delta:{role:'assistant',content:responses[kind]}}]};res.write('data: '+JSON.stringify(chunk)+'\n\n');res.write('data: '+JSON.stringify({...chunk,choices:[{index:0,finish_reason:'stop',delta:{}}],usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}})+'\n\n');res.end('data: [DONE]\n\n');return;}res.setHeader('content-type','application/json');res.end(JSON.stringify({id:'synthetic',created:0,object:'chat.completion',model:b.model,choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:responses[kind]}}],usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}}));};
  pending.add(finish);res.once('close',()=>pending.delete(finish));if(hold!==kind)finish();
 }catch(error){res.writeHead(400);res.end(JSON.stringify({error:{message:String(error)}}));}});
}).listen(4988,'0.0.0.0');
'''


def main():
    parser=argparse.ArgumentParser()
    for name in ('worker-image','runner-image','storage-image'): parser.add_argument('--'+name,required=True)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--scenario',choices=('full','scoring-recovery'),default='full')
    args=parser.parse_args(); require(not args.output.exists(),'DO_NOT_OVERWRITE_PRIOR_PROOF')
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED'); docker=['docker','--host',endpoint]
    tags=dict(api=args.worker_image,worker=args.worker_image,runner=args.runner_image,storage=args.storage_image,postgres='postgres:17-alpine',redis='redis:7.4-alpine')
    images={k:json.loads(subprocess.check_output(docker+['image','inspect',v]))[0] for k,v in tags.items()}
    require(all(v['Os']=='linux' for v in images.values()) and images['worker']['Architecture']=='amd64','LINUX_AMD64_REQUIRED')
    policy,configure=load('codex_runtime'),load('codex_configure'); project='codex-grading-'+uuid.uuid4().hex[:12]
    policy.PROJECT=configure.PROJECT=project
    fixture=Path(__file__).resolve().parent/'fixtures'/'codex-grading'
    report=dict(status='incomplete',project=project,checks=[],images={k:v['Id'] for k,v in images.items()},realModelCalls=0,productionChanged=False,
      scope='Actual raw grading upload service, source bytes, encrypted outbox, PG/BullMQ and original Worker',limitations=['Synthetic model responses; no provider or content quality claim','Not production capacity, HTTP controller or browser verification'])
    report['scenario']=args.scenario
    report['scriptSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();report['fixtureHashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture.iterdir() if p.is_file()}
    def passed(name): report['checks'].append(name);print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-grading-') as directory:
        root=Path(directory).resolve();root.chmod(0o700);app=Application(root,docker,images,project,policy,configure)
        base=json.loads(app.base_path.read_bytes()); env=dict(base['services']['api']['environment'])
        key,secret='synthetic-'+secrets.token_hex(6),secrets.token_hex(24)
        env.update(OBJECT_STORAGE_ENDPOINT='http://storage:19000',OBJECT_STORAGE_REGION='us-east-1',OBJECT_STORAGE_BUCKET='grading-proof',OBJECT_STORAGE_ACCESS_KEY_ID=key,OBJECT_STORAGE_SECRET_ACCESS_KEY=secret,OBJECT_STORAGE_FORCE_PATH_STYLE='true')
        base['services']['web']['environment']=env;base['services']['web']['volumes']=[str(fixture)+':/fixture:ro']
        base['services']['storage']=dict(image=images['storage']['Id'],platform='linux/amd64',networks={'app':None},environment=dict(MINIO_ROOT_USER=key,MINIO_ROOT_PASSWORD=secret),command=['server','/data','--address',':19000'])
        base['services']['worker-general']=dict(image=images['worker']['Id'],platform='linux/amd64',networks={'app':None},environment=dict(env,AI_WORKER_ENABLED='true',AI_WORKER_ROLE='general',API_BACKGROUND_TASKS_ENABLED='false',AI_JOB_WORKER_CONCURRENCY='1',AI_JOB_LOCK_DURATION_MS='60000'),healthcheck={'disable':True})
        path=root/'model.cjs';path.write_text(MODEL);path.chmod(0o644)
        base['services']['model']=dict(image=images['worker']['Id'],platform='linux/amd64',networks={'app':None},entrypoint=['node'],command=['/model.cjs'],volumes=[str(path)+':/model.cjs:ro',str(fixture)+':/fixture:ro'],healthcheck={'disable':True})
        app.atomic(app.base_path,base)
        def wait(check,code,seconds=420):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                result=check()
                if result:return result
                time.sleep(.5)
            raise ValueError(code)
        def model(path='/stats'):return json.loads(app.node("fetch('http://model:4988"+path+"').then(async r=>{if(!r.ok)throw Error('MODEL_CONTROL_FAILED');console.log(JSON.stringify(await r.json()))})",'web'))
        def worker():return app.inspect()['worker-general']
        def start():app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','worker-general'],timeout=60)
        def queue(code):return "const api=require('module').createRequire('/app/apps/api/package.json');const{Queue}=api('bullmq');const{Redis}=api('ioredis');const redis=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});const q=new Queue('teaching-ai-agent-ai-jobs',{connection:redis});try{"+code+"}finally{await q.close();await redis.quit();}"
        def business(code):
            prelude="const api=require('module').createRequire('/app/apps/api/package.json');api('tsx/cjs/api').register();api('tsx/esm/api').register();const load=p=>require('/app/apps/api/src/'+p);const{prisma:p}=load('lib/prisma.ts');"
            raw=app.node(prelude+"(async()=>{"+code+"})().then(async result=>{await p.$disconnect();process.stdout.write('RESULT:'+JSON.stringify(result)+'\\n');process.exit(0)}).catch(e=>{console.error(e);process.exit(1)})",'web',timeout=90).decode()
            return json.loads(next(line[7:] for line in reversed(raw.splitlines()) if line.startswith('RESULT:')))
        identities=None
        def seed(legacy=False):
            return business('const userId='+json.dumps(identities['userId'])+',studentId='+json.dumps(identities['studentId'])+';const legacy='+json.dumps(legacy)+';'+r'''
const requestId=require('crypto').randomUUID(),session=await p.agentSession.create({data:{userId,title:'原始批改',context:{}}});
const run=await p.agentRun.create({data:{userId,sessionId:session.id,clientRequestId:require('crypto').randomUUID(),requestHash:'synthetic',request:'批改原始答卷',context:{},status:'waiting_job',passwordVersion:0,runtimeCipherVersion:1,runtimeCiphertext:Buffer.from('synthetic')}});
await p.agentOperation.create({data:{id:requestId,runId:run.id,stage:'grading',inputHash:'synthetic'}});
const names=['answer-key.png','student-answer.png'],files=names.map(name=>{const buffer=require('fs').readFileSync('/fixture/'+name);return{originalname:name,mimetype:'image/png',buffer,size:buffer.length}});
const metadata={clientRequestId:requestId,localTaskId:requestId,title:'合成方程判分',sourceKind:'EXTERNAL',subject:'数学',educationStage:'junior_middle',imageStorageMode:'r2',pdfParsingMode:'paddle_ocr_text',submissionGroups:[{key:'student-a',displayOrder:1,studentId,sourceFileIds:['student-source'],confirmedByUser:true}],files:files.map((f,i)=>({uploadIndex:i,sourceFileId:i?'student-source':'answer-source',sourceFileName:f.originalname,sourceFileRelativePath:'source/'+f.originalname,role:i?'STUDENT_ANSWER':'ANSWER_KEY',mimeType:f.mimetype,sizeBytes:f.size,...(i?{submissionGroupKey:'student-a',submissionPageIndex:0}:{})}))};
if(legacy)delete metadata.clientRequestId;
const runtime={provider:'openai-compatible',model:'gpt-6-luna',supportsVisionInput:true,apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'};
const accepted=await load('modules/grading/intake/service.ts').importAndRunGradingForUser(userId,metadata,files,runtime,runtime);
await p.agentOperation.update({where:{id:requestId},data:{jobId:accepted.job.id}});
const dispatch=load('queue/lesson-generation-dispatch.ts').dispatchPendingLessonGenerations;
for(let i=0;i<100;i++){await dispatch(accepted.job.id);const d=await p.aiJobDispatch.findUniqueOrThrow({where:{jobId:accepted.job.id}});if(d.dispatchedAt)return{id:accepted.job.id,taskId:accepted.task.id,runId:run.id};await new Promise(r=>setTimeout(r,100));}
throw Error('INITIAL_DISPATCH_NOT_ACKNOWLEDGED');
''')
        def read(job):
            return app.db('const id='+json.dumps(job['id'])+';'+queue(r'''
const db=await p.aiJob.findUniqueOrThrow({where:{id}}),task=await p.gradingTask.findUniqueOrThrow({where:{id:db.relatedEntityId},include:{submissions:{include:{gradingResults:{orderBy:{id:'asc'}},questionScores:{orderBy:{id:'asc'}},conclusionOverview:true}},gradingQuestions:{orderBy:{id:'asc'}}}}),queued=await q.getJob(id),units=await p.aiJobGenerationUnit.findMany({where:{aiJobId:id},orderBy:{unitId:'asc'}}),hash=x=>require('crypto').createHash('sha256').update(JSON.stringify(x)).digest('hex');
console.log(JSON.stringify({status:db.status,error:db.errorMessage,result:db.result,task,units,inputHash:hash(db.payload),scoresHash:hash(task.submissions.map(s=>[s.gradingResults,s.questionScores])),queueState:queued?await queued.getState():null,attemptsStarted:queued?.attemptsStarted,stalledCounter:queued?.stalledCounter}));
'''),'web')
        def partial(job):
            row=read(job);require(row['status'] not in ('FAILED','INTERRUPTED','SUCCESS'),'GRADING_FINISHED_BEFORE_HELD_CONCLUSION')
            if row['status']=='PROCESSING' and model()['active']==1 and model()['requests'][-1]['kind']=='conclusion':
                require(len(row['task']['submissions'])==1 and len(row['task']['submissions'][0]['gradingResults'])==1,'GRADING_NOT_SAVED_BEFORE_CONCLUSION')
                return row
        def complete(job):
            row=read(job);require(row['status'] not in ('FAILED','INTERRUPTED'),'GRADING_FAILED');return row if row['status']=='SUCCESS' and row['queueState']=='completed' else None
        def assert_delivered(row):
            sub=row['task']['submissions'][0]
            require(row['task']['status']=='REVIEW' and sub['processingStatus']=='REVIEW' and sub['conclusionOverview']['resultJson']['status']=='READY','GRADING_DELIVERY_INCOMPLETE')
            require(len(sub['gradingResults'])==1 and sub['gradingResults'][0]['suggestedScore']==0 and sub['gradingResults'][0]['maxScore']==5,'GRADING_SCORE_NOT_SAVED')
            require(row['result']['progress']['percent']==100,'FINAL_PROGRESS_MISSING')
        def cancel(job,target):
            code='await p.agentRun.update({where:{id:'+json.dumps(job['runId'])+'},data:{status:"cancelling",cancelRequested:true}});' if target=='parent' else 'await p.aiJob.update({where:{id:'+json.dumps(job['id'])+'},data:{status:"CANCEL_REQUESTED"}});'
            app.db(code+'console.log("true")','web')
        def interrupted(job):
            row=read(job);return row if row['status']=='INTERRUPTED' and row['queueState']=='completed' else None
        current=None
        try:
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60','postgres','redis','web','model','storage'],timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n)",'web')==0,'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','migrate','deploy'],{stdio:'inherit'})",'web',timeout=120)
            app.node("const{S3Client,CreateBucketCommand}=require('module').createRequire('/app/apps/api/package.json')('@aws-sdk/client-s3');new S3Client({endpoint:process.env.OBJECT_STORAGE_ENDPOINT,region:'us-east-1',forcePathStyle:true,credentials:{accessKeyId:process.env.OBJECT_STORAGE_ACCESS_KEY_ID,secretAccessKey:process.env.OBJECT_STORAGE_SECRET_ACCESS_KEY}}).send(new CreateBucketCommand({Bucket:'grading-proof'})).then(()=>console.log('ready'))",'web')
            identities=app.db("const u=await p.user.create({data:{email:'synthetic-grading@example.invalid'}}),s=await p.student.create({data:{userId:u.id,name:'合成学生'}});console.log(JSON.stringify({userId:u.id,studentId:s.id}))",'web')
            if args.scenario=='full':
                current=initial=seed();start();first=wait(lambda:partial(initial),'INITIAL_GRADING_NOT_PENDING');before=model()['requests'];passed('raw-answer-upload-and-scoring-checkpoints-saved-before-conclusion')
                identity=worker()['Id'];started=time.monotonic();app.command(docker+['kill','--signal','SIGTERM',identity]);wait(lambda:not worker()['State']['Running'],'WORKER_DID_NOT_EXIT',55)
                require(worker()['State']['ExitCode']==1 and not worker()['State']['OOMKilled'],'UNCLEAN_STOP_NOT_REPORTED');report['shutdownSeconds']=round(time.monotonic()-started,3)
                stopped=read(initial);require(stopped['status']=='PROCESSING' and stopped['scoresHash']==first['scoresHash'],'SHUTDOWN_LOST_SCORES')
                model('/release');start();restart_started=time.monotonic();resumed=wait(lambda:complete(initial),'STALLED_GRADING_DID_NOT_RESUME',420);assert_delivered(resumed);report['restartToDeliverySeconds']=round(time.monotonic()-restart_started,3)
                require(resumed['attemptsStarted']==2 and resumed['stalledCounter']==1,'REAL_STALLED_RETRY_NOT_OBSERVED')
                require(resumed['scoresHash']==first['scoresHash'] and resumed['units']==first['units'] and resumed['inputHash']==first['inputHash'],'RESUME_CHANGED_SCORING_OR_CHECKPOINTS')
                require(model()['requests'][len(before):]==[{'kind':'conclusion','model':'gpt-6-luna'}],'RESUME_REPEATED_SCORING')
                passed('stalled-retry-reuses-exact-scores-and-checkpoints-only-retries-conclusion')
                count=len(model()['requests']);duplicate=app.db('const id='+json.dumps(initial['id'])+';'+queue("const old=await q.getJob(id),dup=require('crypto').randomUUID();await q.add(old.name,old.data,{jobId:dup,removeOnComplete:false});console.log(JSON.stringify(dup))"),'web')
                wait(lambda:app.db(queue('const j=await q.getJob('+json.dumps(duplicate)+');console.log(JSON.stringify(await j.getState()))'),'web')=='completed','DUPLICATE_NOT_SETTLED')
                require(read(initial)==resumed and len(model()['requests'])==count,'DUPLICATE_CHANGED_DELIVERY');passed('duplicate-queue-delivery-does-not-regenerate-or-change-scores')
                model('/next');current=active=seed();saved=wait(lambda:partial(active),'ACTIVE_CANCEL_NOT_PENDING');cancel(active,'parent');stopped=wait(lambda:interrupted(active),'PARENT_CANCEL_NOT_HONORED')
                require(stopped['scoresHash']==saved['scoresHash'] and stopped['task']['submissions'][0]['processingStatus']=='CONCLUSION_FAILED' and not stopped['task']['submissions'][0]['conclusionOverview'],'CANCEL_CHANGED_SCORES_OR_DELIVERED_LATE')
                wait(lambda:model()['active']==0,'CANCELLED_REQUEST_ACTIVE');passed('parent-cancel-aborts-conclusion-and-preserves-scores')
                model('/next');current=offline=seed();saved=wait(lambda:partial(offline),'OUTAGE_CASE_NOT_PENDING');app.command(docker+['kill','--signal','SIGKILL',worker()['Id']]);wait(lambda:not worker()['State']['Running'],'WORKER_NOT_KILLED');wait(lambda:model()['active']==0,'KILLED_REQUEST_ACTIVE');count=len(model()['requests']);cancel(offline,'parent');start()
                stopped=wait(lambda:interrupted(offline),'OUTAGE_CANCEL_NOT_SETTLED',420)
                require(stopped['scoresHash']==saved['scoresHash'] and stopped['task']['submissions'][0]['processingStatus']=='CONCLUSION_FAILED' and len(model()['requests'])==count,'OUTAGE_CANCEL_LOST_OR_REGENERATED_SCORES');passed('outage-parent-cancel-settles-submission-without-model-call')
                app.command(docker+['kill','--signal','SIGTERM',worker()['Id']]);wait(lambda:not worker()['State']['Running'],'IDLE_WORKER_DID_NOT_STOP',55)
                current=queued=seed();count=len(model()['requests']);cancel(queued,'job');start();stopped=wait(lambda:interrupted(queued),'QUEUED_CANCEL_NOT_SETTLED')
                require(stopped['task']['submissions'][0]['processingStatus']=='FAILED' and len(model()['requests'])==count,'QUEUED_CANCEL_STARTED_GENERATION');passed('child-cancel-before-first-processing-settles-submission-without-http')
                model('/next');current=clean=seed(legacy=True);saved=wait(lambda:partial(clean),'CLEAN_DRAIN_NOT_PENDING');identity=worker()['Id'];app.command(docker+['kill','--signal','SIGTERM',identity]);wait(lambda:b'SIGTERM, closing' in app.command(docker+['logs',identity]),'SIGNAL_NOT_HANDLED');model('/release');wait(lambda:not worker()['State']['Running'],'CLEAN_DRAIN_DID_NOT_EXIT',55)
                require(worker()['State']['ExitCode']==0,'CLEAN_DRAIN_FAILED');delivered=wait(lambda:complete(clean),'CLEAN_DRAIN_DID_NOT_DELIVER');assert_delivered(delivered)
                require(delivered['scoresHash']==saved['scoresHash'],'DRAIN_CHANGED_SCORES');passed('graceful-drain-atomically-delivers-conclusion-and-success')
            start();model('/next-grading');current=unscored=seed();
            def before_publication():
                row=read(unscored);require(row['status'] not in ('FAILED','INTERRUPTED','SUCCESS'),'GRADING_FINISHED_BEFORE_HELD_SCORING')
                stats=model()
                if stats['active']==1 and stats['requests'][-1]['kind']=='grading':
                    require(not row['task']['submissions'][0]['gradingResults'] and row['units'],'RECOGNITION_CHECKPOINTS_NOT_SAVED')
                    return row
            saved=wait(before_publication,'SCORING_NOT_PENDING');report['beforeScoringRecovery']=saved;before=model()['requests'];app.command(docker+['kill','--signal','SIGKILL',worker()['Id']]);wait(lambda:not worker()['State']['Running'],'SCORING_WORKER_NOT_KILLED');wait(lambda:model()['active']==0,'SCORING_REQUEST_NOT_CLOSED');model('/release');start();delivered=wait(lambda:complete(unscored),'SCORING_DID_NOT_RESUME');assert_delivered(delivered)
            report['afterScoringRecovery']=delivered
            prior={u['unitId']:u for u in saved['units']};after={u['unitId']:u for u in delivered['units']}
            require(all(after.get(k)==v for k,v in prior.items()),'RECOGNITION_CHECKPOINTS_CHANGED')
            require(model()['requests'][len(before):]==[{'kind':'grading','model':'gpt-6-luna'},{'kind':'conclusion','model':'gpt-6-luna'}],'RESUME_REPEATED_RECOGNITION')
            require(delivered['attemptsStarted']==2 and delivered['stalledCounter']==1,'SCORING_STALLED_RETRY_NOT_OBSERVED');passed('kill-before-scoring-publication-reuses-recognition-checkpoints')
            require(not model()['unknown'],'UNEXPECTED_MODEL_REQUESTS');report['fixtureRequests']=model()['requests'];report['status']='passed'
        except BaseException as error:
            report.update(status='failed',error=configure.error_code(error),errorType=type(error).__name__)
            if getattr(app,'last_error',None):report['commandDiagnostic']=app.last_error
            try:
                report['modelStats']=model();report['jobState']=read(current) if current else None;report['workerLogs']=subprocess.check_output(docker+['logs',worker()['Id']],stderr=subprocess.STDOUT,timeout=15).decode(errors='replace')[-30000:]
            except Exception:pass
            raise
        finally:
            try:cleanup(app,docker,project);report['testResourcesRemoved']=True
            except Exception as error:report['testResourcesRemoved']=False;report['cleanupError']=configure.error_code(error);raise
            finally:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],checks=report['checks'],realModelCalls=0,productionChanged=False)),flush=True)

if __name__=='__main__':main()
