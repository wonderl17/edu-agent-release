"""Local original-package schema upgrade and API/Worker rollback proof.

Uses a deployed manifest's immutable amd64 images, real Prisma migrations and
original entrypoints. All data and model HTTP are synthetic. This is not a full
release: candidate Web, signed packaging, root installation and public TLS are
not exercised. No production access or credentials are accepted.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import types
import uuid

from verify_codex_application import Application, load, require
from verify_codex_workers import MODEL


def cleanup(app,docker,project):
    app.command(app.compose_argv()+['down','--volumes','--remove-orphans','--timeout','45'],timeout=150)
    scope='label=com.docker.compose.project='+project
    # The restored old override no longer declares the candidate runtime
    # network. Compose down cannot discover it from that configuration.
    networks=app.command(docker+['network','ls','-q','--filter',scope]).decode().split()
    if networks:
        records=json.loads(app.command(docker+['network','inspect',*networks]))
        require(len(records)==len(networks)and all(record['Labels'].get('com.docker.compose.project')==project and
                record['Name'].startswith(project+'_')and record['Internal']is True and not record['Containers']
                for record in records),'LOCAL_NETWORK_CLEANUP_SCOPE')
        app.command(docker+['network','rm',*networks])
    remaining={kind:app.command(docker+argv+['--filter',scope]).decode().split()for kind,argv in
               [('containers',['ps','-aq']),('networks',['network','ls','-q']),('volumes',['volume','ls','-q'])]}
    require(not any(remaining.values()),'LOCAL_RESOURCES_REMAIN')
    return remaining


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--api-image',required=True)
    parser.add_argument('--worker-image',required=True)
    parser.add_argument('--runner-image',required=True)
    parser.add_argument('--old-image-proof',type=Path,required=True)
    parser.add_argument('--queue-probe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for kind in ('api','worker','runner'):
        value=getattr(args,kind+'_image');tag='runtime'if kind=='runner'else kind
        require(re.fullmatch('teaching-codex-'+tag+r':[a-z0-9-]+',value),'LOCAL_CANDIDATE_IMAGE_REQUIRED')
    old=json.loads(args.old_image_proof.read_bytes())
    require(old['status']=='passed'and {x['kind']for x in old['images']}=={'api','worker','web'},'VERIFIED_OLD_PACKAGE_REQUIRED')
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED')
    docker=['docker','--host',endpoint]
    def inspect_image(ref):return json.loads(subprocess.check_output(docker+['image','inspect',ref]))[0]
    images={key:inspect_image(ref)for key,ref in dict(api=args.api_image,worker=args.worker_image,runner=args.runner_image,
                                                  postgres='postgres:17-alpine',redis='redis:7.4-alpine').items()}
    previous={}
    for item in old['images']:
        require(re.fullmatch('docker.cnb.cool/shukezhinao/edu-agent-cloud-build/'+item['kind']+r'@sha256:[0-9a-f]{64}',item['ref']),
                'IMMUTABLE_OLD_PACKAGE_REQUIRED')
        image=inspect_image(item['ref']);labels=image['Config']['Labels']
        require(image['Id']==item['id']and item['ref']in image['RepoDigests']and
                labels['org.opencontainers.image.revision']==item['sourceSha']and labels['org.opencontainers.image.version']==item['version'],
                'OLD_PACKAGE_IDENTITY_CHANGED')
        previous[item['kind']]=image
    require(all(x['Os']=='linux'and x['Architecture']=='amd64'for x in [*previous.values(),images['api'],images['worker'],images['runner']]),
            'AMD64_APPLICATION_IMAGES_REQUIRED')
    report=dict(status='incomplete',checks=[],productionChanged=False,modelCalls=0,cnbBuildsTriggered=False,
                previousImages={k:v['Id']for k,v in previous.items()},candidateImages={k:images[k]['Id']for k in ('api','worker','runner')},
                limitations=['local uncommitted candidate; version policy and signed release packaging are not exercised',
                             'old packaged Web is served; candidate Web/public TLS/object storage are not exercised',
                             'fresh synthetic database, not a production data migration or full compatibility matrix',
                             'receiver rollback drain is real; complete receiver transfer/activation control flow is covered separately'])
    report['scriptSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['receiverSha256']=hashlib.sha256((Path(__file__).resolve().parents[1]/'server/tencent_receiver.py').read_bytes()).hexdigest()
    report['queueProbeSha256']=hashlib.sha256(args.queue_probe.read_bytes()).hexdigest()
    configure,policy,receiver=load('codex_configure'),load('codex_runtime'),load('tencent_receiver')
    project='codex-recovery-'+uuid.uuid4().hex[:12];report['project']=project
    configure.PROJECT=policy.PROJECT=project
    require(not subprocess.check_output(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip(),'PROJECT_EXISTS')
    def passed(name,**details):
        report['checks'].append(dict(name=name,**details));print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-package-recovery-')as directory:
        root=Path(directory).resolve();root.chmod(0o700)
        app=Application(root,docker,images,project,policy,configure)
        base=json.loads(app.base_path.read_bytes());env=base['services']['api']['environment']
        base['services']={k:v for k,v in base['services'].items()if k in ('api','postgres','redis')}
        base['services']['api']['image']=previous['api']['Id']
        for role in ('general','ppt'):
            worker_env=dict(env,AI_WORKER_ENABLED='true',AI_WORKER_ROLE=role,API_BACKGROUND_TASKS_ENABLED='false',
                            COMPOSE_PROJECT_NAME='teaching-production',PRODUCTION_ACTIVATED='true',TEACHING_DEPLOYMENT_ENV='shukezhinao-prod')
            base['services']['worker-'+role]=dict(image=previous['worker']['Id'],platform='linux/amd64',networks={'app':None},
                                                environment=worker_env,healthcheck={'disable':True})
        base['services']['web']=dict(image=previous['web']['Id'],platform='linux/amd64',networks={'app':None})
        base['services']['control']=dict(image=images['api']['Id'],platform='linux/amd64',networks={'app':None},environment=env,
                                        entrypoint=['node'],command=['-e','setInterval(()=>{},1000)'],healthcheck={'disable':True})
        model=root/'model.cjs';model.write_text(MODEL);model.chmod(0o644)
        base['services']['model']=dict(image=images['api']['Id'],platform='linux/amd64',networks={'app':None},entrypoint=['node'],
                                      command=['/fixture/model.cjs'],volumes=[str(model)+':/fixture/model.cjs:ro'],healthcheck={'disable':True})
        app.atomic(app.base_path,base);app.atomic(app.OVERRIDE,{'services':{}})
        (root/'queue-probe.mjs').write_bytes(args.queue_probe.read_bytes())
        receiver.DOCKER=docker;receiver.OPS=root;receiver.command=app.command
        receiver.ops=types.SimpleNamespace(inspect=app.inspect)
        def wait_for(check,code,seconds=90):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                result=check()
                if result:return result
                time.sleep(.5)
            raise ValueError(code)
        def node(script,service='control'):return app.node(script,service)
        def db(script,service='control'):return app.db(script,service)
        def model_http(path='/stats'):
            return json.loads(node("fetch('http://model:4988"+path+"').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))"))
        def migrate(image,label):
            # A dedicated Compose service reuses only this fresh database's env.
            migration=copy.deepcopy(base['services']['control']);migration.update(image=image,entrypoint=['/usr/local/bin/pnpm'],
                command=['exec','prisma','migrate','deploy','--schema','/app/prisma/schema.prisma'])
            value=json.loads(app.base_path.read_bytes());value['services']['migration']=migration;app.atomic(app.base_path,value)
            output=app.command(app.compose_argv()+['run','--rm','--no-deps','-T','migration'],timeout=300)
            (root/(label+'.log')).write_bytes(output)
            rows=db('console.log(JSON.stringify(await p.$queryRawUnsafe(\'SELECT migration_name, finished_at IS NOT NULL AS finished, rolled_back_at IS NOT NULL AS rolled_back FROM "_prisma_migrations" ORDER BY migration_name\')));')
            require(rows and all(row['finished']and not row['rolled_back']for row in rows),'MIGRATION_NOT_APPLIED')
            return rows
        def health():
            response=json.loads(node("fetch('http://api:4999/health').then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()})))"))
            require(response['status']==200,'API_HEALTH_FAILED')
        def consumers():
            script="const r=require('module').createRequire('/app/apps/api/package.json');const{Queue}=r('bullmq'),{Redis}=r('ioredis');const c=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});(async()=>{const out=[];for(const name of ['teaching-ai-agent-ai-jobs','teaching-ai-agent-ppt-jobs']){const q=new Queue(name,{connection:c});out.push((await q.getWorkers()).length);await q.close()}await c.quit();console.log(JSON.stringify(out))})()"
            return json.loads(node(script))
        def up_apps():
            if 'codex-agent-runtime'in app.config()['services']:
                app.command(app.compose_argv()+['up','-d','--no-deps','--no-build','--pull','never','--wait','--wait-timeout','90','codex-agent-runtime'],timeout=120)
            app.command(app.compose_argv()+['up','-d','--no-deps','--no-build','--pull','never','--force-recreate','--wait','--wait-timeout','90','api','worker-general','worker-ppt','web'],timeout=120)
            health();wait_for(lambda:all(n==1 for n in consumers()),'WORKER_CONSUMERS_NOT_READY')
        def seed_job(label,proposal):
            return db("const label="+json.dumps(label)+";const proposal="+json.dumps(proposal)+";"+r"""
