"""Paper source preparation and recognition across actual Worker lifecycle.

Original Worker entrypoint, isolated PG/Redis/MinIO, real upload service and
persistent dispatch. Synthetic HTTP answers; no external model or production.
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
const http=require('http'),sharp=require('module').createRequire('/app/apps/api/package.json')('sharp');
let hold=true,requests=[],pending=new Set(),unknown=[];
http.createServer((req,res)=>{
 if(req.url==='/stats'){res.end(JSON.stringify({requests,active:pending.size,unknown}));return;}
 if(req.url==='/next'){if(pending.size){res.writeHead(409);res.end('{}');return;}hold=true;res.end('{}');return;}
 if(req.url==='/release'){hold=false;for(const f of [...pending])f();res.end('{}');return;}
 let raw='';req.on('data',c=>raw+=c);req.on('end',async()=>{
  try{
   const body=JSON.parse(raw);
   if(req.url!=='/v1/chat/completions'||body.model!=='gpt-6-luna'||!body.messages[0].content.includes('试卷电子化转录器')){
    unknown.push({url:req.url,model:body.model,system:String(body.messages?.[0]?.content).slice(0,150)});throw Error('Unexpected request');
   }
   const url=body.messages.at(-1).content.find(p=>p.type==='image_url').image_url.url;
   if(!url.startsWith('data:image/'))throw Error('Image bytes required');
   const stats=await sharp(Buffer.from(url.split(',')[1],'base64')).stats(),page=stats.channels[0].mean>200?1:2;
   requests.push({page,model:body.model});
   const content={pageKind:'questions',title:'两页材料',questions:[{number:String(page),stemMarkdown:'阅读第'+page+'页的文字，写出一个完整句子。',bbox:{x:.1,y:.2,width:.8,height:.2}}]};
   const finish=()=>{pending.delete(finish);if(res.destroyed)return;res.setHeader('content-type','application/json');res.end(JSON.stringify({id:'synthetic',object:'chat.completion',model:body.model,
    choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:JSON.stringify(content)}}],usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}}));};
   pending.add(finish);res.once('close',()=>pending.delete(finish));if(!hold||page===1)finish();
  }catch(error){res.writeHead(400);res.end(JSON.stringify({error:{message:String(error)}}));}
 });
}).listen(4988,'0.0.0.0');
'''


def main():
    parser = argparse.ArgumentParser()
    for name in ('worker-image', 'runner-image', 'storage-image'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    endpoint = json.loads(subprocess.check_output(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'), 'LOCAL_DOCKER_REQUIRED')
    docker = ['docker', '--host', endpoint]
    tags = dict(api=args.worker_image, worker=args.worker_image, runner=args.runner_image, storage=args.storage_image,
                postgres='postgres:17-alpine', redis='redis:7.4-alpine')
    images = {key: json.loads(subprocess.check_output(docker+['image', 'inspect', tag]))[0] for key, tag in tags.items()}
    require(all(x['Os'] == 'linux' for x in images.values()), 'LINUX_REQUIRED')
    require(all(images[k]['Architecture'] == 'amd64' for k in ('worker', 'runner', 'storage')), 'AMD64_REQUIRED')
    policy, configure = load('codex_runtime'), load('codex_configure')
    project = 'codex-paper-generation-'+uuid.uuid4().hex[:12]
    policy.PROJECT = configure.PROJECT = project
    report = dict(status='incomplete', checks=[], project=project, images={k: x['Id'] for k, x in images.items()},
                  realModelCalls=0, productionChanged=False,
                  scope='original Worker; real upload service, persistent dispatch, PG, BullMQ and private MinIO; two image sources',
                  limitations=['Synthetic model responses, no content-quality or provider-cost proof',
                               'No browser/HTTP upload controller, PDF parser, audio or production capacity validation'])
    report['scriptSha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['fixtureSha256'] = hashlib.sha256(MODEL.encode()).hexdigest()

    def passed(name):
        report['checks'].append(name)
        print(json.dumps(dict(check=name, status='passed')), flush=True)

    with tempfile.TemporaryDirectory(prefix='codex-paper-generation-') as directory:
        root = Path(directory).resolve()
        root.chmod(0o700)
        app = Application(root, docker, images, project, policy, configure)
        base = json.loads(app.base_path.read_bytes())
        env = dict(base['services']['api']['environment'])
        storage_key, storage_secret = 'synthetic-'+secrets.token_hex(6), secrets.token_hex(24)
        env.update(OBJECT_STORAGE_ENDPOINT='http://storage:19000', OBJECT_STORAGE_REGION='us-east-1', OBJECT_STORAGE_BUCKET='paper-proof',
                   OBJECT_STORAGE_ACCESS_KEY_ID=storage_key, OBJECT_STORAGE_SECRET_ACCESS_KEY=storage_secret, OBJECT_STORAGE_FORCE_PATH_STYLE='true')
        base['services']['web']['environment'] = env
        base['services']['storage'] = dict(image=images['storage']['Id'], platform='linux/amd64', networks={'app': None},
            environment=dict(MINIO_ROOT_USER=storage_key, MINIO_ROOT_PASSWORD=storage_secret), command=['server', '/data', '--address', ':19000'])
        worker_env = dict(env, AI_WORKER_ENABLED='true', AI_WORKER_ROLE='general', API_BACKGROUND_TASKS_ENABLED='false',
                          AI_JOB_WORKER_CONCURRENCY='1', AI_JOB_LOCK_DURATION_MS='60000')
        base['services']['worker-general'] = dict(image=images['worker']['Id'], platform='linux/amd64', networks={'app': None},
                                                 environment=worker_env, healthcheck={'disable': True})
        path = root/'model.cjs'
        path.write_text(MODEL)
        path.chmod(0o644)
        base['services']['model'] = dict(image=images['worker']['Id'], platform='linux/amd64', networks={'app': None},
            entrypoint=['node'], command=['/fixture/model.cjs'], volumes=[str(path)+':/fixture/model.cjs:ro'], healthcheck={'disable': True})
        app.atomic(app.base_path, base)

        def wait(check, code, seconds=90):
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                value = check()
                if value:
                    return value
                time.sleep(.5)
            raise ValueError(code)

        def model(path='/stats'):
            return json.loads(app.node("fetch('http://model:4988"+path+"').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))", 'web'))

        def worker():
            return app.inspect()['worker-general']

        def start():
            app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', 'worker-general'], timeout=60)

        def queue(code):
            return "const api=require('module').createRequire('/app/apps/api/package.json');const{Queue}=api('bullmq');const{Redis}=api('ioredis');const redis=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});const q=new Queue('teaching-ai-agent-ai-jobs',{connection:redis});try{"+code+"}finally{await q.close();await redis.quit();}"

        def business(code):
            prelude = "const api=require('module').createRequire('/app/apps/api/package.json'),load=p=>api('tsx/cjs/api').require('/app/apps/api/src/'+p,'/app/control.cjs');const{prisma:p}=load('lib/prisma.ts');"
            raw = app.node(prelude+"(async()=>{"+code+"})().then(async result=>{await p.$disconnect();process.stdout.write('RESULT:'+JSON.stringify(result)+'\\n');process.exit(0)}).catch(e=>{console.error(e);process.exit(1)})", 'web', timeout=60).decode()
            return json.loads(next(line[7:] for line in reversed(raw.splitlines()) if line.startswith('RESULT:')))

        def seed():
            return business(r'''
const sharp=api('sharp'),requestId=require('crypto').randomUUID(),files=[];
for(const page of [1,2]){const buffer=await sharp({create:{width:300,height:450,channels:3,background:page===1?'white':'#808080'}}).jpeg().toBuffer();files.push({originalname:'page-'+page+'.jpg',mimetype:'image/jpeg',buffer,size:buffer.length});}
const runtime={provider:'openai-compatible',model:'gpt-6-luna',supportsVisionInput:true,apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'};
const detail=await load('modules/paper-digitization/paper-digitization.service.ts').createDigitizedPaper('teacher',files,'两页材料',[0,0],runtime,true,undefined,null,['questions','questions'],undefined,requestId);
const row=await p.homework.findUniqueOrThrow({where:{id:detail.id}}),id=row.configJson.activeJobId;
const session=await p.agentSession.create({data:{userId:'teacher',title:'材料处理',context:{}}});
const run=await p.agentRun.create({data:{userId:'teacher',sessionId:session.id,clientRequestId:require('crypto').randomUUID(),requestHash:'synthetic',request:'处理材料后备课',context:{},status:'waiting_job',passwordVersion:0,runtimeCipherVersion:1,runtimeCiphertext:Buffer.from('synthetic')}});
await p.agentOperation.create({data:{id:'paper-upload:'+requestId,runId:run.id,stage:'source_processing',inputHash:'synthetic',jobId:id}});
await load('queue/lesson-generation-dispatch.ts').dispatchPendingLessonGenerations();
return {id,resourceId:detail.id,runId:run.id,sourceHashes:files.map(f=>require('crypto').createHash('sha256').update(f.buffer).digest('hex'))};
''')

        def read(job):
            return app.db('const id='+json.dumps(job['id'])+';'+queue(r'''
const db=await p.aiJob.findUniqueOrThrow({where:{id}}),resource=await p.homework.findUniqueOrThrow({where:{id:db.relatedEntityId}}),queued=await q.getJob(id);
console.log(JSON.stringify({status:db.status,error:db.errorMessage,result:db.result,resourceStatus:resource.generationStatus,paper:resource.questionsJson,config:resource.configJson,
 queueState:queued?await queued.getState():null,attemptsStarted:queued?.attemptsStarted,stalledCounter:queued?.stalledCounter}));
'''), 'web')

        def partial(job):
            row = read(job)
            require(row['status'] not in ('FAILED', 'INTERRUPTED', 'SUCCESS'), 'PAPER_FINISHED_BEFORE_PENDING_SECOND_PAGE')
            return row if len(row['paper']['questions']) == 1 and row['paper']['pages'][0]['status'] == 'ready' and model()['active'] == 1 else None

        def complete(job):
            row = read(job)
            require(row['status'] not in ('FAILED', 'INTERRUPTED'), 'PAPER_GENERATION_FAILED')
            return row if row['status'] == 'SUCCESS' and row['queueState'] == 'completed' else None

        def assert_delivered(row):
            require(row['resourceStatus'] == 'READY' and row['result']['progress']['percent'] == 100, 'PAPER_NOT_COMPLETED')
            require(len(row['paper']['questions']) == 2 and all(p['status'] == 'ready' for p in row['paper']['pages']), 'PAPER_CONTENT_INCOMPLETE')
            require(len(row['paper']['layout']['pages']) == 2, 'PRINTABLE_LAYOUT_MISSING')

        def assert_preserved(before, after):
            require(before['paper']['questions'][0] == after['paper']['questions'][0], 'FIRST_QUESTION_CHANGED')
            require(before['config']['sources'] == after['config']['sources'] and before['config']['pageAssets'] == after['config']['pageAssets'], 'SOURCE_ASSETS_CHANGED')

        current = None
        try:
            app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '60', 'postgres', 'redis', 'web', 'storage', 'model'], timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n)", 'web') == 0, 'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','migrate','deploy'],{stdio:'inherit'})", 'web', timeout=120)
            app.node("const s=require('module').createRequire('/app/apps/api/package.json')('@aws-sdk/client-s3');const c=new s.S3Client({endpoint:process.env.OBJECT_STORAGE_ENDPOINT,region:'us-east-1',forcePathStyle:true,credentials:{accessKeyId:process.env.OBJECT_STORAGE_ACCESS_KEY_ID,secretAccessKey:process.env.OBJECT_STORAGE_SECRET_ACCESS_KEY}});c.send(new s.CreateBucketCommand({Bucket:'paper-proof'})).then(()=>console.log('created'))", 'web')
            app.db("await p.user.create({data:{id:'teacher',email:'synthetic-paper@example.invalid'}});console.log('true')", 'web')
            current = job = seed()
            report['initialJob'] = job
            start()
            first = wait(lambda: partial(job), 'FIRST_PAGE_NOT_SAVED')
            require([x['page'] for x in model()['requests']] == [1, 2], 'UNEXPECTED_INITIAL_REQUESTS')
            require(all(s['prepared'] for s in first['config']['sources']) and len(first['config']['pageAssets']) == 2, 'SOURCES_NOT_PREPARED')
            assets = business('const row=await p.homework.findUniqueOrThrow({where:{id:'+json.dumps(job['resourceId'])+'}});'+r'''
const c=require('crypto'),{readPaperAsset}=load('modules/paper-digitization/paper-assets.ts');
const hash=async asset=>c.createHash('sha256').update(await readPaperAsset(asset)).digest('hex');
return {sources:await Promise.all(row.configJson.sources.map(s=>hash(s.asset))),pages:await Promise.all(Object.values(row.configJson.pageAssets).map(hash))};
''')
            require(assets['sources'] == job['sourceHashes'] and len(assets['pages']) == 2, 'STORED_SOURCE_BYTES_MISMATCH')
            report['assets'] = assets
            report['sourceHashes'] = json.loads(app.node("const f=require('fs'),c=require('crypto');const names=['src/modules/paper-digitization/paper-digitization.job.ts','src/modules/paper-digitization/paper-execution.ts','src/modules/paper-digitization/paper-digitization.service.ts','src/modules/paper-digitization/paper-assets.ts','src/queue/ai-jobs-worker-factory.ts','src/lib/agent-generation-intent.ts'];console.log(JSON.stringify(Object.fromEntries(names.map(n=>[n,c.createHash('sha256').update(f.readFileSync('/app/apps/api/'+n)).digest('hex')]))))", 'worker-general'))
            passed('real-upload-service-stores-sources-prepares-two-pages-and-checkpoints-first-question')
            identity, started = worker()['Id'], time.monotonic()
            app.command(docker+['kill', '--signal', 'SIGTERM', identity])
            wait(lambda: not worker()['State']['Running'], 'WORKER_DID_NOT_EXIT', 55)
            require(worker()['State']['ExitCode'] == 1 and not worker()['State']['OOMKilled'], 'UNCLEAN_STOP_NOT_REPORTED')
            stopped = read(job)
            require(stopped['status'] == 'PROCESSING' and stopped['queueState'] == 'active', 'STOP_CHANGED_USER_INTENT')
            assert_preserved(first, stopped)
            report['shutdownSeconds'] = round(time.monotonic()-started, 3)
            model('/release')
            start()
            resumed = wait(lambda: complete(job), 'STALLED_PAPER_DID_NOT_RESUME', 170)
            assert_delivered(resumed)
            assert_preserved(first, resumed)
            require(resumed['attemptsStarted'] == 2 and resumed['stalledCounter'] == 1, 'REAL_STALLED_RETRY_NOT_OBSERVED')
            require([x['page'] for x in model()['requests']] == [1, 2, 2], 'RESUME_RECOGNIZED_COMPLETED_PAGE')
            report['resumed'] = dict(attemptsStarted=resumed['attemptsStarted'], stalledCounter=resumed['stalledCounter'], questionIds=[q['id'] for q in resumed['paper']['questions']])
            passed('timeout-and-real-stalled-retry-reuse-source-pages-and-first-question')
            stopped_jobs = []
            for mode in ('job', 'assistant'):
                model('/next')
                current = case = seed()
                saved = wait(lambda: partial(case), 'CANCEL_CASE_NOT_READY')
                if mode == 'job':
                    business("await load('modules/paper-digitization/paper-digitization.service.ts').cancelPaperRecognition("+json.dumps(case['resourceId'])+",'teacher');return true;")
                else:
                    app.db('await p.agentRun.update({where:{id:'+json.dumps(case['runId'])+'},data:{status:"cancelling",cancelRequested:true}});console.log("true")', 'web')
                cancelled = wait(lambda: (lambda r: r if r['status'] == 'INTERRUPTED' and r['queueState'] == 'completed' else None)(read(case)), 'CANCEL_NOT_HONORED')
                assert_preserved(saved, cancelled)
                require(cancelled['resourceStatus'] == 'INTERRUPTED' and len(cancelled['paper']['questions']) == 1, 'CANCELLED_PAPER_DELIVERED_LATE')
                wait(lambda: model()['active'] == 0, 'CANCELLED_HTTP_STILL_ACTIVE')
                stopped_jobs.append(case)
                passed(mode+'-cancellation-aborts-http-preserves-first-question-and-settles-paper')
            model('/next')
            current = offline = seed()
            saved = wait(lambda: partial(offline), 'OFFLINE_CANCEL_CASE_NOT_READY')
            app.command(docker+['kill', '--signal', 'SIGKILL', worker()['Id']])
            wait(lambda: not worker()['State']['Running'], 'KILLED_WORKER_STILL_RUNNING')
            require(worker()['State']['ExitCode'] == 137 and not worker()['State']['OOMKilled'], 'ABRUPT_EXIT_NOT_OBSERVED')
            wait(lambda: model()['active'] == 0, 'KILLED_HTTP_STILL_ACTIVE')
            count = len(model()['requests'])
            app.db('await p.agentRun.update({where:{id:'+json.dumps(offline['runId'])+'},data:{status:"cancelling",cancelRequested:true}});console.log("true")', 'web')
            start()
            settled = wait(lambda: (lambda r: r if r['queueState'] == 'completed' else None)(read(offline)), 'CANCELLED_STALLED_JOB_NOT_SETTLED', 170)
            require(settled['status'] == 'INTERRUPTED' and settled['resourceStatus'] == 'INTERRUPTED' and len(model()['requests']) == count, 'OUTAGE_CANCEL_FAILED')
            require(settled['attemptsStarted'] == 2 and settled['stalledCounter'] == 1, 'OUTAGE_STALLED_RETRY_NOT_OBSERVED')
            assert_preserved(saved, settled)
            stopped_jobs.append(offline)
            passed('assistant-cancel-during-outage-settles-paper-on-real-stalled-retry-with-no-new-http')
            model('/next')
            current = clean = seed()
            wait(lambda: partial(clean), 'CLEAN_DRAIN_CASE_NOT_READY')
            identity = worker()['Id']
            app.command(docker+['kill', '--signal', 'SIGTERM', identity])
            wait(lambda: b'SIGTERM, closing' in app.command(docker+['logs', identity]), 'SIGNAL_NOT_HANDLED')
            model('/release')
            wait(lambda: not worker()['State']['Running'], 'CLEAN_DRAIN_DID_NOT_FINISH', 55)
            require(worker()['State']['ExitCode'] == 0, 'CLEAN_DRAIN_FAILED')
            assert_delivered(wait(lambda: complete(clean), 'CLEAN_DRAIN_DID_NOT_DELIVER'))
            require(all(read(case)['status'] == 'INTERRUPTED' for case in stopped_jobs) and not model()['unknown'], 'STOPPED_PAPER_REVIVED_OR_UNKNOWN_HTTP')
            passed('graceful-drain-delivers-both-pages-atomically-without-reviving-cancelled-jobs')
            report['fixtureRequests'] = model()['requests']
            report['status'] = 'passed'
        except BaseException as error:
            report.update(status='failed', error=configure.error_code(error), errorType=type(error).__name__)
            if getattr(app, 'last_error', None):
                report['commandDiagnostic'] = app.last_error
            try:
                report['modelStats'] = model()
                report['jobState'] = read(current) if current else None
                report['workerLogs'] = app.command(docker+['logs', worker()['Id']]).decode(errors='replace')[-20000:]
            except Exception:
                pass
            raise
        finally:
            try:
                cleanup(app, docker, project)
                report['testResourcesRemoved'] = True
            except Exception as error:
                report['testResourcesRemoved'] = False
                report['cleanupError'] = configure.error_code(error)
                raise
            finally:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(dict(status=report['status'], checks=report['checks'], realModelCalls=0, productionChanged=False)), flush=True)


if __name__ == '__main__':
    main()
