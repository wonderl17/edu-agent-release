"""Actual report and unsent communication Workers and queue lifecycle.

Uses synthetic students, assistant support operations, durable dispatch,
real PG/Redis and original amd64 Worker. HTTP model is a deterministic fixture.
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
const http=require('http');let hold=true,requests=[],unknown=[],pending=new Set();
http.createServer((req,res)=>{
 if(req.url==='/stats'){res.end(JSON.stringify({requests,unknown,active:pending.size}));return;}
 if(req.url==='/next'){if(pending.size){res.writeHead(409);res.end('{}');return;}hold=true;res.end('{}');return;}
 if(req.url==='/release'){hold=false;for(const f of [...pending])f();res.end('{}');return;}
 let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{try{
  const b=JSON.parse(raw),system=b.messages?.[0]?.content;
  if(req.url!=='/v1/chat/completions'||b.model!=='gpt-6-luna'||typeof system!=='string')throw Error('Unexpected endpoint or model');
  let kind,content;
  if(system.includes('你是资深课后服务顾问')){
   kind='report';content=JSON.stringify({stageSummary:'报告恢复验收标记：当前记录不足。',visibleProgress:'尚需补充学习记录。',currentFocus:'建议先补充课堂观察。',teacherWork:['建议核对学习记录。'],nextStagePlan:[{title:'补充记录',plan:'下次课记录实际表现。',expectedResult:'形成可追踪证据。'}],parentCooperation:['建议记录练习情况。'],renewalValue:'建议持续跟踪实际学习表现。'});
  }else if(system.includes('你是教师的家长沟通草稿撰写助手')){
   kind='draft';const input=JSON.stringify(b.messages);if(!input.includes('报告恢复验收标记')||input.includes('受理后的来源改写标记'))throw Error('Draft did not use its frozen report');
   content='家长您好，目前学习记录还不充分，建议下次课补充实际表现，再一起确定后续练习安排。';
  }
  else throw Error('Unexpected system prompt');
  requests.push({kind,model:b.model});
  const finish=()=>{pending.delete(finish);if(res.destroyed)return;res.setHeader('content-type','application/json');res.end(JSON.stringify({id:'synthetic',model:b.model,choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content}}],usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}}));};
  pending.add(finish);res.once('close',()=>pending.delete(finish));if(!hold)finish();
 }catch(error){unknown.push(String(error));res.writeHead(400);res.end(JSON.stringify({error:{message:String(error)}}));}});
}).listen(4988,'0.0.0.0');
'''


def main():
    parser = argparse.ArgumentParser()
    for name in ('worker-image', 'runner-image'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    endpoint = json.loads(subprocess.check_output(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'), 'LOCAL_DOCKER_REQUIRED')
    docker = ['docker', '--host', endpoint]
    tags = dict(api=args.worker_image, worker=args.worker_image, runner=args.runner_image, postgres='postgres:17-alpine', redis='redis:7.4-alpine')
    images = {k: json.loads(subprocess.check_output(docker+['image', 'inspect', v]))[0] for k, v in tags.items()}
    require(all(v['Os'] == 'linux' for v in images.values()) and images['worker']['Architecture'] == 'amd64', 'LINUX_AMD64_REQUIRED')
    policy, configure = load('codex_runtime'), load('codex_configure')
    project = 'codex-student-support-'+uuid.uuid4().hex[:12]
    policy.PROJECT = configure.PROJECT = project
    report = dict(status='incomplete', project=project, checks=[], images={k: v['Id'] for k, v in images.items()}, realModelCalls=0, productionChanged=False,
                  scope='student report and unsent draft, actual assistant operations and domain generators, persistent dispatch, PG/Redis and original Worker',
                  limitations=['No ASR, original audio, provider inference, content-quality or production capacity claim'])
    report['scriptSha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['fixtureSha256'] = hashlib.sha256(MODEL.encode()).hexdigest()

    def passed(name):
        report['checks'].append(name)
        print(json.dumps(dict(check=name, status='passed')), flush=True)

    with tempfile.TemporaryDirectory(prefix='codex-student-support-') as directory:
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
            return json.loads(app.node("fetch('http://model:4988"+path+"').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))", 'web'))

        def worker(): return app.inspect()['worker-general']

        def start(): app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', 'worker-general'], timeout=60)

        def queue(code):
            return "const api=require('module').createRequire('/app/apps/api/package.json');const{Queue}=api('bullmq');const{Redis}=api('ioredis');const redis=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});const q=new Queue('teaching-ai-agent-ai-jobs',{connection:redis});try{"+code+"}finally{await q.close();await redis.quit();}"

        def business(code):
            prelude = "const api=require('module').createRequire('/app/apps/api/package.json');api('tsx/cjs/api').register();api('tsx/esm/api').register();const load=p=>require('/app/apps/api/src/'+p);const{prisma:p}=load('lib/prisma.ts');"
            raw = app.node(prelude+"(async()=>{"+code+"})().then(async result=>{await p.$disconnect();process.stdout.write('RESULT:'+JSON.stringify(result)+'\\n');process.exit(0)}).catch(e=>{console.error(e);process.exit(1)})", 'web', timeout=60).decode()
            return json.loads(next(line[7:] for line in reversed(raw.splitlines()) if line.startswith('RESULT:')))

        report_source = None

        def seed(kind):
            return business('const kind='+json.dumps(kind)+',source='+json.dumps(report_source)+';'+r'''
const operationId=require('crypto').randomUUID();
const session=await p.agentSession.create({data:{userId:'teacher',title:'报告与沟通',context:{}}});
const run=await p.agentRun.create({data:{userId:'teacher',sessionId:session.id,clientRequestId:require('crypto').randomUUID(),requestHash:'synthetic',request:'先报告后家长沟通草稿',context:{},status:'waiting_job',passwordVersion:0,runtimeCipherVersion:1,runtimeCiphertext:Buffer.from('synthetic')}});
await p.agentOperation.create({data:{id:operationId,runId:run.id,stage:kind==='report'?'student_report':'communication_draft',inputHash:'synthetic'}});
const ops=load('modules/agent/agent.student-support.operations.ts'),runtime={provider:'openai-compatible',model:'gpt-6-luna',apiKey:'synthetic-no-provider',baseUrl:'http://model:4988/v1'};
if(kind==='draft')await p.studentReport.update({where:{id:source.id},data:{displayContent:source.content}});
const accepted=kind==='report'?await ops.createAgentStudentReport('teacher',{requestId:operationId,studentId:'student',reportType:'parent_stage_report',requirements:'近七天的阶段报告，说明已有证据与限制'},runtime):await ops.createAgentCommunicationDraft('teacher',{requestId:operationId,studentId:'student',reportId:source.id,requirements:'请根据已保存报告生成家长沟通草稿'},runtime);
if(kind==='draft')await p.studentReport.update({where:{id:source.id},data:{displayContent:'受理后的来源改写标记'}});
await p.agentOperation.update({where:{id:operationId},data:{jobId:accepted.jobId}});
const dispatch=load('queue/lesson-generation-dispatch.ts').dispatchPendingLessonGenerations;
// The create entry point starts its own asynchronous dispatch. A second caller
// can observe that lease and return before it finishes; keep this harness alive.
let dispatched=false;
for(let i=0;i<100;i++){
 await dispatch(accepted.jobId);
 const intent=await p.aiJobDispatch.findUniqueOrThrow({where:{jobId:accepted.jobId}});
 if(intent.dispatchedAt){dispatched=true;break;}
 await new Promise(resolve=>setTimeout(resolve,100));
}
if(!dispatched)throw Error('INITIAL_DISPATCH_NOT_ACKNOWLEDGED');
return {kind,id:accepted.jobId,resourceId:accepted.resource.id,runId:run.id};
''')

        def read(job):
            return app.db('const id='+json.dumps(job['id'])+';'+queue(r'''
const db=await p.aiJob.findUniqueOrThrow({where:{id}}),isReport=db.type==='REPORT_STUDENT_GENERATE';
const row=isReport?await p.studentReport.findUniqueOrThrow({where:{id:db.payload.agentArtifact.reportId}}):await p.communicationDraft.findUniqueOrThrow({where:{id:db.payload.draftId}});
const queued=await q.getJob(id),hash=v=>require('crypto').createHash('sha256').update(JSON.stringify(v)).digest('hex');
console.log(JSON.stringify({status:db.status,error:db.errorMessage,result:db.result,resourceId:row.id,content:isReport?row.displayContent:row.body,metadata:isReport?row.contentJson:row.contextJson,
 inputHash:hash(db.payload),sourceReportHash:db.payload.reportSnapshot?.sha256,contentHash:hash(isReport?row.displayContent:row.body),updatedAt:row.updatedAt,
 queueState:queued?await queued.getState():null,attemptsStarted:queued?.attemptsStarted,stalledCounter:queued?.stalledCounter}));
'''), 'web')

        def partial(job):
            row = read(job)
            require(row['status'] not in ('FAILED', 'INTERRUPTED', 'SUCCESS'), 'SUPPORT_FINISHED_BEFORE_PENDING_MODEL')
            require(not row['content'], 'SUPPORT_SAVED_BEFORE_MODEL_RESPONSE')
            return row if row['status'] == 'PROCESSING' and model()['active'] == 1 else None

        def complete(job):
            row = read(job)
            require(row['status'] not in ('FAILED', 'INTERRUPTED'), 'SUPPORT_GENERATION_FAILED')
            return row if row['status'] == 'SUCCESS' and row['queueState'] == 'completed' else None

        def assert_delivered(job, row):
            require(row['result']['progress']['percent'] == 100 and row['content'] and row['resourceId'] == job['resourceId'], 'SUPPORT_DELIVERY_INCOMPLETE')
            if job['kind'] == 'report': require(row['metadata']['llmStatus'] == 'READY' and '报告恢复验收标记' in row['content'], 'REPORT_NOT_GENERATED')
            else: require(row['metadata']['lifecycle']['sent'] is False and row['metadata']['sourceReport']['sha256'] == row['sourceReportHash'], 'DRAFT_SENT_OR_SOURCE_CHANGED')

        current = None
        try:
            app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '60', 'postgres', 'redis', 'web', 'model'], timeout=90)
            require(app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS n FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].n)", 'web') == 0, 'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','migrate','deploy'],{stdio:'inherit'})", 'web', timeout=120)
            app.db("await p.user.create({data:{id:'teacher',email:'synthetic-text@example.invalid'}});await p.student.create({data:{id:'student',userId:'teacher',name:'合成学生'}});console.log('true')", 'web')
            for kind in ('report', 'draft'):
                model('/next'); current = initial = seed(kind); start()
                first = wait(lambda: partial(initial), 'INITIAL_SUPPORT_NOT_PENDING')
                before = model()['requests']
                passed(kind+'-assistant-domain-operation-dispatches-real-worker')
                identity, started = worker()['Id'], time.monotonic()
                app.command(docker+['kill', '--signal', 'SIGTERM', identity])
                wait(lambda: not worker()['State']['Running'], 'WORKER_DID_NOT_EXIT', 55)
                require(worker()['State']['ExitCode'] == 1 and not worker()['State']['OOMKilled'], 'UNCLEAN_STOP_NOT_REPORTED')
                stopped = read(initial)
                require(stopped['status'] == 'PROCESSING' and stopped['queueState'] == 'active' and stopped['inputHash'] == first['inputHash'] and not stopped['content'], 'SHUTDOWN_LOST_STATE_OR_INTENT')
                report[kind+'ShutdownSeconds'] = round(time.monotonic()-started, 3)
                model('/release'); start()
                resumed = wait(lambda: complete(initial), 'STALLED_SUPPORT_DID_NOT_RESUME', 170); assert_delivered(initial, resumed)
                require(resumed['attemptsStarted'] == 2 and resumed['stalledCounter'] == 1, 'REAL_STALLED_RETRY_NOT_OBSERVED')
                require(resumed['inputHash'] == first['inputHash'], 'FROZEN_INPUT_CHANGED')
                if kind == 'report': report_source = dict(id=initial['resourceId'], content=resumed['content'])
                require(len(model()['requests']) == len(before)+1, 'RESUME_REPEATED_FINISHED_MODEL_WORK')
                passed(kind+'-timeout-stalled-retry-preserves-completed-work-and-delivers-once')
                count = len(model()['requests'])
                duplicate = app.db('const id='+json.dumps(initial['id'])+';'+queue("const old=await q.getJob(id),duplicateId=require('crypto').randomUUID();await q.add(old.name,old.data,{jobId:duplicateId,removeOnComplete:false});console.log(JSON.stringify(duplicateId));"), 'web')
                wait(lambda: app.db(queue('const j=await q.getJob('+json.dumps(duplicate)+');console.log(JSON.stringify(await j.getState()))'), 'web') == 'completed', 'DUPLICATE_NOT_SETTLED')
                require(read(initial) == resumed and len(model()['requests']) == count, 'DUPLICATE_CHANGED_DELIVERY')
                passed(kind+'-duplicate-queue-delivery-does-not-regenerate-or-change-content')
                model('/next'); current = cancelled = seed(kind); saved = wait(lambda: partial(cancelled), 'CANCEL_CASE_NOT_PENDING')
                app.db('await p.agentRun.update({where:{id:'+json.dumps(cancelled['runId'])+'},data:{status:"cancelling",cancelRequested:true}});console.log("true")', 'web')
                stopped = wait(lambda: (lambda r: r if r['status'] == 'INTERRUPTED' and r['queueState'] == 'completed' else None)(read(cancelled)), 'PARENT_CANCEL_NOT_HONORED')
                require(not stopped['content'] and stopped['inputHash'] == saved['inputHash'], 'CANCEL_LOST_TEXT_OR_DELIVERED_LATE')
                wait(lambda: model()['active'] == 0, 'CANCELLED_REQUEST_STILL_ACTIVE')
                passed(kind+'-parent-cancel-aborts-http-without-saving-late-content')
                model('/next'); current = offline = seed(kind); saved = wait(lambda: partial(offline), 'OUTAGE_CASE_NOT_PENDING')
                app.command(docker+['kill', '--signal', 'SIGKILL', worker()['Id']]); wait(lambda: not worker()['State']['Running'], 'WORKER_NOT_KILLED')
                require(worker()['State']['ExitCode'] == 137 and not worker()['State']['OOMKilled'], 'ABRUPT_EXIT_NOT_OBSERVED')
                wait(lambda: model()['active'] == 0, 'KILLED_REQUEST_STILL_ACTIVE'); count = len(model()['requests'])
                app.db('await p.agentRun.update({where:{id:'+json.dumps(offline['runId'])+'},data:{status:"cancelling",cancelRequested:true}});console.log("true")', 'web')
                start(); stopped = wait(lambda: (lambda r: r if r['queueState'] == 'completed' else None)(read(offline)), 'OUTAGE_CANCEL_NOT_SETTLED', 170)
                require(stopped['status'] == 'INTERRUPTED' and not stopped['content'] and stopped['inputHash'] == saved['inputHash'], 'OUTAGE_SUPPORT_STATE_INCORRECT')
                require(stopped['stalledCounter'] == 1 and stopped['attemptsStarted'] == 2 and len(model()['requests']) == count, 'OUTAGE_CANCEL_REGENERATED')
                passed(kind+'-outage-parent-cancel-settles-on-stalled-redelivery-without-http')
                model('/next'); current = clean = seed(kind); wait(lambda: partial(clean), 'CLEAN_DRAIN_NOT_PENDING')
                identity = worker()['Id']; app.command(docker+['kill', '--signal', 'SIGTERM', identity])
                wait(lambda: b'SIGTERM, closing' in app.command(docker+['logs', identity]), 'SIGNAL_NOT_HANDLED'); model('/release')
                wait(lambda: not worker()['State']['Running'], 'CLEAN_DRAIN_DID_NOT_EXIT', 55)
                require(worker()['State']['ExitCode'] == 0, 'CLEAN_DRAIN_FAILED'); assert_delivered(clean, wait(lambda: complete(clean), 'CLEAN_DRAIN_DID_NOT_DELIVER'))
                require(read(cancelled)['status'] == 'INTERRUPTED' and read(offline)['status'] == 'INTERRUPTED', 'CANCELLED_SUPPORT_REVIVED')
                passed(kind+'-graceful-drain-delivers-content-and-preserves-prior-cancellations')
            report['sourceHashes'] = json.loads(app.node("const f=require('fs'),c=require('crypto');const names=['src/lib/agent-job-cancellation.ts','src/lib/agent-generation-intent.ts','src/modules/agent/agent.student-support.operations.ts','src/modules/learning-report/learning-report.service.ts','src/modules/learning-report/learning-report.llm.ts','src/modules/smart-reply/smart-reply.service.ts','src/queue/ai-jobs-worker-factory.ts'];console.log(JSON.stringify(Object.fromEntries(names.map(n=>[n,c.createHash('sha256').update(f.readFileSync('/app/apps/api/'+n)).digest('hex')]))))", 'web'))
            require(not model()['unknown'], 'UNEXPECTED_MODEL_REQUESTS')
            report['fixtureRequests'] = model()['requests']; report['status'] = 'passed'
        except BaseException as error:
            report.update(status='failed', error=configure.error_code(error), errorType=type(error).__name__)
            if getattr(app, 'last_error', None): report['commandDiagnostic'] = app.last_error
            try:
                report['modelStats'] = model(); report['jobState'] = read(current) if current else None
                report['workerLogs'] = app.command(docker+['logs', worker()['Id']]).decode(errors='replace')[-20000:]
            except Exception: pass
            raise
        finally:
            try: cleanup(app, docker, project); report['testResourcesRemoved'] = True
            except Exception as error: report['testResourcesRemoved'] = False; report['cleanupError'] = configure.error_code(error); raise
            finally:
                args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(dict(status=report['status'], checks=report['checks'], realModelCalls=0, productionChanged=False)), flush=True)


if __name__ == '__main__': main()
