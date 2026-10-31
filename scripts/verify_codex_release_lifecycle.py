"""Real local application topology through the receiver's deploy/rollback flow.

All eight application services use real images; the candidate adds the actual
SDK runner. An extra control container performs HTTP/DB probes. Registry
transport is replaced by already inspected local images; root paths/ownership,
project name and the receiver's host-loopback HTTP check are adapted locally.
No production operations, real model credentials or published ports.
"""
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import types
import uuid

from verify_codex_application import Application, load, require
from verify_codex_package_recovery import cleanup

BUSINESS={'api','worker-general','worker-ppt','web','gateway','postgres','redis','storage'}


class Stack(Application):
    def __init__(self,root,docker,images,project,policy,configure,receiver,old):
        super().__init__(root,docker,images,project,policy,configure)
        self.receiver=receiver;self.old=old
        base=json.loads(self.base_path.read_bytes());env=base['services']['api']['environment']
        storage_key='synthetic-'+secrets.token_hex(6);storage_secret=secrets.token_hex(24)
        env.update(OBJECT_STORAGE_ENDPOINT='http://gateway:19000',OBJECT_STORAGE_REGION='us-east-1',OBJECT_STORAGE_BUCKET='pilot-proof',
                   OBJECT_STORAGE_ACCESS_KEY_ID=storage_key,OBJECT_STORAGE_SECRET_ACCESS_KEY=storage_secret,OBJECT_STORAGE_FORCE_PATH_STYLE='true')
        base['services']['api']['image']=images['old-api']['Id']
        base['services']['api']['restart']='unless-stopped'
        for role in ('general','ppt'):
            worker_env=dict(env,AI_WORKER_ENABLED='true',AI_WORKER_ROLE=role,API_BACKGROUND_TASKS_ENABLED='false',
                AI_JOB_WORKER_CONCURRENCY='1',COMPOSE_PROJECT_NAME='teaching-production',PRODUCTION_ACTIVATED='true',TEACHING_DEPLOYMENT_ENV='shukezhinao-prod')
            base['services']['worker-'+role]=dict(image=images['old-worker']['Id'],platform='linux/amd64',networks={'app':None},
                                                environment=worker_env,restart='unless-stopped',healthcheck={'disable':True})
        self.old_web=root/'web-old'
        self.extract_web(images['old-web']['Id'],self.old_web,dict(sourceSha=old['sourceSha'],version=old['version']),root)
        nginx=root/'nginx.conf';nginx.write_text('server { listen 8080; root /usr/share/nginx/html; location / { try_files $uri $uri/ /index.html; } }\n');nginx.chmod(0o644)
        caddy=root/'Caddyfile';caddy.write_text('''{
 admin off
 auto_https off
}
:8080 {
 handle_path /api/* {
  reverse_proxy api:4999
 }
 handle {
  reverse_proxy web:8080
 }
}
:19000 {
 reverse_proxy storage:19000
}
''');caddy.chmod(0o644)
        base['services']['web']=dict(image=images['web-server']['Id'],platform='linux/amd64',networks={'app':None},restart='unless-stopped',
            volumes=[dict(type='bind',source=str(self.old_web),target='/usr/share/nginx/html',read_only=True,bind={'create_host_path':True}),
                     dict(type='bind',source=str(nginx),target='/etc/nginx/conf.d/default.conf',read_only=True,bind={'create_host_path':True})])
        base['services']['gateway']=dict(image=images['gateway']['Id'],platform='linux/amd64',networks={'app':None},restart='unless-stopped',
            volumes=[str(caddy)+':/etc/caddy/Caddyfile:ro'])
        base['services']['storage']=dict(image=images['storage']['Id'],platform='linux/amd64',networks={'app':None},restart='unless-stopped',
            environment=dict(MINIO_ROOT_USER=storage_key,MINIO_ROOT_PASSWORD=storage_secret),command=['server','/data','--address',':19000'])
        for name in ('postgres','redis'):
            base['services'][name]['platform']='linux/amd64';base['services'][name]['restart']='unless-stopped'
        base['services']['control']=dict(image=images['api']['Id'],platform='linux/amd64',networks={'app':None},environment=env,
            entrypoint=['node'],command=['-e',"process.on('SIGTERM',()=>process.exit(0));setInterval(()=>{},1000)"],healthcheck={'disable':True})
        self.atomic(self.base_path,base);self.atomic(self.OVERRIDE,{'services':{}})
        module=types.SimpleNamespace(extension=self.runtime_extension,load_settings=lambda _:policy.load_settings(self),
                                     probe=policy.probe,inspect_api=policy.inspect_api)
        self.ops=types.SimpleNamespace(config=self.config,preflight=self.preflight,validate_release=self.validate,inspect=self.inspect,
            snapshot=self.snapshot,compose_argv=self.compose_argv,codex_module=lambda:module,codex_context=lambda:self,
            maintenance=self.maintenance,isolation=self.isolation,fixed_env=lambda:{'PATH':os.environ['PATH'],'PRODUCTION_ACTIVATED':'true'})
        receiver.ROOT=root;receiver.OPS=root/'operations';receiver.OPS.mkdir()
        receiver.STATE=self.STATE;receiver.OVERRIDE=self.OVERRIDE;receiver.RELEASES=root/'releases'
        receiver.PROJECT=project;receiver.DOCKER=docker;receiver.ops=self.ops
        receiver.atomic=self.receiver_atomic;receiver.private=self.private;receiver.command=self.receiver_command
        self.prepared=None
        receiver.registry_handler=lambda:(types.SimpleNamespace(ticket=lambda *_:True,pull=self.artifacts),None)

    def receiver_atomic(self,path,value):
        require(path.resolve().is_relative_to(self.ROOT),'LOCAL_WRITE_SCOPE')
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.atomic(path,value)

    def extract_web(self,image,destination,h,work):
        identity=self.command(self.DOCKER+['create',image]).decode().strip()
        require(re.fullmatch(r'[0-9a-f]{64}',identity),'LOCAL_WEB_CONTAINER')
        copied=work/('copied-'+uuid.uuid4().hex)
        try:self.command(self.DOCKER+['cp',identity+':/usr/share/nginx/html',str(copied)])
        finally:self.command(self.DOCKER+['rm',identity])
        try:
            self.receiver.web_permissions(copied)
            archive=work/('web-'+uuid.uuid4().hex+'.tar.gz')
            def metadata(info):
                info.uid=info.gid=0;info.uname=info.gname='';info.mtime=int(info.mtime);info.pax_headers={};return info
            with tarfile.open(archive,'w:gz',format=tarfile.USTAR_FORMAT)as out:
                for path in sorted(copied.rglob('*')):out.add(path,arcname=str(path.relative_to(copied)),recursive=False,filter=metadata)
            result=self.receiver.web_unpack(archive,destination,h)
            archive.unlink();return result
        finally:shutil.rmtree(copied)

    def artifacts(self,_context,h,_stream,_ticket):
        manifest=self.prepared
        require(manifest and manifest['version']==h['version']and manifest['sourceSha']==h['sourceSha'],'LOCAL_PACKAGE_IDENTITY')
        raw=(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n').encode()
        require(len(raw)==h['bundleBytes']and hashlib.sha256(raw).hexdigest()==h['bundleSha256'],'LOCAL_PACKAGE_BYTES_CHANGED')
        self.receiver_atomic(self.receiver.run_dir/'manifest.json',raw)
        for item in manifest['images'].values():
            actual=json.loads(self.command(self.DOCKER+['image','inspect',item['tag']]))[0]
            require(actual['Id']==item['id']and actual['Architecture']=='amd64'and actual['Os']=='linux','LOCAL_IMAGE_CHANGED')
        web=self.receiver.run_dir/'web-dist'
        info=self.extract_web(manifest['images']['web']['tag'],web,h,self.receiver.run_dir)
        runtime=dict(manifest,images={k:v for k,v in manifest['images'].items()if k!='web'})
        return runtime,web,info,{k:dict(targetId=v['id'],localIdentityVerified=True)for k,v in runtime['images'].items()}

    def runtime_extension(self,image,identity,settings):
        require(image==self.images['runner']['Id']and identity==self.identity,'LOCAL_RUNTIME_IDENTITY')
        return self.local_extension(image,settings)

    def isolation(self,env,name):
        require(name=='api'and env['DATABASE_HOST']=='postgres'and self.database in env['DATABASE_URL']and
                '@postgres:'in env['DATABASE_URL']and env['REDIS_URL'].startswith('redis://redis:'),'LOCAL_DATA_SCOPE')

    def validate(self,base,candidate,approved_codex_settings=None):
        view=copy.deepcopy(candidate)
        if 'codex-agent-runtime'in candidate['services']:
            expected=self.extension(approved_codex_settings or json.loads(self.settings_path.read_bytes()))
            expected['services']['api']['image']=self.images['api']['Id']
            normalized=json.loads(self.command(self.DOCKER+['compose','--project-name',self.project,'-f','-','config','--format','json'],
                                               data=json.dumps(expected).encode()))
            view=self.policy.strip_extension(base,candidate,normalized)
        for name,kind in [('api','api'),('worker-general','worker'),('worker-ppt','worker')]:
            require(view['services'][name]['image']in(self.images[kind]['Id'],self.images['old-'+kind]['Id']),'LOCAL_RELEASE_IMAGE')
            view['services'][name]['image']=base['services'][name]['image']
        mounts=[m for m in view['services']['web']['volumes']if m['target']=='/usr/share/nginx/html']
        require(len(mounts)==1 and Path(mounts[0]['source']).resolve().is_relative_to(self.ROOT)and Path(mounts[0]['source']).is_dir(),
                'LOCAL_WEB_ROOT_SCOPE')
        original=next(m for m in base['services']['web']['volumes']if m['target']=='/usr/share/nginx/html')
        mounts[0]['source']=original['source']
        require(view==base,'LOCAL_BASELINE_CHANGED')
        require(all(n.get('internal')for n in candidate['networks'].values())and
                all(not s.get('ports')for s in candidate['services'].values()),'LOCAL_EXTERNAL_ACCESS_REFUSED')

    def inspect(self):
        result=super().inspect()
        return {k:v for k,v in result.items()if k in BUSINESS|{'codex-agent-runtime'}}

    def control(self,script):return self.node(script,'control')

    def fetch(self,path='/version.json'):
        return json.loads(self.control("fetch('http://gateway:8080"+path+"',{signal:AbortSignal.timeout(5000)}).then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()})))"))

    def consumers(self):
        return json.loads(self.control("const r=require('module').createRequire('/app/apps/api/package.json');const{Queue}=r('bullmq'),{Redis}=r('ioredis');const c=new Redis(process.env.REDIS_URL,{maxRetriesPerRequest:null});(async()=>{const out=[];for(const name of ['teaching-ai-agent-ai-jobs','teaching-ai-agent-ppt-jobs']){const q=new Queue(name,{connection:c});out.push({workers:(await q.getWorkers()).length});await q.close()}await c.quit();console.log(JSON.stringify(out))})()"))

    def snapshot(self):
        failures=[];consumers=[]
        try:
            config=self.preflight();actual=self.inspect()
            require(set(actual)==BUSINESS|({'codex-agent-runtime'}if 'codex-agent-runtime'in config['services']else set()),'LOCAL_APPLICATION_SET')
            require(all(x['State']['Running']and x['State'].get('Health',{}).get('Status','healthy')=='healthy'for x in actual.values()),'LOCAL_APPLICATION_UNHEALTHY')
            require(self.fetch('/api/health')['status']==200,'LOCAL_GATEWAY_API_FAILED')
            consumers=self.consumers();require(all(x['workers']==1 for x in consumers),'LOCAL_CONSUMERS_FAILED')
            if 'codex-agent-runtime'in actual:
                self.policy.inspect_isolation(actual['codex-agent-runtime'],'codex-agent-runtime',config)
                self.policy.inspect_api(actual['api'],config)
                self.policy.probe(self,actual['api']['Id'],self.identity)
        except Exception as error:failures.append(str(error))
        return dict(healthy=not failures,failures=failures,dependencies={'consumers':consumers})

    def receiver_command(self,argv,**kwargs):
        if argv[0]=='/usr/bin/curl':
            require(argv[-1]=='http://127.0.0.1:28080/version.json','LOCAL_HTTP_SCOPE')
            value=self.fetch();require(value['status']==200,'LOCAL_PUBLIC_VERSION_HTTP_FAILED')
            return json.dumps(value['body']).encode()
        return self.command(argv,**kwargs)

    def object_probe(self,create=False):
        script=r"""
const r=require('module').createRequire('/app/apps/api/package.json');const s=r('@aws-sdk/client-s3'),{getSignedUrl}=r('@aws-sdk/s3-request-presigner');
(async()=>{const c=new s.S3Client({region:'us-east-1',endpoint:process.env.OBJECT_STORAGE_ENDPOINT,forcePathStyle:true,
 credentials:{accessKeyId:process.env.OBJECT_STORAGE_ACCESS_KEY_ID,secretAccessKey:process.env.OBJECT_STORAGE_SECRET_ACCESS_KEY}});
const Bucket='pilot-proof',Key='preserved.txt';
if(CREATE){await c.send(new s.CreateBucketCommand({Bucket}));
 const put=await getSignedUrl(c,new s.PutObjectCommand({Bucket,Key}),{expiresIn:60});
 const response=await fetch(put,{method:'PUT',body:'synthetic-private-object'});if(!response.ok)throw Error('signed put failed');}
const get=await getSignedUrl(c,new s.GetObjectCommand({Bucket,Key}),{expiresIn:60});
const signed=await fetch(get);const body=await signed.text();const unsigned=await fetch(process.env.OBJECT_STORAGE_ENDPOINT+'/'+Bucket+'/'+Key);
if(signed.status!==200||body!=='synthetic-private-object'||unsigned.status!==403)throw Error('private object mismatch');
console.log(JSON.stringify({signedRead:200,unsignedRead:403,contentPreserved:true}));c.destroy();})().catch(e=>{console.error(e.name);process.exitCode=1});
""".replace('CREATE',str(create).lower())
        return json.loads(self.control(script))


def main():
    parser=argparse.ArgumentParser()
    for name in ('api','worker','web','runner'):parser.add_argument('--'+name+'-image',required=True)
    parser.add_argument('--image-proof',type=Path,required=True)
    parser.add_argument('--old-manifest',type=Path,required=True)
    parser.add_argument('--queue-probe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    evidence=json.loads(args.image_proof.read_bytes());old=json.loads(args.old_manifest.read_bytes())
    require(evidence['status']=='passed'and evidence['oldSourceSha']==old['sourceSha']and evidence['oldVersion']==old['version'],'OLD_PACKAGE_PROOF')
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'),'LOCAL_DOCKER_REQUIRED');docker=['docker','--host',endpoint]
    def inspect_image(ref):return json.loads(subprocess.check_output(docker+['image','inspect',ref]))[0]
    images={}
    for row in evidence['images']:
        image=inspect_image(row['id']);require(image['Id']==row['id']and image['Architecture']=='amd64'and image['Os']=='linux','IMAGE_PROOF_CHANGED')
        images['web-server'if row['kind']=='web'else row['kind']]=image
    for kind in ('api','worker','web','runner'):
        tag=getattr(args,kind+'_image');stem='runtime'if kind=='runner'else kind
        require(re.fullmatch('teaching-codex-'+stem+r':[a-z0-9-]+',tag),'LOCAL_CANDIDATE_TAG')
        images[kind]=inspect_image(tag);require(images[kind]['Architecture']=='amd64'and images[kind]['Os']=='linux','AMD64_CANDIDATE_REQUIRED')
    configure,policy,receiver=load('codex_configure'),load('codex_runtime'),load('tencent_receiver')
    project='codex-lifecycle-'+uuid.uuid4().hex[:12];configure.PROJECT=policy.PROJECT=project
    require(not subprocess.check_output(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip(),'PROJECT_EXISTS')
    report=dict(status='incomplete',checks=[],project=project,modelCalls=0,productionChanged=False,cnbBuildsTriggered=False,
        images={k:v['Id']for k,v in images.items()},
        limitations=['local candidate uses synthetic all-zero source SHA and current workspace version; not a signed/releasable package',
                     'the selected old package and local candidate exercise the real explicit same-version source-change policy',
                     'registry transport is replaced with inspected local images; actual receiver Web validation/migrations/deploy/postflight/rollback run',
                     'root ownership, model endpoint credentials, public TLS and host installation are not exercised',
                     'local base uses internal networks, synthetic credentials/data and an extra control container; no production configuration or business data'])
    report['sourceHashes']={name:hashlib.sha256((Path(__file__).resolve().parents[1]/name).read_bytes()).hexdigest()for name in
                            ['server/tencent_receiver.py','server/codex_runtime.py','scripts/verify_codex_release_lifecycle.py']}
    def passed(name,**extra):report['checks'].append(dict(name=name,**extra));print(json.dumps(dict(check=name,status='passed')),flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-release-lifecycle-')as temporary:
        root=Path(temporary).resolve();root.chmod(0o700);app=None;started=False
        try:
            app=Stack(root,docker,images,project,policy,configure,receiver,old)
            (receiver.OPS/'queue-probe.mjs').write_bytes(args.queue_probe.read_bytes())
            started=True
            app.command(app.compose_argv()+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','60',
                'postgres','redis','storage','gateway','control'],timeout=90)
            require(app.db("const x=await p.$queryRawUnsafe(\"SELECT count(*)::int AS count FROM information_schema.tables WHERE table_schema='public'\");console.log(x[0].count);",'control')==0,'FRESH_DB_REQUIRED')
            receiver.run_dir=root/'initial';receiver.run_dir.mkdir()
            receiver.migration({'sourceSha':old['sourceSha']},{'images':{'api':{'tag':images['old-api']['Id']}}})
            receiver.up_four()
            until=time.monotonic()+90
            while True:
                sample=app.snapshot()
                if sample['healthy']:break
                if time.monotonic()>=until:report['initialHealth']=sample;raise ValueError('INITIAL_STACK_UNHEALTHY')
                time.sleep(1)
            baseline_version,before=receiver.current()
            require(baseline_version['buildSha']==old['sourceSha']and baseline_version['version']==old['version'],'OLD_VERSION_MISMATCH')
            app.db("await p.user.create({data:{id:'teacher',email:'lifecycle@example.invalid',role:'ADMIN'}});console.log(true);",'api')
            object_proof=app.object_probe(True)
            passed('real-eight-service-baseline-and-private-object-through-gateway',object=object_proof)
            candidate_version=json.loads(app.control("console.log(JSON.stringify({version:require('/app/package.json').version}))"))['version']
            require(candidate_version==old['version'],'THIS_PROOF_REQUIRES_SAME_VERSION_PAIR')
            candidate=dict(version=candidate_version,sourceSha='0'*40,runtime=app.identity,
                images={kind:dict(tag=images[key]['Id'],id=images[key]['Id'])for kind,key in [('api','api'),('worker','worker'),('web','web'),('codex-runtime','runner')]})
            legacy=dict(version=old['version'],sourceSha=old['sourceSha'],images={kind:dict(tag=images['old-'+kind]['Id'],id=images['old-'+kind]['Id'])for kind in ('api','worker','web')})
            def deploy(package):
                current,_=receiver.current()
                raw=(json.dumps(package,sort_keys=True,separators=(',',':'))+'\n').encode()
                h=dict(sourceRepository=receiver.SOURCE,sourceSha=package['sourceSha'],version=package['version'],
                    bundleBytes=len(raw),bundleSha256=hashlib.sha256(raw).hexdigest(),allowSameVersionSourceChange=True,expectedCurrentSourceSha=current['buildSha'])
                app.prepared=package;receiver.rolling_back=False;receiver.client_done.clear()
                return receiver.deploy(h,io.BytesIO())
            # Check a real downgrade independently of the same-version pair.
            # The candidate may itself already be version 0.0.132 or newer.
            try:receiver.version_policy(dict(version='0.0.131',sourceSha='0'*40),dict(version='0.0.132',buildSha='f'*40))
            except receiver.Refused as error:require(str(error)=='VERSION_REGRESSION_REFUSED','WRONG_VERSION_REFUSAL')
            else:raise ValueError('PRODUCTION_VERSION_REGRESSION_ACCEPTED')
            passed('actual-production-version-regression-remains-refused')
            original_postflight=receiver.postflight
            def fail_after_real_checks(*arguments):
                original_postflight(*arguments)
                raise receiver.Refused('SYNTHETIC_POSTFLIGHT_FAILURE')
            receiver.postflight=fail_after_real_checks
            failed=deploy(candidate);receiver.postflight=original_postflight
            require(failed['code']=='DEPLOY_FAILED_ROLLBACK_RESTORED'and failed['migrationSucceeded']and
                    failed['causeCode']=='SYNTHETIC_POSTFLIGHT_FAILURE','ACTUAL_ROLLBACK_FAILED')
            require(receiver.current()[0]==baseline_version and 'codex-agent-runtime'not in app.inspect(),'OLD_PACKAGE_NOT_RESTORED')
            app.object_probe();passed('receiver-failed-upgrade-restores-original-package-after-real-postflight',receipt=failed)
            succeeded=deploy(candidate)
            require(succeeded['status']=='deployed'and succeeded['verification']['codex']['unauthorizedRequestDenied'],'ACTUAL_ACTIVATION_FAILED')
            require(app.fetch()['body']['buildSha']=='0'*40,'CANDIDATE_WEB_NOT_SERVED')
            app.object_probe();passed('receiver-completes-candidate-api-workers-web-runner-activation',receipt=succeeded)
            access=json.loads(app.control("const j=require('module').createRequire('/app/apps/api/package.json')('jsonwebtoken');const token=j.sign({sub:'teacher',email:'lifecycle@example.invalid',passwordVersion:0},process.env.JWT_SECRET,{expiresIn:'5m'});fetch('http://gateway:8080/api/agent/access',{headers:{authorization:'Bearer '+token}}).then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()})))"))
            require(access['status']==200 and access['body']['visible']is False and access['body']['canStart']is False,'DEFAULT_CLOSED_FAILED')
            passed('new-web-api-route-keeps-admin-account-outside-closed-rollout')
            restored=deploy(legacy)
            require(restored['status']=='deployed'and 'codex-agent-runtime'not in app.inspect(),'LEGACY_RESUME_FAILED')
            after=app.inspect();require(receiver.current()[0]==baseline_version,'LEGACY_WEB_IDENTITY_FAILED')
            for name in ('gateway','postgres','redis','storage'):
                require(after[name]['Id']==before[name]['Id']and after[name]['State']['StartedAt']==before[name]['State']['StartedAt'],'IMMUTABLE_SERVICE_RESTARTED')
            app.object_probe();passed('receiver-resumes-original-three-image-package-and-preserves-data-services',receipt=restored)
            report['status']='passed'
        except BaseException as error:
            report['error']=type(error).__name__+': '+str(error)
            if app and hasattr(app,'last_error'):report['containerError']=app.last_error
            if app:
                for file in ('last-failure.json','last-success.json'):
                    if (app.STATE/file).exists():report[file]=json.loads((app.STATE/file).read_bytes())
            raise
        finally:
            try:
                if started:
                    report['remainingResources']=cleanup(app,docker,project);report['testResourcesRemoved']=True
            except BaseException as error:
                report['testResourcesRemoved']=False;report['status']='incomplete';report['cleanupError']=type(error).__name__+': '+str(error)
                raise
            finally:
                args.output.write_text(json.dumps(report,indent=2)+'\n')
                print(json.dumps(dict(status=report['status'],checks=len(report['checks']),cleaned=report.get('testResourcesRemoved'))),flush=True)


if __name__=='__main__':main()
