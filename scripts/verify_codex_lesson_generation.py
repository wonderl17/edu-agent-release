"""Initial lesson generation: original Worker + real queue/database, local HTTP fixtures.

No production access or provider calls. Exercises default single-stage lesson and
handout generation, advisory review, durable delivery and actual stalled retry.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from verify_codex_application import Application, load, require
from verify_codex_package_recovery import cleanup

MODEL = r'''
const http=require('http');let hold=true,plans=0,batches=0,modules=0,examples=0,practices=0,reviews=0,patches=0,requests=[],unknown=[],pending=new Set();
const plan={objectives:['理解等式性质','能正确解简单方程'],keyPoints:['等式两边同时加减相同的数'],difficulties:['保持两边相等'],
 textbookAnalysis:'本节复习等式的基本性质。'.repeat(35),studentAnalysis:'学生已经掌握基本加减运算。'.repeat(35),teachingMethods:['练习法'],
 teachingFlow:['复习导入','概念讲解','典型例题','分层练习','课堂小结','作业安排'].map((title,i)=>({title,content:
 '**教师活动**\n'+('比较方程两边的量，说明同时减去相同的数仍然相等。'.repeat(55))+
 '\n**学生活动与预设回答**\n学生观察并回答。例题：x+3=8，解得x=5。分层练习：基础练习、提高练习、拓展练习。\n**设计意图**\n检查对等式性质的理解。'})),
 activities:['比较两个等式','独立解题'],homeworkSuggestion:'巩固等式性质',homeworkRequired:'完成一道基础题',homeworkOptional:'解释为什么保持相等',blackboardDesign:'等式两边同时减去同一个数',teachingReflection:'根据学生解释调整讲评'};
const question=(n,role)=>({title:'解方程',difficulty:1,question:`解方程 $x+3=${n+3}$。`,knowledgeTags:['等式性质'],answer:String(n),analysis:`等式两边同时减去3，得到x=${n}。`,
 teachingBlueprint:{objective:'会用等式性质解方程',role,prerequisite:'整数加减',expectedEvidence:'写出两边同时减去3',difficultyRationale:'一步运算'},
 teacherGuidance:{anticipatedError:'可能只在一边减去3',explanationFocus:'两边同时减去相同的数',followUpQuestion:'为什么两边仍然相等？',checkForUnderstanding:'能说明两边减去相同的数'}});
const handout={version:1,objectives:['会解一步方程','能说明等式性质'],modules:[{title:'等式性质',knowledgeBlocks:[{title:'保持相等',content:'等式两边同时减去相同的数，等式仍然成立。',teacherSupplement:'强调两边同时操作。'}],examples:[question(5,'worked_example')]}],practice:[question(8,'practice')]};
http.createServer((req,res)=>{
 if(req.url==='/stats'){res.end(JSON.stringify({plans,batches,modules,examples,practices,reviews,patches,requests,unknown,active:pending.size}));return}
 if(req.url==='/next'){if(pending.size){res.writeHead(409);res.end('{}');return}hold=true;plans=batches=modules=examples=practices=reviews=patches=0;res.end('{}');return}
 if(req.url==='/release'){hold=false;for(const finish of [...pending])finish();res.end('{}');return}
 if(req.url!=='/v1/chat/completions'){res.writeHead(404);res.end('{}');return}
 let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{
  try{
   const body=JSON.parse(raw);if(body.model!=='gpt-6-luna')throw new Error('Unexpected model');
   const system=String(body.messages.find(m=>m.role==='system')?.content??'');const user=String(body.messages.at(-1).content);
   let kind,output;
   if(system.includes('expert instructional designer')){kind='plan';plans++;output=plan}
   else if(system.includes('负责局部修复一份已经生成的结构化教案')){kind='plan-patch';patches++;output={}}
   else if(system.includes('一次性生成一份紧凑但可直接使用的课堂讲义候选稿')){kind='handout';batches++;output=handout}
   else if(system.includes('请根据讲义蓝图中的单个模块')){kind='module';modules++;output={knowledgeBlocks:handout.modules[0].knowledgeBlocks}}
   else if(system.includes('请根据题目蓝图生成一道完整讲义题')){const practice=user.includes('请根据以下综合演练蓝图');kind=practice?'practice':'example';if(practice)practices++;else examples++;output=question(practice?8:5,practice?'practice':'worked_example')}
   else if(system.includes('你是独立解题教师')){kind='premise';reviews++;const input=JSON.parse(user);output={records:input.targets.map(t=>({target:t.target,premiseAssessment:'题面给出一步方程或等式性质，条件完整且可直接计算。',independentAnswer:t.content.question?.includes('11')?'8':t.content.question?'5':'等式两边同时减去相同的数仍相等。',evidence:[{field:t.content.question?'question':'content',quote:t.content.question??t.content.content}]}))}}
   else if(system.includes('你是讲义内容语义复核员')||system.includes('你是学科讲义复核教师')){kind='semantic';reviews++;output={reviewedTargets:[...new Set([...user.matchAll(/"target"\s*:\s*"([^"\n]+)"/g)].map(m=>m[1]))],numericalChecks:[],issues:[]}}
   else{unknown.push(system.slice(0,250));throw new Error('Unsupported fixture request')}
   requests.push({type:kind,model:body.model});
   const finish=()=>{pending.delete(finish);if(res.destroyed)return;res.setHeader('content-type','application/json');res.end(JSON.stringify({id:'synthetic',object:'chat.completion',model:body.model,
    choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:JSON.stringify(output)}}],usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}}));};
   pending.add(finish);res.once('close',()=>pending.delete(finish));if(!hold||kind!=='example')finish();
  }catch(error){res.writeHead(400);res.end(JSON.stringify({error:{message:String(error)}}));}
 });
}).listen(4988,'0.0.0.0');
'''


def main():
    parser=argparse.ArgumentParser()
    for name in ('api-image','runner-image','worker-image'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED');docker=['docker','--host',endpoint]
    tags=dict(api=args.api_image,runner=args.runner_image,worker=args.worker_image,postgres='postgres:17-alpine',redis='redis:7.4-alpine')
    images={key:json.loads(subprocess.check_output(docker+['image','inspect',tag]))[0]for key,tag in tags.items()}
    require(all(x['Os']=='linux'for x in images.values()),'LINUX_REQUIRED')
    require(all(images[key]['Architecture']=='amd64'for key in ('api','runner','worker')),'AMD64_REQUIRED')
    policy,configure=load('codex_runtime'),load('codex_configure');project='codex-lesson-generation-'+uuid.uuid4().hex[:12]
    policy.PROJECT=configure.PROJECT=project
    report=dict(status='incomplete',checks=[],project=project,images={key:x['Id']for key,x in images.items()},realModelCalls=0,productionChanged=False,
      scope='initial standalone lesson, original Worker entrypoint, real BullMQ/PostgreSQL; deterministic HTTP model fixture',
      limitations=['No provider inference, content quality, SDK planning, series/attachments, other domain Jobs, production capacity or deployment claim'])
    report['scriptSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    def passed(name):report['checks'].append(name);print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-lesson-generation-')as directory:
        root=Path(directory).resolve();root.chmod(0o700);app=Application(root,docker,images,project,policy,configure)
        base=json.loads(app.base_path.read_bytes());env=dict(base['services']['api']['environment'])
        worker_env=dict(env,AI_WORKER_ENABLED='true',AI_WORKER_ROLE='general',API_BACKGROUND_TASKS_ENABLED='false',AI_JOB_WORKER_CONCURRENCY='1',AI_JOB_LOCK_DURATION_MS='60000')
        base['services']['worker-general']=dict(image=images['worker']['Id'],platform='linux/amd64',networks={'app':None},environment=worker_env,healthcheck={'disable':True})
        path=root/'model.cjs';path.write_text(MODEL);path.chmod(0o644)
        base['services']['model']=dict(image=images['api']['Id'],platform='linux/amd64',networks={'app':None},entrypoint=['node'],command=['/fixture/model.cjs'],volumes=[str(path)+':/fixture/model.cjs:ro'],healthcheck={'disable':True})
        app.atomic(app.base_path,base)
        def wait(check,code,seconds=90):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                result=check()
                if result:return result
                time.sleep(.5)
            raise ValueError(code)
        def model(path='/stats'):return json.loads(app.node("fetch('http://model:4988"+path+"').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))",'web'))
        def worker():return app.inspect()['worker-general']
        def start():app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','worker-general'],timeout=60)
        def queue(code):return "const api=require('module').createRequire('/app/apps/api/package.json');const{Queue}=api('bullmq');const{Redis}=api('ioredis');const redis=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});const q=new Queue('teaching-ai-agent-ai-jobs',{connection:redis});try{"+code+"}finally{await q.close();await redis.quit();}"
        def seed():return app.db(queue(r'''
const shared=api('tsx/cjs/api').require('/app/packages/shared/src/generation-requirements.ts','/app/control.cjs');
const prompt='只生成1道例题和1道巩固题，讲解等式两边同时减去相同的数。';
const payload={topic:'一步方程',subject:'数学',gradeLevel:'初一',lessonHours:1,durationMinutes:45,objectives:'掌握等式性质',creationPrompt:prompt,requirementContract:shared.compileGenerationRequirementContract('lesson_plan',prompt)};
const resource=await p.lessonPlan.create({data:{userId:'teacher',topic:payload.topic,subject:payload.subject,gradeLevel:payload.gradeLevel,objectives:payload.objectives,status:'GENERATING',structuredContent:{}}});
const job=await p.aiJob.create({data:{userId:'teacher',type:'LESSON_PLAN_GENERATE',relatedEntityType:'LessonPlan',relatedEntityId:resource.id,payload}});
const data={aiJobId:job.id,userId:'teacher',lessonPlanId:resource.id,llmOverrides:{provider:'openai-compatible',model:'gpt-6-luna',apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'}};
await q.add('LESSON_PLAN_GENERATE',data,{jobId:job.id,attempts:2,removeOnComplete:false,removeOnFail:false});console.log(JSON.stringify({id:job.id,resourceId:resource.id}));
'''),'web')
        def read(job):return app.db('const id='+json.dumps(job['id'])+';'+queue(r'''
const db=await p.aiJob.findUniqueOrThrow({where:{id}}),resource=await p.lessonPlan.findUniqueOrThrow({where:{id:db.relatedEntityId}}),queued=await q.getJob(id);
const receipt=await p.aiJobGenerationUnit.findUnique({where:{aiJobId_unitId:{aiJobId:id,unitId:'lesson-generation-delivery:v1'}}});
const units=(await p.aiJobGenerationUnit.findMany({where:{aiJobId:id},orderBy:{unitId:'asc'}})).map(u=>({unitId:u.unitId,status:u.status,inputHash:u.inputHash,outputHash:require('crypto').createHash('sha256').update(JSON.stringify(u.checkpointJson?.output??null)).digest('hex')}));
const versions=await p.lessonPlanVersion.findMany({where:{lessonPlanId:resource.id},select:{id:true,version:true,displayContent:true,displayContentStudent:true}});
console.log(JSON.stringify({status:db.status,error:db.errorMessage,payload:db.payload,result:db.result,resourceStatus:resource.status,structured:resource.structuredContent,
 teacher:resource.displayContent,student:resource.displayContentStudent,receipt:receipt?.checkpointJson,versions,units,queueState:await queued.getState(),attemptsStarted:queued.attemptsStarted,stalledCounter:queued.stalledCounter}));
'''),'web')
        def partial(job):
            row=read(job);require(row['status']not in ('FAILED','INTERRUPTED'),'INITIAL_JOB_FAILED')
            return row if (row.get('result')or{}).get('stageArtifacts',{}).get('lesson_plan_quality_checkpoint')and model()['active']==1 and model()['examples']==1 and model()['modules']>=1 else None
        def complete(job):
            row=read(job);require(row['status']not in ('FAILED','INTERRUPTED'),'GENERATION_FAILED')
            return row if row['status']=='SUCCESS'and row['queueState']=='completed'else None
        def assert_delivered(row):
            require(row['resourceStatus']=='READY'and bool(row['teacher'])and bool(row['student']),'PAIRED_HANDOUTS_NOT_DELIVERED')
            require(len(row['versions'])==1 and row['receipt']['snapshotId']==row['versions'][0]['id'],'DELIVERY_VERSION_NOT_ATOMIC')
            require(row['teacher']==row['versions'][0]['displayContent']and row['student']==row['versions'][0]['displayContentStudent'],'SNAPSHOT_CONTENT_MISMATCH')
            handout=row['structured']['handout'];require(sum(len(m['examples'])for m in handout['modules'])==1 and len(handout['practice'])==1,'HANDOUT_QUANTITY_MISMATCH')
        try:
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60','postgres','redis','web','model'],timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n)",'web')==0,'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','db','push','--skip-generate'],{stdio:'inherit'})",'web',timeout=120)
            app.db("await p.user.create({data:{id:'teacher',email:'synthetic-lesson@example.invalid'}});console.log('true')",'web')
            job=seed();report['initialJob']=job;start();first=wait(lambda:partial(job),'LESSON_CHECKPOINT_NOT_SAVED')
            require(model()['plans']==1 and not model()['unknown'],'UNEXPECTED_INITIAL_REQUESTS');require(not first.get('receipt')and len(first['versions'])==0,'PREMATURE_DELIVERY')
            report['sourceHashes']=json.loads(app.node("const f=require('fs'),c=require('crypto');const names=['src/modules/lesson-plan/service/lesson-generation-delivery.ts','src/modules/lesson-plan/service/jobs/lesson-generate.job.ts','src/modules/lesson-plan/service/deps.ts','src/modules/lesson-plan/service/ppt.ts','src/lib/ai-job-debug-timing.ts'];console.log(JSON.stringify(Object.fromEntries(names.map(n=>[n,c.createHash('sha256').update(f.readFileSync('/app/apps/api/'+n)).digest('hex')]))))",'worker-general'))
            passed('initial-lesson-saves-draft-and-handout-units-before-pending-example')
            identity=worker()['Id'];start_time=time.monotonic();app.command(docker+['kill','--signal','SIGTERM',identity])
            wait(lambda:not worker()['State']['Running'],'WORKER_DID_NOT_EXIT',55)
            require(worker()['State']['ExitCode']==1 and not worker()['State']['OOMKilled'],'UNCLEAN_GENERATION_NOT_REPORTED')
            stopped=read(job);require(stopped['status']=='PROCESSING'and stopped['queueState']=='active','SHUTDOWN_CHANGED_USER_INTENT')
            require(stopped['result']['stageArtifacts']['lesson_plan_quality_checkpoint']==first['result']['stageArtifacts']['lesson_plan_quality_checkpoint'],'SAVED_DRAFT_CHANGED')
            report['shutdownSeconds']=round(time.monotonic()-start_time,3)
            stopped_stats=model();completed_units=[u for u in stopped['units']if u['status']=='complete'];require(len(completed_units)>0,'NO_COMPLETED_HANDOUT_UNIT')
            model('/release');start();resumed=wait(lambda:complete(job),'STALLED_TASK_DID_NOT_RESUME',170);assert_delivered(resumed)
            require(resumed['attemptsStarted']==2 and resumed['stalledCounter']==1,'REAL_STALLED_RETRY_NOT_OBSERVED')
            require(model()['plans']==1 and model()['examples']==2 and model()['modules']==stopped_stats['modules'] and model()['practices']==stopped_stats['practices'] and not model()['unknown'],'RESUME_REGENERATED_DRAFT_OR_UNEXPECTED_MODEL_REQUEST')
            require(all(u in resumed['units']for u in completed_units),'COMPLETED_HANDOUT_UNIT_CHANGED')
            report['resumed']=dict(reusedUnitCount=len(completed_units),attemptsStarted=resumed['attemptsStarted'],stalledCounter=resumed['stalledCounter'],snapshotId=resumed['versions'][0]['id'],stats=model())
            passed('real-timeout-and-stalled-retry-reuse-draft-and-complete-units-with-one-paired-version')
            model('/next');cancelled=seed();saved=wait(lambda:partial(cancelled),'CANCEL_CASE_NOT_READY')
            app.db('await p.aiJob.update({where:{id:'+json.dumps(cancelled['id'])+'},data:{status:"CANCEL_REQUESTED"}});console.log("true")','web')
            stopped=wait(lambda:(lambda r:r if r['status']=='INTERRUPTED'else None)(read(cancelled)),'CANCELLATION_NOT_HONORED')
            require(stopped['resourceStatus']=='INTERRUPTED'and not stopped.get('receipt')and len(stopped['versions'])==0,'CANCELLED_JOB_DELIVERED')
            require(stopped['teacher']==saved['teacher']and stopped['student']==saved['student'],'CANCELLATION_LOST_SAVED_DRAFT')
            wait(lambda:model()['active']==0,'CANCELLED_REQUEST_STILL_ACTIVE');passed('user-cancellation-aborts-request-and-retains-checkpoint-without-late-delivery')
            model('/next');clean=seed();wait(lambda:partial(clean),'CLEAN_DRAIN_CASE_NOT_READY')
            identity=worker()['Id'];app.command(docker+['kill','--signal','SIGTERM',identity])
            wait(lambda:b'SIGTERM, closing'in app.command(docker+['logs',identity]),'SIGNAL_NOT_HANDLED');model('/release')
            wait(lambda:not worker()['State']['Running'],'CLEAN_DRAIN_NOT_FINISHED',55)
            require(worker()['State']['ExitCode']==0,'CLEAN_DRAIN_FAILED');ready=wait(lambda:complete(clean),'CLEAN_DRAIN_DID_NOT_DELIVER');assert_delivered(ready)
            require(read(cancelled)['status']=='INTERRUPTED'and not model()['unknown'],'CANCELLED_TASK_RESUMED_OR_UNKNOWN_REQUEST')
            passed('graceful-stop-delivers-paired-handouts-and-keeps-cancelled-job-stopped')
            report['fixtureRequests']=model()['requests'];report['status']='passed'
        except BaseException as error:
            report['status']='failed';report['error']=configure.error_code(error);report['errorType']=type(error).__name__
            if getattr(app,'last_error',None):report['commandDiagnostic']=app.last_error
            try:
                report['modelStats']=model();report['jobState']=read(report['initialJob'])if report.get('initialJob')else None
                report['workerLogs']=app.command(docker+['logs',worker()['Id']]).decode(errors='replace')[-20000:]
            except Exception:pass
            raise
        finally:
            try:cleanup(app,docker,project);report['testResourcesRemoved']=True
            except Exception as error:
                report['testResourcesRemoved']=False;report['cleanupError']=configure.error_code(error);raise
            finally:
                args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],checks=report['checks'],productionChanged=False,realModelCalls=0)),flush=True)

if __name__=='__main__':main()
