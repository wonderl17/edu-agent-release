"""Local Docker proof: sealed SDK network + existing API gateway. No model calls."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import types
import uuid


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',required=True)
    parser.add_argument('--api-source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'teaching-codex-runtime:[a-z0-9-]+',args.image):raise ValueError('Local verification image required')
    source=args.api_source.resolve()
    if source.name!='agent.egress.ts' or not source.is_file():raise ValueError('API transport source required')
    context=json.loads(subprocess.check_output(['docker','context','inspect'],timeout=10))[0]
    endpoint=context['Endpoints']['docker']['Host']
    if not endpoint.startswith('unix:///'):raise ValueError('Local Unix Docker context required')
    docker=['docker','--host',endpoint]
    env={k:v for k,v in os.environ.items() if not k.startswith(('DOCKER_', 'COMPOSE_'))}
    def command(values,*,data=None,timeout=45):
        return subprocess.check_output(docker+values,input=data,stderr=subprocess.PIPE,env=env,timeout=timeout)
    image=json.loads(command(['image','inspect',args.image]))[0]
    if image['Os']!='linux':raise ValueError('Linux test image required')
    spec=importlib.util.spec_from_file_location('codex_runtime',Path(__file__).resolve().parents[1]/'server/codex_runtime.py')
    policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
    project='codex-isolation-'+uuid.uuid4().hex[:12]
    policy.PROJECT=project
    if command(['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip():raise ValueError('Test project already exists')
    config=dict(schemaVersion=1,runnerToken=secrets.token_hex(32),egressToken=secrets.token_hex(32),
                modelBaseUrls=['https://model.example.invalid/v1'],rolloutMode='closed',userIds=[])
    identity=dict(schemaVersion=1,protocol='teaching-codex-plan-v1',sdkVersion='0.165.0')
    addition=policy.extension(policy.REGISTRY+'a'*64,identity,config)
    addition['services']['codex-agent-runtime']['image']=image['Id']
    with tempfile.TemporaryDirectory(prefix='codex-isolation-') as temp:
        root=Path(temp).resolve();os.chmod(root,0o700)
        # Compile the actual API module, not a substitute gateway. Only the entry
        # footer is a fixture; full API startup/build has separate scoped checks.
        compile_js="const fs=require('fs'),path=require('path'),ts=require(path.join(process.argv[2],'apps/api/node_modules/typescript'));process.stdout.write(ts.transpileModule(fs.readFileSync(process.argv[1],'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText)"
        compiled=subprocess.check_output(['node','-e',compile_js,str(source),str(source.parents[5])],timeout=20)
        footer=b"\nconst verificationServer=await startAgentEgressServer();if(!verificationServer)throw new Error('disabled');for(const s of ['SIGTERM','SIGINT'])process.once(s,()=>{void closeAgentEgressServer(verificationServer).then(()=>process.exit(0))});\n"
        (root/'api-egress.mjs').write_bytes(compiled+footer);(root/'api-egress.mjs').chmod(0o644)
        stub=['node','-e',"require('net').createServer(s=>s.end('synthetic')).listen(443,'0.0.0.0')"]
        base=dict(name=project,services={
            'api':dict(image=image['Id'],command=['node','/verification/api-egress.mjs'],networks={'app':None},environment={'SYNTHETIC_API':'true'},
                       volumes=[str(root/'api-egress.mjs')+':/verification/api-egress.mjs:ro'],
                       healthcheck=dict(test=['CMD','node','-e',"fetch('http://codex-agent-egress:4987/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"],interval='1s',timeout='2s',retries=10)),
            'database':dict(image=image['Id'],command=stub,networks={'app':None},healthcheck={'disable':True}),
            'outside':dict(image=image['Id'],command=stub,networks={'app':{'aliases':['model.example.invalid']}},healthcheck={'disable':True})},
            networks={'app':{'name':project+'_app'}})
        for name,value in [('base',base),('runtime',addition)]:
            path=root/(name+'.json');path.write_text(json.dumps(value));path.chmod(0o600)
        compose=['compose','--project-name',project,'--project-directory',str(root),'-f',str(root/'base.json'),'-f',str(root/'runtime.json')]
        resolved=json.loads(command(compose+['config','--format','json']))
        base_resolved=json.loads(command(['compose','--project-name',project,'-f',str(root/'base.json'),'config','--format','json']))
        if not resolved['networks']['codex-api'].get('internal'):raise ValueError('Runner network must be internal')
        if policy.strip_extension(base_resolved,resolved,resolved)!=base_resolved:raise ValueError('Policy changed the baseline API/data configuration')
        def inspect(name):
            cid=command(compose+['ps','-q',name]).decode().strip()
            return json.loads(command(['inspect',cid]))[0]
        def execute(name,code):
            return command(compose+['exec','-T',name,'node','--input-type=module','-e',code],timeout=12)
        def tcp(name,host,reachable,port=443):
            code="import net from 'node:net';const s=net.connect({host:"+json.dumps(host)+",port:"+str(port)+"});let done=false;function end(ok){if(done)return;done=true;s.destroy();process.exit(ok==="+str(reachable).lower()+"?0:1)}s.on('connect',()=>end(true));s.on('error',()=>end(false));s.setTimeout(800,()=>end(false));"
            execute(name,code)
        def negative_checks():
            database=inspect('database')['NetworkSettings']['Networks'][project+'_app']['IPAddress']
            outside=inspect('outside')['NetworkSettings']['Networks'][project+'_app']['IPAddress']
            api_app=inspect('api')['NetworkSettings']['Networks'][project+'_app']['IPAddress']
            tcp('codex-agent-runtime',database,False)
            tcp('codex-agent-runtime',outside,False)
            tcp('codex-agent-runtime','1.1.1.1',False)
            tcp('database',api_app,False,4987)
            execute('codex-agent-runtime',"const r=await fetch('http://codex-agent-egress:4987/health');if(!r.ok||(await r.json()).protocol!=='teaching-codex-egress-v1')process.exit(1)")
            code="import {createEgressFetch} from './egress-client.mjs';const send=createEgressFetch({url:process.env.CODEX_AGENT_EGRESS_URL,token:process.env.CODEX_AGENT_EGRESS_TOKEN});const r=await send('https://model.example.invalid/v1/responses',{method:'POST',redirect:'error',headers:{authorization:'Bearer SYNTHETIC'},body:JSON.stringify({model:'gpt-6-luna',stream:true,store:false}),signal:AbortSignal.timeout(4000)});const v=await r.json();console.log(JSON.stringify({status:r.status,code:v.code}))"
            result=json.loads(execute('codex-agent-runtime',code))
            if result!={'status':403,'code':'EGRESS_ENDPOINT_DENIED'}:raise ValueError('Private DNS destination was not explicitly refused')
        started=False
        try:
            started=True;command(compose+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','45'],timeout=70)
            policy.inspect_isolation(inspect('codex-agent-runtime'),'codex-agent-runtime',resolved)
            policy.inspect_api(inspect('api'),resolved)
            execute('api',"const r=await fetch('http://codex-agent-runtime:4986/health');const v=await r.json();if(!r.ok||v.runtime.sdkVersion!=='0.165.0')process.exit(1)")
            tcp('api','database',True);tcp('api','model.example.invalid',True)
            negative_checks()
            policy.probe(types.SimpleNamespace(DOCKER=[],run=command),inspect('api')['Id'],identity)
            before=inspect('codex-agent-runtime')['NetworkSettings']['SandboxID']
            command(compose+['restart','--timeout','2','codex-agent-runtime','api'],timeout=20)
            # health is checked explicitly after the process restarts; no temporary
            # firewall or privileged helper is needed to restore isolation.
            command(compose+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','45'],timeout=70)
            after=inspect('codex-agent-runtime')['NetworkSettings']['SandboxID']
            policy.inspect_isolation(inspect('codex-agent-runtime'),'codex-agent-runtime',resolved)
            policy.inspect_api(inspect('api'),resolved)
            negative_checks()
            policy.probe(types.SimpleNamespace(DOCKER=[],run=command),inspect('api')['Id'],identity)
            mutated=copy.deepcopy(resolved);mutated['services']['codex-agent-runtime']['networks']['app']=None
            try:policy.strip_extension(base_resolved,mutated,resolved)
            except ValueError:pass
            else:raise ValueError('Network policy allowed a data network')
            report=dict(status='passed',imageId=image['Id'],apiSourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                platform='linux/'+image['Architecture'],architecture='internal-runner-existing-api-egress',checks=[
                'api-to-runner-health','existing-api-data-and-outbound-positive-controls',
                'runner-to-database-ip-denied','runner-to-outside-ip-denied','runner-direct-public-ip-denied',
                'egress-not-listening-on-api-business-interface','runner-to-private-api-egress-health',
                'private-dns-answer-rejected-by-real-api-module','actual-runner-isolation','actual-api-private-network-and-credentials',
                'baseline-api-and-data-configuration-preserved','data-network-configuration-rejected',
                'all-network-negative-checks-repeated-after-restart',
                'release-private-health-and-unauthorized-request-probe',
                'release-private-health-probe-after-restart'],networkNamespaceChangedOnRestart=before!=after,
                realModelCalls=0,productionChanged=False,privilegedHelpers=0,hostFirewallChanged=False,
                previousSeparateEgressFailure='retained in network-baseline and runtime-network-proof.json; separate externally routed service removed')
        finally:
            if started:command(compose+['down','--volumes','--remove-orphans'],timeout=60)
        if command(['ps','-aq','--filter','label=com.docker.compose.project='+project]).strip():raise ValueError('Test containers remain')
        if command(['network','ls','-q','--filter','label=com.docker.compose.project='+project]).strip():raise ValueError('Test networks remain')
        report['testResourcesRemoved']=True
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))


if __name__=='__main__':main()
