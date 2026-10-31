"""Initial full image-deck generation across real Worker shutdown and BullMQ retry.

Real isolated PostgreSQL, Redis, MinIO and original Worker entrypoint. Model HTTP
responses/images are deterministic fixtures, not provider/content-quality proof.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import uuid

from verify_codex_application import Application, load, require
from verify_codex_package_recovery import cleanup

MODEL=r'''
const http=require('http'),sharp=require('module').createRequire('/app/apps/api/package.json')('sharp');
let hold=true,images=0,plans=0,reviews=0,requests=[],pending=new Set();
const plan={version:1,styleGuide:{visualStyle:'plain educational cards',palette:['#FFFFFF','#2563EB'],typography:'clear',consistencyRules:[]},
 slides:[1,2].map(i=>({id:'page-'+i,title:'PAGE_'+i,sectionType:'concept',goal:'Compare shapes',content:'PAGE_'+i+' Observe the circle and square.',
  keyPoints:['Circle','Square'],visualIntent:'Two shapes side by side',speakerNotes:'Explain shapes.'}))};
http.createServer((req,res)=>{
 if(req.url==='/stats'){res.end(JSON.stringify({images,plans,reviews,requests,active:pending.size}));return}
 if(req.url==='/next'){if(pending.size){res.writeHead(409);res.end('{}');return}hold=true;images=plans=reviews=0;res.end('{}');return}
 if(req.url==='/release'){hold=false;for(const finish of [...pending])finish();res.end('{}');return}
 let raw='';req.on('data',c=>raw+=c);req.on('end',async()=>{
  try{
   const body=JSON.parse(raw);
   if(req.url==='/v1/chat/completions'){
    if(body.model!=='gpt-6-luna')throw new Error('Unexpected text model');
    const planning=JSON.stringify(body.messages).includes('high-quality teaching presentation content plans');
    if(planning)plans++;else reviews++;
    requests.push({type:planning?'plan':'review',model:body.model});
    res.setHeader('content-type','application/json');res.end(JSON.stringify({id:'synthetic',object:'chat.completion',model:body.model,
     choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:JSON.stringify(planning?plan:{passed:true,issues:[]})}}],
     usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}}));return;
   }
   if(req.url==='/v1/images/generations'){
    if(body.model!=='gpt-image-2')throw new Error('Unexpected image model');
    const index=++images;requests.push({type:'image',model:body.model,index});
    const png=await sharp({create:{width:800,height:450,channels:3,background:index===1?'#2563EB':'#14B8A6'}}).png().toBuffer();
    const finish=()=>{pending.delete(finish);if(res.destroyed)return;res.setHeader('content-type','application/json');res.end(JSON.stringify({data:[{b64_json:png.toString('base64')}]}));};
    pending.add(finish);res.once('close',()=>pending.delete(finish));if(!hold||index!==2)finish();return;
   }
   res.writeHead(404);res.end('{}');
  }catch(error){res.writeHead(400);res.end(JSON.stringify({error:{message:String(error)}}));}
 });
}).listen(4988,'0.0.0.0');
'''


def main():
    parser=argparse.ArgumentParser()
    for name in('api-image','runner-image','worker-image','storage-image'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED');docker=['docker','--host',endpoint]
    tags=dict(api=args.api_image,runner=args.runner_image,worker=args.worker_image,storage=args.storage_image,postgres='postgres:17-alpine',redis='redis:7.4-alpine')
    images={key:json.loads(subprocess.check_output(docker+['image','inspect',tag]))[0]for key,tag in tags.items()}
    require(all(x['Os']=='linux'for x in images.values()),'LINUX_REQUIRED')
    require(all(images[key]['Architecture']=='amd64'for key in('api','runner','worker','storage')),'AMD64_REQUIRED')
    policy,configure=load('codex_runtime'),load('codex_configure');project='codex-ppt-generation-'+uuid.uuid4().hex[:12]
    policy.PROJECT=configure.PROJECT=project
    report=dict(status='incomplete',checks=[],project=project,images={key:x['Id']for key,x in images.items()},
        realModelCalls=0,productionChanged=False,scope='initial two-page image deck, original Worker image entrypoint, real queue/DB/object storage; deterministic HTTP model fixtures',
        limitations=['No provider inference, visual quality, SDK planning, initial lesson generation, migrations or production capacity claim'])
    report['scriptSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    def passed(name):report['checks'].append(name);print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-ppt-generation-')as directory:
        root=Path(directory).resolve();root.chmod(0o700);app=Application(root,docker,images,project,policy,configure)
        base=json.loads(app.base_path.read_bytes());env=dict(base['services']['api']['environment'])
        storage_key='synthetic-'+secrets.token_hex(6);storage_secret=secrets.token_hex(24)
        env.update(OBJECT_STORAGE_ENDPOINT='http://storage:19000',OBJECT_STORAGE_REGION='us-east-1',OBJECT_STORAGE_BUCKET='ppt-proof',
            OBJECT_STORAGE_ACCESS_KEY_ID=storage_key,OBJECT_STORAGE_SECRET_ACCESS_KEY=storage_secret,OBJECT_STORAGE_FORCE_PATH_STYLE='true')
        base['services']['web']['environment']=env
        base['services']['storage']=dict(image=images['storage']['Id'],platform='linux/amd64',networks={'app':None},
            environment=dict(MINIO_ROOT_USER=storage_key,MINIO_ROOT_PASSWORD=storage_secret),command=['server','/data','--address',':19000'])
        worker_env=dict(env,AI_WORKER_ENABLED='true',AI_WORKER_ROLE='ppt',API_BACKGROUND_TASKS_ENABLED='false',AI_JOB_WORKER_CONCURRENCY='1',AI_JOB_LOCK_DURATION_MS='60000')
        base['services']['worker-ppt']=dict(image=images['worker']['Id'],platform='linux/amd64',networks={'app':None},environment=worker_env,healthcheck={'disable':True})
        path=root/'model.cjs';path.write_text(MODEL);path.chmod(0o644)
        base['services']['model']=dict(image=images['api']['Id'],platform='linux/amd64',networks={'app':None},entrypoint=['node'],command=['/fixture/model.cjs'],
            volumes=[str(path)+':/fixture/model.cjs:ro'],healthcheck={'disable':True})
        app.atomic(app.base_path,base)
        def wait(check,code,seconds=90):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                result=check()
                if result:return result
                time.sleep(.5)
            raise ValueError(code)
        def model(path='/stats'):return json.loads(app.node("fetch('http://model:4988"+path+"').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))",'web'))
        def worker():return app.inspect()['worker-ppt']
        def start():app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','worker-ppt'],timeout=60)
        def queue(code):return "const api=require('module').createRequire('/app/apps/api/package.json');const{Queue}=api('bullmq');const{Redis}=api('ioredis');const redis=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});const q=new Queue('teaching-ai-agent-ppt-jobs',{connection:redis});try{"+code+"}finally{await q.close();await redis.quit();}"
        def seed():return app.db(queue(r'''
const config={generationMode:'image_direct',imageDirect:{aspectRatio:'16:9',slideCountMode:'auto',fixedSlideCount:2,visualStyle:{customText:'Plain shapes on white background'}}};
const resource=await p.pptProject.create({data:{userId:'teacher',configJson:config,generationStatus:'GENERATING',slidesJson:{version:1,slides:[],slideScenes:[]}}});
const payload={...config,pptProjectId:resource.id,source:{topic:'Shapes',subject:'Art',gradeLevel:'Grade 1',lessonHours:1,objectives:'Recognize circle and square'}};
const job=await p.aiJob.create({data:{userId:'teacher',type:'PPT_GENERATE',relatedEntityType:'PptProject',relatedEntityId:resource.id,payload}});
await p.pptProject.update({where:{id:resource.id},data:{generationRunId:job.id}});
const data={aiJobId:job.id,userId:'teacher',lessonPlanId:null,
 llmOverrides:{provider:'openai-compatible',model:'gpt-6-luna',apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'},
 imageRuntimeOverrides:{provider:'custom-openai-image',model:'gpt-image-2',apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'}};
await q.add('PPT_GENERATE',data,{jobId:job.id,attempts:2,removeOnComplete:false,removeOnFail:false});
console.log(JSON.stringify({id:job.id,resourceId:resource.id}));
'''),'web')
        def read(job):return app.db('const id='+json.dumps(job['id'])+';'+queue(r'''
const db=await p.aiJob.findUniqueOrThrow({where:{id}}),resource=await p.pptProject.findUniqueOrThrow({where:{id:db.relatedEntityId}}),queued=await q.getJob(id);
const checkpoint=await p.aiJobGenerationUnit.findUnique({where:{aiJobId_unitId:{aiJobId:id,unitId:'ppt-generation-deck:v1'}}});
const assets=await p.pptAsset.findMany({where:{pptProjectId:resource.id,kind:'SLIDE_IMAGE'},orderBy:{slideIndex:'asc'},select:{id:true,status:true,sha256:true,slideIndex:true,storageKey:true,bucket:true}});
console.log(JSON.stringify({status:db.status,error:db.errorMessage,payload:db.payload,resourceStatus:resource.generationStatus,deck:resource.slidesJson,
 checkpoint:checkpoint?.checkpointJson,assets,queueState:await queued.getState(),attemptsStarted:queued.attemptsStarted,stalledCounter:queued.stalledCounter}));
'''),'web')
        def partial(job):
            row=read(job);require(row['status']not in('FAILED','INTERRUPTED'),'INITIAL_JOB_FAILED')
            return row if row.get('checkpoint')and len(row['assets'])==1 and row['assets'][0]['status']=='READY'and model()['active']==1 else None
        def complete(job):
            row=read(job);require(row['status']not in('FAILED','INTERRUPTED'),'GENERATION_FAILED')
            return row if row['status']=='SUCCESS'and row['queueState']=='completed'else None
        try:
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60','postgres','redis','web','storage','model'],timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n)",'web')==0,'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','db','push','--skip-generate'],{stdio:'inherit'})",'web',timeout=120)
            app.node("const s=require('module').createRequire('/app/apps/api/package.json')('@aws-sdk/client-s3');const c=new s.S3Client({endpoint:process.env.OBJECT_STORAGE_ENDPOINT,region:'us-east-1',forcePathStyle:true,credentials:{accessKeyId:process.env.OBJECT_STORAGE_ACCESS_KEY_ID,secretAccessKey:process.env.OBJECT_STORAGE_SECRET_ACCESS_KEY}});c.send(new s.CreateBucketCommand({Bucket:'ppt-proof'})).then(()=>console.log('created'))",'web')
            app.db("await p.user.create({data:{id:'teacher',email:'synthetic-ppt@example.invalid'}});console.log('true')",'web')
            job=seed();report['initialJob']=job;start();first=wait(lambda:partial(job),'FIRST_PAGE_NOT_SAVED')
            require(model()['plans']==1 and model()['images']==2,'INITIAL_PLAN_OR_IMAGE_COUNT');require('resumeMode'not in first['payload'],'INITIAL_JOB_NOT_INITIAL')
            report['sourceHashes']=json.loads(app.node("const f=require('fs'),c=require('crypto');const names=['src/modules/lesson-plan/service/ppt-generation-checkpoint.ts','src/modules/lesson-plan/service/jobs/ppt-generate.job.ts','src/modules/lesson-plan/service/ppt.ts','src/modules/lesson-plan/service/deps.ts'];console.log(JSON.stringify(Object.fromEntries(names.map(n=>[n,c.createHash('sha256').update(f.readFileSync('/app/apps/api/'+n)).digest('hex')]))))",'worker-ppt'))
            passed('initial-task-freezes-plan-and-persists-first-image-with-job-owned-checkpoint')
            before=worker();start_time=time.monotonic();app.command(docker+['kill','--signal','SIGTERM',before['Id']])
            wait(lambda:not worker()['State']['Running'],'WORKER_DID_NOT_EXIT',55)
            require(worker()['State']['ExitCode']==1 and not worker()['State']['OOMKilled'],'UNCLEAN_GENERATION_NOT_REPORTED')
            stopped=read(job);require(stopped['status']=='PROCESSING'and stopped['queueState']=='active','SHUTDOWN_CHANGED_USER_INTENT')
            require(stopped['assets']==first['assets']and stopped['checkpoint']==first['checkpoint'],'SAVED_PAGE_CHANGED_DURING_SHUTDOWN')
            report['shutdownSeconds']=round(time.monotonic()-start_time,3)
            model('/release');start();resumed=wait(lambda:complete(job),'STALLED_TASK_DID_NOT_RESUME',170)
            require(resumed['attemptsStarted']==2 and resumed['stalledCounter']==1,'REAL_STALLED_RETRY_NOT_OBSERVED')
            require(model()['plans']==1 and model()['images']==3,'RESUME_REGENERATED_PLAN_OR_SAVED_IMAGE')
            require(resumed['assets'][0]==first['assets'][0]and len(resumed['assets'])==2,'SAVED_IMAGE_NOT_REUSED')
            require(resumed['resourceStatus']=='READY'and len(resumed['deck']['slides'])==2,'FULL_DECK_NOT_DELIVERED')
            report['resumed']=dict(attemptsStarted=resumed['attemptsStarted'],stalledCounter=resumed['stalledCounter'],assets=resumed['assets'],stats=model())
            passed('real-timeout-and-bullmq-lock-expiry-reuse-frozen-plan-and-first-image')
            model('/next');cancelled=seed();saved=wait(lambda:partial(cancelled),'CANCEL_CASE_NOT_READY')
            app.db('await p.aiJob.update({where:{id:'+json.dumps(cancelled['id'])+'},data:{status:"CANCEL_REQUESTED"}});console.log("true")','web')
            stopped=wait(lambda:(lambda r:r if r['status']=='INTERRUPTED'else None)(read(cancelled)),'CANCELLATION_NOT_HONORED')
            require(stopped['assets']==saved['assets']and stopped['resourceStatus']=='INTERRUPTED','CANCELLATION_LOST_PARTIAL_PAGE')
            wait(lambda:model()['active']==0,'CANCELLED_HTTP_REQUEST_STILL_ACTIVE')
            passed('user-cancellation-aborts-in-flight-image-and-retains-saved-page')
            model('/next');clean=seed();wait(lambda:partial(clean),'CLEAN_DRAIN_CASE_NOT_READY')
            identity=worker()['Id'];app.command(docker+['kill','--signal','SIGTERM',identity])
            wait(lambda:b'SIGTERM, closing'in app.command(docker+['logs',identity]),'SIGNAL_NOT_HANDLED');model('/release')
            wait(lambda:not worker()['State']['Running'],'CLEAN_DRAIN_NOT_FINISHED',55)
            require(worker()['State']['ExitCode']==0,'CLEAN_DRAIN_FAILED');ready=wait(lambda:complete(clean),'CLEAN_DRAIN_DID_NOT_DELIVER')
            require(len(ready['assets'])==2 and ready['resourceStatus']=='READY','CLEAN_DECK_INCOMPLETE')
            require(read(cancelled)['status']=='INTERRUPTED','CANCELLED_TASK_RESUMED')
            passed('graceful-stop-saves-complete-in-flight-deck-and-keeps-cancelled-task-stopped')
            report['fixtureRequests']=model()['requests'];report['status']='passed'
        except BaseException as error:
            report['status']='failed';report['error']=configure.error_code(error);report['errorType']=type(error).__name__
            if getattr(app,'last_error',None):report['commandDiagnostic']=app.last_error
            try:
                report['modelStats']=model();report['jobState']=read(report['initialJob'])if report.get('initialJob')else None
                report['workerLogs']=app.command(docker+['logs',worker()['Id']]).decode(errors='replace')[-16000:]
            except Exception:pass
            raise
        finally:
            try:
                cleanup(app,docker,project);report['testResourcesRemoved']=True
            except Exception as error:
                report['testResourcesRemoved']=False;report['cleanupError']=configure.error_code(error)
                raise
            finally:
                args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],checks=report['checks'],productionChanged=False,realModelCalls=0)),flush=True)


if __name__=='__main__':main()
