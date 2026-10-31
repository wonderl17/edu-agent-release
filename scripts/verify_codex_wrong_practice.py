"""Wrong-question practice lifecycle through the original amd64 Worker.

Synthetic owned sources, actual assistant/domain operations, encrypted outbox,
PG/Redis, deterministic private HTTP model. Never uses a provider or Production.
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
const http=require('http');let hold='blind',requests=[],unknown=[],pending=new Set();
http.createServer((req,res)=>{
 if(req.url==='/stats'){res.end(JSON.stringify({requests,unknown,active:pending.size}));return;}
 if(req.url==='/next'||req.url==='/next-generation'){if(pending.size){res.writeHead(409);res.end('{}');return;}hold=req.url==='/next'?'blind':'generate';res.end('{}');return;}
 if(req.url==='/release'){hold=null;for(const f of [...pending])f();res.end('{}');return;}
 let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{try{
  const b=JSON.parse(raw),system=b.messages?.[0]?.content;
  if(req.url!=='/v1/chat/completions'||b.model!=='gpt-6-luna'||typeof system!=='string')throw Error('Unexpected endpoint or model');
  let kind,items;
  if(system.includes('独立题目质量审查教师')){kind=system.includes('核对给定答案')?'compare':'blind';items=[];}
  else if(system.includes('变式')){
   kind='generate';items=['MISTAKE_FOCUS','ISOMORPHIC','PERSONALIZED_TRANSFER'].map((kind,index)=>({kind,title:'移项练习',questionMarkdown:`解方程 x+${index+1}=6`,answerMarkdown:`x=${5-index}`,analysisMarkdown:'两边同时减去常数。',difficulty:['LOWER','SAME','HIGHER'][index],targetKnowledgeTags:['移项'],mistakeFocus:'符号',personalizationReason:'针对原错题。'}));
  }else throw Error('Unexpected system prompt');
  requests.push({kind,model:b.model});
  const finish=()=>{pending.delete(finish);if(res.destroyed)return;res.setHeader('content-type','application/json');res.end(JSON.stringify({id:'synthetic',model:b.model,choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:JSON.stringify({items})}}],usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}}));};
  pending.add(finish);res.once('close',()=>pending.delete(finish));if(hold!==kind)finish();
 }catch(error){unknown.push(String(error));res.writeHead(400);res.end(JSON.stringify({error:{message:String(error)}}));}});
}).listen(4988,'0.0.0.0');
'''


def main():
    parser = argparse.ArgumentParser()
    for name in ('worker-image', 'runner-image'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'DO_NOT_OVERWRITE_PRIOR_PROOF')
    endpoint = json.loads(subprocess.check_output(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'), 'LOCAL_DOCKER_REQUIRED')
    docker = ['docker', '--host', endpoint]
    tags = dict(api=args.worker_image, worker=args.worker_image, runner=args.runner_image, postgres='postgres:17-alpine', redis='redis:7.4-alpine')
    images = {k: json.loads(subprocess.check_output(docker+['image', 'inspect', v]))[0] for k, v in tags.items()}
    require(all(v['Os'] == 'linux' for v in images.values()) and images['worker']['Architecture'] == 'amd64', 'LINUX_AMD64_REQUIRED')
    policy, configure = load('codex_runtime'), load('codex_configure')
    project = 'codex-wrong-practice-'+uuid.uuid4().hex[:12]
    policy.PROJECT = configure.PROJECT = project
    report = dict(status='incomplete', project=project, checks=[], images={k: v['Id'] for k, v in images.items()}, realModelCalls=0, productionChanged=False,
                  scope='wrong-question practice, actual assistant and domain operations, persistent dispatch, PG/Redis and original Worker',
                  limitations=['Synthetic HTTP responses, no provider inference or content quality claim', 'Raw grading pipeline and production capacity not covered'])
    report['scriptSha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['fixtureSha256'] = hashlib.sha256(MODEL.encode()).hexdigest()

    def passed(name):
        report['checks'].append(name)
        print(json.dumps(dict(check=name, status='passed')), flush=True)

    with tempfile.TemporaryDirectory(prefix='codex-wrong-practice-') as directory:
        root = Path(directory).resolve(); root.chmod(0o700)
        app = Application(root, docker, images, project, policy, configure)
        base = json.loads(app.base_path.read_bytes())
        env = dict(base['services']['api']['environment'], AI_WORKER_ENABLED='true', AI_WORKER_ROLE='general', API_BACKGROUND_TASKS_ENABLED='false',
                   AI_JOB_WORKER_CONCURRENCY='1', AI_JOB_LOCK_DURATION_MS='60000')
        base['services']['worker-general'] = dict(image=images['worker']['Id'], platform='linux/amd64', networks={'app': None}, environment=env, healthcheck={'disable': True})
        path = root/'model.cjs'; path.write_text(MODEL); path.chmod(0o644)
        base['services']['model'] = dict(image=images['worker']['Id'], platform='linux/amd64', networks={'app': None}, entrypoint=['node'],
                                       command=['/fixture/model.cjs'], volumes=[str(path)+':/fixture/model.cjs:ro'], healthcheck={'disable': True})
        app.atomic(app.base_path, base)

        def wait(check, code, seconds=90):
            end = time.monotonic()+seconds
            while time.monotonic() < end:
                result = check()
                if result: return result
                time.sleep(.5)
            raise ValueError(code)

        def model(path='/stats'):
            return json.loads(app.node("fetch('http://model:4988"+path+"').then(async r=>{if(!r.ok)throw Error('MODEL_CONTROL_FAILED');console.log(JSON.stringify(await r.json()))})", 'web'))

        def worker(): return app.inspect()['worker-general']
        def start(): app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', 'worker-general'], timeout=60)

        def queue(code):
            return "const api=require('module').createRequire('/app/apps/api/package.json');const{Queue}=api('bullmq');const{Redis}=api('ioredis');const redis=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});const q=new Queue('teaching-ai-agent-ai-jobs',{connection:redis});try{"+code+"}finally{await q.close();await redis.quit();}"

        def business(code):
            prelude = "const api=require('module').createRequire('/app/apps/api/package.json');api('tsx/cjs/api').register();api('tsx/esm/api').register();const load=p=>require('/app/apps/api/src/'+p);const{prisma:p}=load('lib/prisma.ts');"
            raw = app.node(prelude+"(async()=>{"+code+"})().then(async result=>{await p.$disconnect();process.stdout.write('RESULT:'+JSON.stringify(result)+'\\n');process.exit(0)}).catch(e=>{console.error(e);process.exit(1)})", 'web', timeout=60).decode()
            return json.loads(next(line[7:] for line in reversed(raw.splitlines()) if line.startswith('RESULT:')))

        identities = None

        def seed():
            return business('const userId='+json.dumps(identities['userId'])+',studentId='+json.dumps(identities['studentId'])+';'+r'''
const operationId=require('crypto').randomUUID();
const wrong=await p.wrongQuestion.create({data:{userId,studentId,subject:'数学',displayTitle:'解方程',questionText:'解方程 x+2=5',snapshotJson:{standardAnswer:'x=3',studentAnswer:'x=7'}}});
const session=await p.agentSession.create({data:{userId,title:'错题练习',context:{}}});
const run=await p.agentRun.create({data:{userId,sessionId:session.id,clientRequestId:require('crypto').randomUUID(),requestHash:'synthetic',request:'生成三阶错题练习',context:{},status:'waiting_job',passwordVersion:0,runtimeCipherVersion:1,runtimeCiphertext:Buffer.from('synthetic')}});
await p.agentOperation.create({data:{id:operationId,runId:run.id,stage:'wrong_question_practice',inputHash:'synthetic'}});
const ops=load('modules/agent/agent.grading-followup.operations.ts'),runtime={provider:'openai-compatible',model:'gpt-6-luna',apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'};
const accepted=await ops.createAgentWrongQuestionPractice(userId,{requestId:operationId,studentId,wrongQuestionId:wrong.id},runtime);
await p.agentOperation.update({where:{id:operationId},data:{jobId:accepted.jobId}});
const dispatch=load('queue/lesson-generation-dispatch.ts').dispatchPendingLessonGenerations;
let dispatched=false;
for(let i=0;i<100;i++){
 await dispatch(accepted.jobId);
 const intent=await p.aiJobDispatch.findUniqueOrThrow({where:{jobId:accepted.jobId}});
 if(intent.dispatchedAt){dispatched=true;break;}
 await new Promise(resolve=>setTimeout(resolve,100));
}
if(!dispatched)throw Error('INITIAL_DISPATCH_NOT_ACKNOWLEDGED');
return {id:accepted.jobId,resourceId:accepted.resource.id,runId:run.id};
''')

        def read(job):
            return app.db('const id='+json.dumps(job['id'])+';'+queue(r'''
const db=await p.aiJob.findUniqueOrThrow({where:{id}}),row=await p.wrongQuestionVariantGeneration.findUniqueOrThrow({where:{id:db.relatedEntityId},include:{variants:{orderBy:{orderIndex:'asc'}},homework:{include:{homeworkQuestions:{orderBy:{orderIndex:'asc'}}}}}});
const queued=await q.getJob(id),hash=v=>require('crypto').createHash('sha256').update(JSON.stringify(v)).digest('hex');
console.log(JSON.stringify({status:db.status,error:db.errorMessage,result:db.result,resourceId:row.id,generationStatus:row.status,variants:row.variants,variantsHash:hash(row.variants),metadata:row.llmMetaJson,homework:row.homework,
 inputHash:hash(db.payload),sourceHash:hash(row.sourceJson),updatedAt:row.updatedAt,queueState:queued?await queued.getState():null,attemptsStarted:queued?.attemptsStarted,stalledCounter:queued?.stalledCounter}));
'''), 'web')

        def partial(job, ready=True):
            row = read(job)
            require(row['status'] not in ('FAILED', 'INTERRUPTED', 'SUCCESS'), 'PRACTICE_FINISHED_BEFORE_HELD_MODEL')
            require(not row['homework'], 'HOMEWORK_CREATED_BEFORE_HELD_MODEL')
            if row['status'] == 'PROCESSING' and model()['active'] == 1:
                require(row['generationStatus'] == ('READY' if ready else 'GENERATING') and len(row['variants']) == (3 if ready else 0), 'WRONG_CHECKPOINT_BEFORE_REVIEW')
                return row

        def complete(job):
            row = read(job)
            require(row['status'] not in ('FAILED', 'INTERRUPTED'), 'PRACTICE_GENERATION_FAILED')
            return row if row['status'] == 'SUCCESS' and row['queueState'] == 'completed' else None

        def assert_delivered(job, row):
            homework = row['homework']
            require(row['result']['progress']['percent'] == 100 and row['generationStatus'] == 'READY' and row['resourceId'] == job['resourceId'], 'PRACTICE_DELIVERY_INCOMPLETE')
            require(homework and homework['displayStudent'] and homework['displayAnswer'] and len(homework['homeworkQuestions']) == 3 and len(row['variants']) == 3, 'HOMEWORK_CONTENT_MISSING')
            require(row['result']['homeworkId'] == homework['id'], 'JOB_ARTIFACT_NOT_BOUND')
            require([v['answerMarkdown'] for v in row['variants']] == ['x=5', 'x=4', 'x=3'], 'SAVED_VARIANTS_CHANGED')

        def cancel(job, target):
            if target == 'parent': code = 'await p.agentRun.update({where:{id:'+json.dumps(job['runId'])+'},data:{status:"cancelling",cancelRequested:true}});'
            else: code = 'await p.aiJob.update({where:{id:'+json.dumps(job['id'])+'},data:{status:"CANCEL_REQUESTED"}});'
            app.db(code+'console.log("true")', 'web')

        def interrupted(job):
            row = read(job)
            return row if row['status'] == 'INTERRUPTED' and row['queueState'] == 'completed' else None

        current = None
        try:
            app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '60', 'postgres', 'redis', 'web', 'model'], timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n)", 'web') == 0, 'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','migrate','deploy'],{stdio:'inherit'})", 'web', timeout=120)
            identities = app.db("const user=await p.user.create({data:{email:'synthetic-practice@example.invalid'}});const student=await p.student.create({data:{userId:user.id,name:'合成学生'}});console.log(JSON.stringify({userId:user.id,studentId:student.id}))", 'web')
            model('/next'); current = initial = seed(); start()
            first = wait(lambda: partial(initial), 'INITIAL_PRACTICE_NOT_PENDING')
            before = model()['requests']
            require([r['kind'] for r in before] == ['generate', 'blind'], 'INITIAL_MODEL_BOUNDARY_UNEXPECTED')
            passed('assistant-domain-operation-saves-three-variants-before-optional-review')
            identity, started = worker()['Id'], time.monotonic()
            app.command(docker+['kill', '--signal', 'SIGTERM', identity])
            wait(lambda: not worker()['State']['Running'], 'WORKER_DID_NOT_EXIT', 55)
            require(worker()['State']['ExitCode'] == 1 and not worker()['State']['OOMKilled'], 'UNCLEAN_STOP_NOT_REPORTED')
            stopped = read(initial)
            require(stopped['status'] == 'PROCESSING' and stopped['queueState'] == 'active' and stopped['variantsHash'] == first['variantsHash'] and not stopped['homework'], 'SHUTDOWN_LOST_CHECKPOINT')
            report['shutdownSeconds'] = round(time.monotonic()-started, 3)
            model('/release'); start()
            resumed = wait(lambda: complete(initial), 'STALLED_PRACTICE_DID_NOT_RESUME', 170); assert_delivered(initial, resumed)
            require(resumed['attemptsStarted'] == 2 and resumed['stalledCounter'] == 1, 'REAL_STALLED_RETRY_NOT_OBSERVED')
            require(resumed['inputHash'] == first['inputHash'] and resumed['sourceHash'] == first['sourceHash'] and resumed['variantsHash'] == first['variantsHash'], 'FROZEN_INPUT_OR_VARIANTS_CHANGED')
            require(model()['requests'] == before, 'RESUME_REPEATED_COMPLETED_MODEL_WORK')
            passed('timeout-stalled-retry-reuses-saved-variants-and-delivers-without-http')
            count = len(model()['requests'])
            duplicate = app.db('const id='+json.dumps(initial['id'])+';'+queue("const old=await q.getJob(id),duplicateId=require('crypto').randomUUID();await q.add(old.name,old.data,{jobId:duplicateId,removeOnComplete:false});console.log(JSON.stringify(duplicateId));"), 'web')
            wait(lambda: app.db(queue('const j=await q.getJob('+json.dumps(duplicate)+');console.log(JSON.stringify(await j.getState()))'), 'web') == 'completed', 'DUPLICATE_NOT_SETTLED')
            require(read(initial) == resumed and len(model()['requests']) == count, 'DUPLICATE_CHANGED_DELIVERY')
            passed('duplicate-queue-delivery-does-not-regenerate-or-change-artifacts')
            model('/next'); current = active_cancel = seed(); saved = wait(lambda: partial(active_cancel), 'CANCEL_CASE_NOT_PENDING')
            cancel(active_cancel, 'parent'); stopped = wait(lambda: interrupted(active_cancel), 'PARENT_CANCEL_NOT_HONORED')
            require(not stopped['homework'] and stopped['variantsHash'] == saved['variantsHash'] and stopped['generationStatus'] == 'READY', 'CANCEL_LOST_VARIANTS_OR_DELIVERED_LATE')
            wait(lambda: model()['active'] == 0, 'CANCELLED_REQUEST_STILL_ACTIVE')
            passed('parent-cancel-aborts-review-preserves-variants-and-prevents-homework')
            cancelled = [active_cancel]
            for target, ready in [('parent', True), ('job', True), ('job', False)]:
                model('/next' if ready else '/next-generation'); current = offline = seed(); saved = wait(lambda: partial(offline, ready), 'OUTAGE_CASE_NOT_PENDING')
                app.command(docker+['kill', '--signal', 'SIGKILL', worker()['Id']]); wait(lambda: not worker()['State']['Running'], 'WORKER_NOT_KILLED')
                require(worker()['State']['ExitCode'] == 137 and not worker()['State']['OOMKilled'], 'ABRUPT_EXIT_NOT_OBSERVED')
                wait(lambda: model()['active'] == 0, 'KILLED_REQUEST_STILL_ACTIVE'); count = len(model()['requests']); cancel(offline, target)
                start(); stopped = wait(lambda: interrupted(offline), 'OUTAGE_CANCEL_NOT_SETTLED', 170)
                require(not stopped['homework'] and stopped['variantsHash'] == saved['variantsHash'] and stopped['inputHash'] == saved['inputHash'], 'OUTAGE_CHANGED_SAVED_WORK')
                require(stopped['generationStatus'] == ('READY' if ready else 'FAILED'), 'CANCELLED_GENERATION_NOT_SETTLED')
                require(stopped['stalledCounter'] == 1 and stopped['attemptsStarted'] == 2 and len(model()['requests']) == count, 'OUTAGE_CANCEL_REGENERATED')
                cancelled.append(offline)
                passed(target+'-outage-cancel-settles-stalled-delivery-ready-'+str(ready).lower())
            # Stop the idle Worker, then cancel an already enqueued job before
            # its first processor invocation. Queue guard must settle the source.
            identity = worker()['Id']; app.command(docker+['kill', '--signal', 'SIGTERM', identity])
            wait(lambda: not worker()['State']['Running'], 'IDLE_WORKER_DID_NOT_STOP', 55)
            require(worker()['State']['ExitCode'] == 0, 'IDLE_DRAIN_FAILED')
            current = queued = seed(); count = len(model()['requests']); cancel(queued, 'job'); start()
            stopped = wait(lambda: interrupted(queued), 'QUEUED_CANCEL_NOT_SETTLED')
            require(stopped['generationStatus'] == 'FAILED' and not stopped['homework'] and not stopped['variants'] and len(model()['requests']) == count, 'QUEUED_CANCEL_STARTED_GENERATION')
            cancelled.append(queued); passed('job-cancel-before-first-processor-settles-placeholder-without-http')
            model('/next'); current = clean = seed(); saved = wait(lambda: partial(clean), 'CLEAN_DRAIN_NOT_PENDING')
            identity = worker()['Id']; app.command(docker+['kill', '--signal', 'SIGTERM', identity])
            wait(lambda: b'SIGTERM, closing' in app.command(docker+['logs', identity]), 'SIGNAL_NOT_HANDLED'); model('/release')
            wait(lambda: not worker()['State']['Running'], 'CLEAN_DRAIN_DID_NOT_EXIT', 55)
            require(worker()['State']['ExitCode'] == 0, 'CLEAN_DRAIN_FAILED'); delivered = wait(lambda: complete(clean), 'CLEAN_DRAIN_DID_NOT_DELIVER'); assert_delivered(clean, delivered)
            require(delivered['variantsHash'] == saved['variantsHash'] and delivered['metadata']['semanticReview']['status'] == 'unavailable', 'OPTIONAL_REVIEW_CHANGED_QUESTIONS')
            require(all(read(job)['status'] == 'INTERRUPTED' for job in cancelled), 'CANCELLED_PRACTICE_REVIVED')
            passed('graceful-drain-delivers-despite-unavailable-review-and-preserves-cancellations')
            report['sourceHashes'] = json.loads(app.node("const f=require('fs'),c=require('crypto');const names=['src/lib/agent-job-cancellation.ts','src/lib/agent-generation-intent.ts','src/modules/agent/agent.grading-followup.operations.ts','src/modules/student/student-wrong-question-variant.repo.ts','src/modules/student/student-wrong-question-variant.service.ts','src/modules/student/wrong-question-variant-review.ts','src/queue/ai-jobs-worker-factory.ts','src/queue/lesson-generation-dispatch.ts'];console.log(JSON.stringify(Object.fromEntries(names.map(n=>[n,c.createHash('sha256').update(f.readFileSync('/app/apps/api/'+n)).digest('hex')]))))", 'web'))
            require(not model()['unknown'], 'UNEXPECTED_MODEL_REQUESTS')
            report['fixtureRequests'] = model()['requests']; report['status'] = 'passed'
        except BaseException as error:
            report.update(status='failed', error=configure.error_code(error), errorType=type(error).__name__)
            if getattr(app, 'last_error', None): report['commandDiagnostic'] = app.last_error
            try:
                report['modelStats'] = model(); report['jobState'] = read(current) if current else None
                report['workerLogs'] = subprocess.check_output(docker+['logs', worker()['Id']], stderr=subprocess.STDOUT, timeout=15).decode(errors='replace')[-24000:]
            except Exception: pass
            raise
        finally:
            try: cleanup(app, docker, project); report['testResourcesRemoved'] = True
            except Exception as error: report['testResourcesRemoved'] = False; report['cleanupError'] = configure.error_code(error); raise
            finally:
                args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(dict(status=report['status'], checks=report['checks'], realModelCalls=0, productionChanged=False)), flush=True)


if __name__ == '__main__': main()