const h=await p.homework.create({data:{userId:'teacher',questionsJson:{},answersJson:{},displayStudent:'1. original',displayAnswer:'1. original answer'}});
const payload={prompt:'synthetic-'+label,target:'both',...(proposal?{reviewMode:'proposal',clientRequestId:crypto.randomUUID(),baseUpdatedAt:h.updatedAt.toISOString()}:{})};
const job=await p.aiJob.create({data:{userId:'teacher',type:'HOMEWORK_ADJUST',relatedEntityId:h.id,payload}});
const r=requireApi,{Queue}=r('bullmq'),{Redis}=r('ioredis');const c=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});
const q=new Queue('teaching-ai-agent-ai-jobs',{connection:c});
try{await q.add('HOMEWORK_ADJUST',{aiJobId:job.id,userId:'teacher',homeworkId:h.id,lessonPlanId:null,
 llmOverrides:{provider:'openai-compatible',model:'gpt-6-luna',baseUrl:'http://model:4988/v1',apiKey:'synthetic-no-provider'}},{jobId:job.id,attempts:1,removeOnComplete:false});}
finally{await q.close();await c.quit()}
console.log(JSON.stringify({id:job.id,homeworkId:h.id}));
""")
        def read_job(job):
            return db('console.log(JSON.stringify(await p.aiJob.findUniqueOrThrow({where:{id:'+json.dumps(job['id'])+'}})));')
        started=False
        try:
            started=True
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60','postgres','redis','control','model'],timeout=90)
            require(db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS count FROM information_schema.tables WHERE table_schema='public'\");console.log(r[0].count);")==0,'FRESH_DATABASE_REQUIRED')
            old_rows=migrate(previous['api']['Id'],'old-migrations')
            require(not any('agent_lesson_workflow'in x['migration_name']for x in old_rows),'OLD_SCHEMA_ALREADY_HAS_AGENT')
            db("await p.user.create({data:{id:'teacher',email:'synthetic-recovery@example.invalid',role:'ADMIN'}});console.log('true');",'control')
            up_apps();before=app.inspect()
            baseline=db("const h=await p.homework.create({data:{userId:'teacher',questionsJson:{},answersJson:{},displayStudent:'old preserved',displayAnswer:'old answer'}});console.log(JSON.stringify(h));",'api')
            served=json.loads(node("fetch('http://web:8080/version.json').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))"))
            require(served['version']==old['images'][0]['version']and served['buildSha']==old['images'][0]['sourceSha'],'OLD_WEB_BYTES_MISMATCH')
            passed('original-package-api-workers-web-start-on-original-migration-history',migrations=len(old_rows),version=served['version'])
            receiver.stop_api_for_release(before['api']);receiver.drain(before['worker-general'])
            receiver.stop_workers_for_release(before['worker-general'],before['worker-ppt'])
            new_rows=migrate(images['api']['Id'],'candidate-migrations')
            added=[x['migration_name']for x in new_rows if x['migration_name']not in {r['migration_name']for r in old_rows}]
            require('20261028000100_agent_lesson_workflow'in added,'AGENT_MIGRATION_NOT_EXERCISED')
            require(db('console.log(JSON.stringify(await p.homework.findUniqueOrThrow({where:{id:'+json.dumps(baseline['id'])+'}})));')==baseline,'MIGRATION_CHANGED_EXISTING_CONTENT')
            passed('candidate-prisma-migrations-preserve-existing-content',addedMigrations=added)
            extension=app.extension(app.initial)
            extension['services']['api']['image']=images['api']['Id']
            for name in ('worker-general','worker-ppt'):extension['services'][name]={'image':images['worker']['Id']}
            app.atomic(app.OVERRIDE,extension);up_apps()
            policy.probe(app,app.inspect()['api']['Id'],app.identity)
            policy.inspect_isolation(app.inspect()['codex-agent-runtime'],'codex-agent-runtime',app.config())
            passed('candidate-api-workers-and-real-runner-start-on-migrated-database')
            job=seed_job('candidate-before-rollback',True)
            wait_for(lambda:model_http()['active']==1,'CANDIDATE_NOT_GENERATING')
            original_override=app.OVERRIDE.read_bytes()
            proofs={kind:{'targetId':images[kind]['Id']}for kind in ('api','worker')}
            with ThreadPoolExecutor(max_workers=1)as executor:
                future=executor.submit(receiver.quiesce_for_rollback,before,proofs)
                try:
                    wait_for(lambda:not app.inspect()['api']['State']['Running'],'ROLLBACK_DID_NOT_STOP_API')
                    require(not future.done()and app.OVERRIDE.read_bytes()==original_override,'ROLLBACK_SWITCHED_BEFORE_GENERATION_DRAIN')
                finally:model_http('/release')
                future.result(timeout=90)
            require(read_job(job)['status']=='SUCCESS','CANDIDATE_GENERATION_NOT_SAVED')
            require(db('console.log(await p.contentRevisionProposal.count({where:{sourceJobId:'+json.dumps(job['id'])+'}}));')==1,'PROPOSAL_NOT_SAVED_ONCE')
            require(all(not app.inspect()[name]['State']['Running']for name in ('api','worker-general','worker-ppt')),'CANDIDATE_WRITERS_STILL_RUNNING')
            passed('real-receiver-rollback-drain-saves-in-flight-proposal-before-switch')
            app.atomic(app.OVERRIDE,{'services':{}});up_apps()
            # Remove only this proof's stateless runner after old applications work.
            runner=app.inspect()['codex-agent-runtime']
            require(runner['Config']['Labels']['com.docker.compose.project']==project and runner['Config']['User']=='1000:1000'and
                    all(m['Type']=='tmpfs'and m['Destination']=='/tmp'for m in runner['Mounts']),'LOCAL_RUNNER_REMOVAL_SCOPE')
            app.command(docker+['rm','--force',runner['Id']])
            after=app.inspect()
            for name,kind in [('api','api'),('worker-general','worker'),('worker-ppt','worker'),('web','web')]:
                require(after[name]['Image']==previous[kind]['Id'],'ORIGINAL_IMAGE_NOT_RESTORED')
            for name in ('postgres','redis'):
                require(after[name]['Id']==before[name]['Id']and after[name]['State']['StartedAt']==before[name]['State']['StartedAt'],'DATABASE_OR_REDIS_RESTARTED')
            require(db('console.log(JSON.stringify(await p.homework.findUniqueOrThrow({where:{id:'+json.dumps(baseline['id'])+'}})));','api')==baseline,'OLD_CLIENT_CONTENT_CHANGED')
            resumed=seed_job('old-worker-after-rollback',False)
            wait_for(lambda:read_job(resumed)['status']=='SUCCESS','OLD_WORKER_CANNOT_GENERATE_AFTER_MIGRATION')
            require('synthetic-old-worker-after-rollback'in db('console.log(JSON.stringify(await p.homework.findUniqueOrThrow({where:{id:'+json.dumps(resumed['homeworkId'])+'}})));','api')['displayStudent'],
                    'OLD_WORKER_CONTENT_NOT_SAVED')
            require(db('console.log(await p.contentRevisionProposal.count({where:{sourceJobId:'+json.dumps(job['id'])+'}}));')==1,'NEW_DATA_LOST_ON_ROLLBACK')
            passed('original-api-client-and-worker-read-write-after-upgrade-with-new-data-retained')
            report['fixtureRequests']=len(model_http()['requests']);report['status']='passed'
        except BaseException as error:
            report['error']=type(error).__name__+': '+str(error)
            if hasattr(app,'last_error'):report['containerError']=app.last_error
            raise
        finally:
            for name in ('old-migrations','candidate-migrations'):
                source=root/(name+'.log')
                if source.exists():args.output.with_name(args.output.stem+'-'+name+'.log').write_bytes(source.read_bytes())
            try:
                if started:
                    report['remainingResources']=cleanup(app,docker,project)
                    report['testResourcesRemoved']=True
            except BaseException as error:
                report['testResourcesRemoved']=False;report['status']='incomplete'
                report['cleanupError']=type(error).__name__+': '+str(error)
                raise
            finally:
                args.output.write_text(json.dumps(report,indent=2)+'\n')
                print(json.dumps(dict(status=report['status'],checks=len(report['checks']),cleaned=report.get('testResourcesRemoved'))),flush=True)


if __name__=='__main__':main()
