"""Two-lesson series: original Worker, real queue/PG and deterministic HTTP.

No provider inference or production access. Start from a saved, confirmed plan;
prove child reuse, cancellation/pause and actual stalled retry of its parent.
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

from verify_codex_lesson_generation import MODEL as LESSON_MODEL

# Deliver lesson one, then hold lesson two inside its example generation.
# All other requests still execute the real lesson/handout engine.
MODEL = LESSON_MODEL.replace("if(!hold||kind!=='example')finish();", "if(!hold||kind!=='example'||examples<2)finish();")
require(MODEL != LESSON_MODEL, 'LESSON_FIXTURE_CONTRACT_CHANGED')
# Series lessons use the existing bounded blueprint/flow and formal-document
# path, which the standalone fixture does not need. Model the real contracts.
MODEL = MODEL.replace("if(system.includes('expert instructional designer'))", """
if(system.includes('字段必须为 objectives、keyPoints、difficulties、teachingMethods、activities、homeworkSuggestion、evidenceAnchors')){
 kind='blueprint';plans++;output={objectives:['理解等式性质','会解一步方程','能检验方程解'],keyPoints:['两边同时减去同一个数','代入检验'],difficulties:['保持等式相等','说明变形依据'],teachingMethods:['练习法','讨论法'],activities:['比较等式','独立解题'],homeworkSuggestion:'复习等式性质',evidenceAnchors:['等式两边同时减去相同的数']}
}else if(system.includes('只生成本批次列出的 1-2 个教学环节')){
 kind='flow';const units=JSON.parse(user.slice(user.indexOf('[')));output={items:units.map(u=>({unitId:u.unitId,title:u.title,content:'**教师活动**\\n'+('比较方程两边的量，说明同时减去相同的数仍然相等。'.repeat(28))+'\\n**学生活动与预设回答**\\n学生说明等式性质，并用代入法检查答案。例题：x+3=8，解得x=5。\\n**设计意图**\\n检查等式变形与逆运算的关系。'}))}
}else if(system.includes('你是资深学科教研员与一线特级教师')){
 kind='document';const data={...plan,meta:{lessonType:'复习课',periodLabel:'第1课时',teacher:'',scheduleTime:''},assumptions:['合成测试学情'],contentNavigation:'等式性质、例题、练习',objectivesByDimension:{knowledgeSkills:['掌握等式性质'],processMethod:['观察与检验'],affective:['认真计算']},preparation:{teacher:'准备例题',student:'复习加减法'}};
 const fields=user.match(/本批次只生成这些字段：([^；]+)；/)[1].split('、');output=Object.fromEntries(fields.map(k=>[k,data[k]]));
}else if(system.includes('expert instructional designer'))
""")




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
    policy,configure=load('codex_runtime'),load('codex_configure');project='codex-series-generation-'+uuid.uuid4().hex[:12]
    policy.PROJECT=configure.PROJECT=project
    report=dict(status='incomplete',checks=[],project=project,images={key:x['Id']for key,x in images.items()},realModelCalls=0,productionChanged=False,
      scope='saved two-lesson plan, original Worker entrypoint, real BullMQ/PostgreSQL; deterministic HTTP model fixture',
      limitations=['No provider inference, content quality, SDK/series planning, source attachments, other domain Jobs, production capacity or deployment claim'])
    report['scriptSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['fixtureSha256']=hashlib.sha256(MODEL.encode()).hexdigest()
    def passed(name):report['checks'].append(name);print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-series-generation-')as directory:
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
const shared=api('tsx/cjs/api').require('/app/packages/shared/src/index.ts','/app/control.cjs');
const prompt='每节只生成1道例题和1道巩固题，讲解等式两边同时减去相同的数。';
const settings=shared.lessonSeriesSettingsSchema.parse({title:'两节方程巩固',subject:'数学',gradeLevel:'初一',totalLessons:2,minutesPerLesson:45,requirements:prompt,
 requirementContract:shared.compileGenerationRequirementContract('lesson_plan',prompt)});
const parentId=require('crypto').randomUUID();
const series=await p.lessonSeries.create({data:{userId:'teacher',title:settings.title,settings,source:{kind:'requirements'},activeJobId:parentId,
 units:[{id:'u1',title:'方程',pages:[],lessonCount:2,planned:true,recommended:true,allocated:true,review:[]}]}});
for(let i=1;i<=2;i++){
 const plan=shared.seriesLessonPlanSchema.parse({title:'一步方程第'+i+'课',pages:[],lessonType:'探究课',objectives:['理解等式性质'],prerequisite:'加减法',assessment:'解方程',
  flow:['导入','探究','总结'].map(title=>({title,instruction:'两边同时减去相同的数',minutes:15}))});
 await p.lessonSeriesLesson.create({data:{seriesId:series.id,unitId:'u1',position:i,plan}});
}
await p.aiJob.create({data:{id:parentId,userId:'teacher',type:'LESSON_SERIES_GENERATE',relatedEntityType:'LessonSeries',relatedEntityId:series.id,payload:{}}});
await q.add('LESSON_SERIES_GENERATE',{aiJobId:parentId,userId:'teacher',seriesId:series.id,llmOverrides:{provider:'openai-compatible',model:'gpt-6-luna',apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'}},
 {jobId:parentId,attempts:2,removeOnComplete:false,removeOnFail:false});console.log(JSON.stringify({id:parentId,resourceId:series.id}));
'''),'web')
        def read(job):return app.db('const id='+json.dumps(job['id'])+';'+queue(r'''
const db=await p.aiJob.findUniqueOrThrow({where:{id}}),series=await p.lessonSeries.findUniqueOrThrow({where:{id:db.relatedEntityId}}),queued=await q.getJob(id),children=[];
for(const lesson of await p.lessonSeriesLesson.findMany({where:{seriesId:series.id},orderBy:{position:'asc'}})){
 if(!lesson.generationJobId){children.push({lessonId:lesson.id});continue;}
 const child=await p.aiJob.findUniqueOrThrow({where:{id:lesson.generationJobId}}),resource=await p.lessonPlan.findUniqueOrThrow({where:{id:lesson.lessonPlanId}});
 const receipt=await p.aiJobGenerationUnit.findUnique({where:{aiJobId_unitId:{aiJobId:child.id,unitId:'lesson-generation-delivery:v1'}}});
 const units=(await p.aiJobGenerationUnit.findMany({where:{aiJobId:child.id},orderBy:{unitId:'asc'}})).map(u=>({unitId:u.unitId,status:u.status,inputHash:u.inputHash,outputHash:require('crypto').createHash('sha256').update(JSON.stringify(u.checkpointJson?.output??null)).digest('hex')}));
 const versions=await p.lessonPlanVersion.findMany({where:{lessonPlanId:resource.id},select:{id:true,version:true,displayContent:true,displayContentStudent:true}});
 children.push({lessonId:lesson.id,id:child.id,resourceId:resource.id,status:child.status,error:child.errorMessage,payload:child.payload,result:child.result,resourceStatus:resource.status,
  structured:resource.structuredContent,teacher:resource.displayContent,student:resource.displayContentStudent,receipt:receipt?.checkpointJson,versions,units});
}
console.log(JSON.stringify({status:db.status,error:db.errorMessage,result:db.result,seriesStatus:series.status,activeJobId:series.activeJobId,pauseRequested:series.pauseRequested,children,
 queueState:await queued.getState(),attemptsStarted:queued.attemptsStarted,stalledCounter:queued.stalledCounter}));
'''),'web')
        def partial(job):
            row=read(job);require(row['status']not in ('FAILED','INTERRUPTED'),'SERIES_JOB_FAILED')
            require(row['status']!='SUCCESS','SERIES_FINISHED_WITHOUT_SECOND_LESSON_CHECKPOINT')
            first,second=row['children'];stats=model()
            return row if first.get('status')=='SUCCESS'and second.get('status')=='PROCESSING'and (second.get('result')or{}).get('stageArtifacts',{}).get('lesson_plan_quality_checkpoint')and stats['active']==1 and stats['examples']==2 else None
        def complete(job):
            row=read(job);require(row['status']not in ('FAILED','INTERRUPTED'),'SERIES_GENERATION_FAILED')
            return row if row['status']=='SUCCESS'and row['queueState']=='completed'else None
        def assert_child(row):
            require(row['status']=='SUCCESS'and row['resourceStatus']=='READY'and bool(row['teacher'])and bool(row['student']),'PAIRED_HANDOUTS_NOT_DELIVERED')
            require(len(row['versions'])==1 and row['receipt']['snapshotId']==row['versions'][0]['id'],'DELIVERY_VERSION_NOT_ATOMIC')
            require(row['teacher']==row['versions'][0]['displayContent']and row['student']==row['versions'][0]['displayContentStudent'],'SNAPSHOT_CONTENT_MISMATCH')
            handout=row['structured']['handout'];require(sum(len(m['examples'])for m in handout['modules'])==1 and len(handout['practice'])==1,'HANDOUT_QUANTITY_MISMATCH')
        def assert_delivered(row):
            require(row['seriesStatus']=='READY'and row['result']['progress']['percent']==100,'SERIES_NOT_COMPLETED')
            for child in row['children']:assert_child(child)
        def assert_preserved(before,after):
            for key in ('id','resourceId','status','resourceStatus','teacher','student','versions','receipt','units'):
                require(before[key]==after[key],'DELIVERED_CHILD_CHANGED_'+key.upper())
        current_job=None
        try:
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60','postgres','redis','web','model'],timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n)",'web')==0,'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','db','push','--skip-generate'],{stdio:'inherit'})",'web',timeout=120)
            app.db("await p.user.create({data:{id:'teacher',email:'synthetic-lesson@example.invalid'}});console.log('true')",'web')
            current_job=job=seed();report['initialJob']=job;start();first=wait(lambda:partial(job),'SECOND_LESSON_CHECKPOINT_NOT_SAVED')
            assert_child(first['children'][0]);require(model()['plans']==4 and all(sum(x['type']==kind for x in model()['requests'])==2 for kind in ('plan','blueprint')) and not model()['unknown'],'UNEXPECTED_INITIAL_REQUESTS')
            require(not first['children'][1].get('receipt')and len(first['children'][1]['versions'])==0,'PREMATURE_SECOND_DELIVERY')
            report['sourceHashes']=json.loads(app.node("const f=require('fs'),c=require('crypto');const names=['src/modules/lesson-series/job.ts','src/modules/lesson-series/series-execution.ts','src/modules/lesson-plan/service/lesson-generation-delivery.ts','src/modules/lesson-plan/service/jobs/lesson-generate.job.ts','src/modules/lesson-plan/service/deps.ts','src/modules/lesson-plan/service/ppt.ts','src/queue/ai-jobs-worker-factory.ts','src/lib/ai-job-debug-timing.ts'];console.log(JSON.stringify(Object.fromEntries(names.map(n=>[n,c.createHash('sha256').update(f.readFileSync('/app/apps/api/'+n)).digest('hex')]))))",'worker-general'))
            passed('series-delivers-first-child-and-checkpoints-second-before-pending-example')
            identity=worker()['Id'];start_time=time.monotonic();app.command(docker+['kill','--signal','SIGTERM',identity])
            wait(lambda:not worker()['State']['Running'],'WORKER_DID_NOT_EXIT',55)
            require(worker()['State']['ExitCode']==1 and not worker()['State']['OOMKilled'],'UNCLEAN_GENERATION_NOT_REPORTED')
            stopped=read(job);require(stopped['status']=='PROCESSING'and stopped['queueState']=='active','SHUTDOWN_CHANGED_USER_INTENT')
            assert_preserved(first['children'][0],stopped['children'][0]);second=stopped['children'][1]
            require(second['result']['stageArtifacts']['lesson_plan_quality_checkpoint']==first['children'][1]['result']['stageArtifacts']['lesson_plan_quality_checkpoint'],'SAVED_SECOND_DRAFT_CHANGED')
            report['shutdownSeconds']=round(time.monotonic()-start_time,3)
            stopped_stats=model();completed_units=[u for u in second['units']if u['status']=='complete'];require(len(completed_units)>0,'NO_COMPLETED_SECOND_HANDOUT_UNIT')
            model('/release');start();resumed=wait(lambda:complete(job),'STALLED_SERIES_DID_NOT_RESUME',170);assert_delivered(resumed)
            require(resumed['attemptsStarted']==2 and resumed['stalledCounter']==1,'REAL_STALLED_RETRY_NOT_OBSERVED')
            assert_preserved(first['children'][0],resumed['children'][0]);resumed_second=resumed['children'][1]
            require(resumed_second['id']==second['id']and resumed_second['resourceId']==second['resourceId'],'SECOND_CHILD_RECREATED')
            require(model()['plans']==4 and model()['examples']==3 and model()['modules']==stopped_stats['modules'] and model()['practices']==stopped_stats['practices'] and not model()['unknown'],'RESUME_REGENERATED_COMPLETED_WORK')
            require(all(sum(x['type']==kind for x in model()['requests'])==sum(x['type']==kind for x in stopped_stats['requests']) for kind in ('plan','blueprint','flow','document')),'RESUME_REGENERATED_SERIES_LESSON_STAGES')
            require(all(u in resumed_second['units']for u in completed_units),'COMPLETED_SECOND_HANDOUT_UNIT_CHANGED')
            report['resumed']=dict(reusedUnitCount=len(completed_units),attemptsStarted=resumed['attemptsStarted'],stalledCounter=resumed['stalledCounter'],childIds=[x['id']for x in resumed['children']],stats=model())
            passed('real-timeout-and-stalled-retry-preserve-first-child-and-reuse-second-draft-with-two-paired-versions')
            cancelled=[]
            for mode in ('cancel','pause'):
                model('/next');current_job=case=seed();saved=wait(lambda:partial(case),'STOP_CASE_NOT_READY')
                if mode=='cancel':app.db('await p.aiJob.update({where:{id:'+json.dumps(case['id'])+'},data:{status:"CANCEL_REQUESTED"}});console.log("true")','web')
                else:app.db('await p.lessonSeries.update({where:{id:'+json.dumps(case['resourceId'])+'},data:{pauseRequested:true}});console.log("true")','web')
                stopped=wait(lambda:(lambda r:r if r['status']=='INTERRUPTED'and r['queueState']=='completed'else None)(read(case)),'SERIES_STOP_NOT_HONORED')
                require(stopped['seriesStatus']=='PAUSED','STOPPED_SERIES_NOT_PAUSED');assert_preserved(saved['children'][0],stopped['children'][0])
                child=stopped['children'][1];require(child['status']=='INTERRUPTED'and child['resourceStatus']=='INTERRUPTED'and not child.get('receipt')and len(child['versions'])==0,'STOPPED_SECOND_CHILD_DELIVERED')
                require(child['teacher']==saved['children'][1]['teacher']and child['student']==saved['children'][1]['student'],'STOP_LOST_SAVED_DRAFT')
                wait(lambda:model()['active']==0,'STOPPED_REQUEST_STILL_ACTIVE');cancelled.append(case)
                passed(mode+'-aborts-second-request-preserves-first-child-and-prevents-late-delivery')
            model('/next');current_job=offline=seed();saved=wait(lambda:partial(offline),'OFFLINE_STOP_CASE_NOT_READY')
            identity=worker()['Id'];app.command(docker+['kill','--signal','SIGKILL',identity])
            wait(lambda:not worker()['State']['Running'],'KILLED_WORKER_STILL_RUNNING')
            require(worker()['State']['ExitCode']==137 and not worker()['State']['OOMKilled'],'ABRUPT_EXIT_NOT_OBSERVED')
            wait(lambda:model()['active']==0,'KILLED_WORKER_REQUEST_STILL_ACTIVE');request_count=len(model()['requests'])
            app.node("const api=require('module').createRequire('/app/apps/api/package.json');const service=api('tsx/cjs/api').require('/app/apps/api/src/modules/lesson-plan/service/ppt.ts','/app/control.cjs');service.interruptAiJobForUser('teacher',"+json.dumps(offline['id'])+").then(r=>{if(!r.ok||r.job.status!=='INTERRUPTED')throw Error('SERIES_STOP_REQUEST_FAILED');process.exit(0)}).catch(e=>{console.error(e.message);process.exit(1)})",'web')
            stopped=read(offline);assert_preserved(saved['children'][0],stopped['children'][0])
            require(stopped['status']=='INTERRUPTED'and stopped['seriesStatus']=='PAUSED'and stopped['children'][1]['status']=='INTERRUPTED'and stopped['children'][1]['resourceStatus']=='INTERRUPTED','OFFLINE_STOP_LEFT_CHILD_PROCESSING')
            start();settled=wait(lambda:(lambda r:r if r['queueState']=='completed'else None)(read(offline)),'STOPPED_STALLED_JOB_NOT_SETTLED',170)
            require(settled['stalledCounter']==1 and settled['attemptsStarted']==2 and len(model()['requests'])==request_count,'STOPPED_STALLED_PARENT_REGENERATED')
            assert_preserved(saved['children'][0],settled['children'][0]);require(not settled['children'][1].get('receipt')and not settled['children'][1]['versions'],'STOPPED_STALLED_CHILD_DELIVERED')
            cancelled.append(offline);passed('cancel-during-worker-outage-settles-children-and-stalled-delivery-makes-no-model-request')
            model('/next');current_job=clean=seed();wait(lambda:partial(clean),'CLEAN_DRAIN_CASE_NOT_READY')
            identity=worker()['Id'];app.command(docker+['kill','--signal','SIGTERM',identity])
            wait(lambda:b'SIGTERM, closing'in app.command(docker+['logs',identity]),'SIGNAL_NOT_HANDLED');model('/release')
            wait(lambda:not worker()['State']['Running'],'CLEAN_DRAIN_NOT_FINISHED',55)
            require(worker()['State']['ExitCode']==0,'CLEAN_DRAIN_FAILED');ready=wait(lambda:complete(clean),'CLEAN_DRAIN_DID_NOT_DELIVER');assert_delivered(ready)
            require(all(read(case)['status']=='INTERRUPTED'for case in cancelled)and not model()['unknown'],'STOPPED_SERIES_RESUMED_OR_UNKNOWN_REQUEST')
            passed('graceful-stop-delivers-both-pairs-and-keeps-stopped-series-interrupted')
            report['fixtureRequests']=model()['requests'];report['status']='passed'
        except BaseException as error:
            report['status']='failed';report['error']=configure.error_code(error);report['errorType']=type(error).__name__
            if getattr(app,'last_error',None):report['commandDiagnostic']=app.last_error
            try:
                report['modelStats']=model();report['jobState']=read(current_job)if current_job else None
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
