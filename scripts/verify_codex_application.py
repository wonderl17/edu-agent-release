"""Full API image + real runner configuration/restart proof on local Docker.

No model calls, published ports, existing data, or production access. Postgres
and Redis are real; the other five services are explicitly inert identity
fixtures. This verifies configuration transitions, not the complete release
pipeline, old application compatibility, or production database migrations.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import time
import types
import uuid


def require(condition, code):
    if not condition:
        raise ValueError(code)


def load(name):
    path = Path(__file__).resolve().parents[1] / 'server' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InjectedCrash(BaseException):
    pass


class Application:
    def __init__(self, root, docker, images, project, policy, configure):
        self.ROOT = root
        self.STATE = root / 'state'
        self.STATE.mkdir(mode=0o700)
        (root / 'environments').mkdir(mode=0o700)
        self.OVERRIDE = root / 'runtime.json'
        self.base_path = root / 'base.json'
        self.settings_path = root / configure.SETTINGS
        self.DOCKER = docker
        self.images, self.project = images, project
        self.policy, self.configure = policy, configure
        self.require, self.decode = require, json.loads
        self.rolling_back = False
        self.fault = None
        self.identity = dict(schemaVersion=1, protocol='teaching-codex-plan-v1', sdkVersion='0.165.0')
        self.initial = dict(schemaVersion=1, runnerToken=secrets.token_hex(32), egressToken=secrets.token_hex(32),
                            modelBaseUrls=['https://model.example.invalid/v1'], rolloutMode='closed', userIds=[])
        self.database = 'teaching_agent_e2e_' + uuid.uuid4().hex
        db_password = secrets.token_hex(24)
        api_env = dict(NODE_ENV='production', HOST='0.0.0.0', PORT='4999', DATABASE_HOST='postgres',
                       DATABASE_URL=f'postgresql://postgres:{db_password}@postgres:5432/{self.database}?schema=public',
                       REDIS_URL='redis://redis:6379', JWT_SECRET=secrets.token_hex(32),
                       MODEL_CREDENTIAL_ENCRYPTION_KEY=secrets.token_urlsafe(32),
                       API_BACKGROUND_TASKS_ENABLED='true', AI_WORKER_ENABLED='false', MONITORING_ENABLED='false',
                       PRISMA_MIGRATE_DEPLOY='false', PRISMA_DB_PUSH='false')
        api = dict(image=images['api']['Id'], platform='linux/amd64', networks={'app': None}, environment=api_env,
                   healthcheck=dict(test=['CMD', 'node', '-e', "fetch('http://127.0.0.1:4999/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"],
                                    interval='2s', timeout='5s', retries=15, start_period='15s'))
        noop = dict(image=images['api']['Id'], platform='linux/amd64', entrypoint=['node'],
                    command=['-e', 'setInterval(()=>{},1000)'], networks={'app': None},
                    environment=api_env, healthcheck={'disable': True})
        base = dict(name=project, services={
            'api': api,
            'postgres': dict(image=images['postgres']['Id'], networks={'app': None},
                             environment=dict(POSTGRES_PASSWORD=db_password, POSTGRES_DB=self.database),
                             healthcheck=dict(test=['CMD-SHELL', 'pg_isready -U postgres'], interval='1s', timeout='3s', retries=30)),
            'redis': dict(image=images['redis']['Id'], networks={'app': None},
                          healthcheck=dict(test=['CMD', 'redis-cli', 'ping'], interval='1s', timeout='3s', retries=30)),
            **{name: copy.deepcopy(noop) for name in ('worker-general', 'worker-ppt', 'web', 'gateway', 'storage')},
        }, networks={'app': dict(name=project+'_app', driver='bridge', internal=True)})
        self.atomic(self.base_path, base)
        self.atomic(self.settings_path, self.initial)
        self.atomic(self.OVERRIDE, self.extension(self.initial))
        self.ops = types.SimpleNamespace(codex_module=lambda: types.SimpleNamespace(
            SERVICES=policy.SERVICES, settings=policy.settings,
            trusted_extension=lambda _ops, image, desired=None: self.local_extension(image, desired)),
            codex_context=lambda: self, maintenance=self.maintenance, preflight=self.preflight,
            compose_argv=self.compose_argv, config=self.config, validate_release=self.validate,
            snapshot=self.snapshot)

    def command(self, argv, *, data=None, timeout=45):
        require(argv[:len(self.DOCKER)] == self.DOCKER, 'LOCAL_DOCKER_COMMAND_REQUIRED')
        environment = {k: v for k, v in os.environ.items() if not k.startswith(('DOCKER_', 'COMPOSE_'))}
        try:
            return subprocess.check_output(argv, input=data, stderr=subprocess.PIPE, env=environment, timeout=timeout)
        except subprocess.CalledProcessError as error:
            # Docker may quote complete environment settings in diagnostics.
            text = error.stderr.decode(errors='replace')[-6000:]
            for path in (self.base_path, self.settings_path, self.OVERRIDE):
                if path.exists():
                    def redact(value):
                        nonlocal text
                        if isinstance(value, dict):
                            for key, item in value.items():
                                if isinstance(item, str) and any(x in key.upper() for x in ('TOKEN', 'SECRET', 'PASSWORD', 'KEY', 'DATABASE_URL')):
                                    text = text.replace(item, '[REDACTED]')
                                redact(item)
                        elif isinstance(value, list):
                            for item in value: redact(item)
                    redact(json.loads(path.read_bytes()))
            self.last_error = text
            raise ValueError('LOCAL_CONTAINER_COMMAND_FAILED') from None

    run = command

    def private(self, path):
        info = path.lstat()
        require(path.resolve().is_relative_to(self.ROOT) and not path.is_symlink() and stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600, 'LOCAL_PRIVATE_FILE_REQUIRED')

    safe_file = private

    def atomic(self, path, value):
        raw = value if isinstance(value, bytes) else self.configure.canonical(value)
        temporary = path.with_name(path.name+'.'+uuid.uuid4().hex)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if self.fault: self.fault('write:'+path.name)

    @contextlib.contextmanager
    def release_lock(self):
        with (self.ROOT/'release.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: yield
            finally: fcntl.flock(stream, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def maintenance(self, *_args, **_kwargs):
        yield  # No local traffic or external maintenance gate exists in this fixture.

    def extension(self, desired):
        value = self.policy.extension(self.policy.REGISTRY+'a'*64, self.identity, desired)
        value['services']['codex-agent-runtime']['image'] = self.images['runner']['Id']
        value['services']['codex-agent-runtime']['platform'] = 'linux/amd64'
        return value

    def local_extension(self, image, desired):
        # Only the registry reference is adapted for local unpushed images.
        require(image == self.images['runner']['Id'], 'LOCAL_RUNTIME_IMAGE_CHANGED')
        actual = json.loads(self.command(self.DOCKER+['image', 'inspect', image]))[0]
        require(actual['Id'] == image and actual['Architecture'] == 'amd64' and actual['Os'] == 'linux'
                and actual['Config']['User'] == 'node' and actual['Config']['Cmd'] == ['node', 'server.mjs']
                and not actual['Config'].get('Entrypoint'), 'LOCAL_RUNTIME_IMAGE_IDENTITY')
        return self.extension(desired or json.loads(self.settings_path.read_bytes()))

    def compose_argv(self, include_release=True):
        argv = self.DOCKER+['compose', '--project-name', self.project, '--project-directory', str(self.ROOT), '-f', str(self.base_path)]
        if include_release: argv += ['-f', str(self.OVERRIDE)]
        return argv

    def config(self, include_release=True):
        return json.loads(self.command(self.compose_argv(include_release)+['config', '--format', 'json']))

    def validate(self, base, candidate, approved_codex_settings=None):
        expected = self.extension(approved_codex_settings or json.loads(self.settings_path.read_bytes()))
        expected['services']['api']['image'] = self.images['api']['Id']
        normalized = json.loads(self.command(self.DOCKER+['compose', '--project-name', self.project, '-f', '-', 'config', '--format', 'json'],
                                             data=json.dumps(expected).encode()))
        stripped = self.policy.strip_extension(base, candidate, normalized)
        require(stripped == base, 'LOCAL_BASELINE_CHANGED')
        require(all(net.get('internal') for net in candidate['networks'].values()), 'LOCAL_EXTERNAL_NETWORK_REFUSED')

    def preflight(self):
        result = self.config()
        self.validate(self.config(False), result)
        return result

    def inspect(self):
        ids = self.command(self.DOCKER+['ps', '-aq', '--filter', 'label=com.docker.compose.project='+self.project,
                                      '--filter', 'label=com.docker.compose.oneoff=False']).decode().split()
        require(bool(ids), 'LOCAL_CONTAINERS_MISSING')
        return {x['Config']['Labels']['com.docker.compose.service']: x for x in json.loads(self.command(self.DOCKER+['inspect', *ids]))}

    def current(self):
        merged = self.preflight(); actual = self.inspect()
        self.policy.inspect_isolation(actual['codex-agent-runtime'], 'codex-agent-runtime', merged)
        self.policy.inspect_api(actual['api'], merged)
        # Source version is read from the actual API image; synthetic buildSha is
        # explicit because this is a local dirty tree, not a signed release.
        version = json.loads(self.node("console.log(JSON.stringify({version:require('/app/package.json').version,buildSha:'0'.repeat(40)}))"))
        return version, actual

    def snapshot(self):
        failures = []
        try:
            _, actual = self.current()
            require(set(actual) == self.configure.SERVICES, 'LOCAL_SERVICE_SET')
            require(all(x['State']['Running'] and x['State'].get('Health', {}).get('Status', 'healthy') == 'healthy'
                        for x in actual.values()), 'LOCAL_SERVICE_UNHEALTHY')
            self.policy.probe(self, actual['api']['Id'], self.identity)
            self.policy.inspect_networks(self, self.config())
        except Exception as error:
            failures.append(self.configure.error_code(error))
        if (self.STATE/self.configure.PENDING).exists(): failures.append('CODEX_CONFIGURATION_PENDING')
        return dict(healthy=not failures, failures=failures)

    def node(self, script, service='api', timeout=30):
        return self.command(self.compose_argv()+['exec', '-T', service, 'node', '--input-type=commonjs'], data=script.encode(), timeout=timeout)

    def db(self, script, service='api'):
        prelude = "const requireApi=require('module').createRequire('/app/apps/api/package.json');const{PrismaClient}=requireApi('@prisma/client');const p=new PrismaClient();"
        return json.loads(self.node(prelude+"(async()=>{"+script+"})().finally(()=>p.$disconnect()).catch(e=>{console.error(e.message);process.exitCode=1})", service))

    def http(self, user, path, method='GET', body=None):
        script = "const jwt=require('module').createRequire('/app/apps/api/package.json')('jsonwebtoken');const who="+json.dumps(user)+";"
        script += "const token=who?jwt.sign({sub:who,email:who+'@example.invalid',passwordVersion:0},process.env.JWT_SECRET,{expiresIn:'5m'}):null;"
        script += "fetch('http://api:4999'+"+json.dumps(path)+",{method:"+json.dumps(method)+",headers:{'content-type':'application/json',...(token?{authorization:'Bearer '+token}:{})},"
        if body is not None: script += "body:JSON.stringify("+json.dumps(body)+"),"
        script += "signal:AbortSignal.timeout(10000)}).then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()}))).catch(e=>{console.error(e.message);process.exitCode=1})"
        return json.loads(self.node(script, 'web'))

    def request(self, desired):
        return dict(settingsSha256=self.configure.digest(self.settings_path.read_bytes()),
                    overrideSha256=self.configure.digest(self.OVERRIDE.read_bytes()), settings=desired)

    def token_probe(self, config, accepted):
        for key, url in [('runnerToken', 'http://codex-agent-runtime:4986/plan'), ('egressToken', 'http://codex-agent-egress:4987/responses')]:
            result = json.loads(self.node("fetch("+json.dumps(url)+",{method:'POST',headers:{authorization:'Bearer '+"+json.dumps(config[key])+"},body:'{}',signal:AbortSignal.timeout(5000)}).then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()})))"))
            require(result['status'] == ((400 if key == 'runnerToken' else 403) if accepted else 401), 'TOKEN_TRANSITION_MISMATCH')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--api-image', required=True)
    parser.add_argument('--runner-image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(re.fullmatch(r'teaching-codex-api:[a-z0-9-]+', args.api_image), 'LOCAL_API_TAG_REQUIRED')
    require(re.fullmatch(r'teaching-codex-runtime:[a-z0-9-]+', args.runner_image), 'LOCAL_RUNNER_TAG_REQUIRED')
    context = json.loads(subprocess.check_output(['docker', 'context', 'inspect'], timeout=10))[0]
    endpoint = context['Endpoints']['docker']['Host']
    require(endpoint.startswith('unix:///'), 'LOCAL_UNIX_DOCKER_REQUIRED')
    docker = ['docker', '--host', endpoint]
    images = {key: json.loads(subprocess.check_output(docker+['image', 'inspect', name], timeout=10))[0]
              for key, name in dict(api=args.api_image, runner=args.runner_image, postgres='postgres:17-alpine', redis='redis:7.4-alpine').items()}
    require(all(x['Os'] == 'linux' for x in images.values()), 'LINUX_IMAGES_REQUIRED')
    require(all(images[key]['Architecture'] == 'amd64' for key in ('api', 'runner')), 'TARGET_API_RUNNER_ARCH_REQUIRED')
    configure, policy = load('codex_configure'), load('codex_runtime')
    project = 'codex-app-'+uuid.uuid4().hex[:12]
    configure.PROJECT = policy.PROJECT = project  # Local-only adapter; production defaults stay fixed.
    require(not subprocess.check_output(docker+['ps', '-aq', '--filter', 'label=com.docker.compose.project='+project]).strip(), 'PROJECT_EXISTS')
    report = dict(status='incomplete', checks=[], project=project, realModelCalls=0, productionChanged=False,
                  apiImage=images['api']['Id'], runnerImage=images['runner']['Id'], platform='linux/amd64',
                  limitations=['five non-API application services are inert identity fixtures',
                               'fresh synthetic DB schema push; not production migration validation',
                               'local image IDs; not registry/release-package validation',
                               'local locks/file owner; not actual root host installation or maintenance gate',
                               'no old application package or live generation drain exercised'])
    for name in ('codex_configure', 'codex_runtime'):
        report[name+'Sha256'] = hashlib.sha256((Path(__file__).resolve().parents[1]/'server'/(name+'.py')).read_bytes()).hexdigest()
    def passed(name):
        report['checks'].append(name)
        print(json.dumps(dict(check=name, status='passed')), flush=True)
    with tempfile.TemporaryDirectory(prefix='codex-application-') as temp:
        root = Path(temp).resolve(); root.chmod(0o700)
        app = Application(root, docker, images, project, policy, configure)
        started = False
        try:
            app.preflight()
            started = True
            app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '60',
                                          'postgres', 'redis', 'worker-general', 'worker-ppt', 'web', 'gateway', 'storage'], timeout=90)
            count = app.db("const r=await p.$queryRawUnsafe(\"SELECT count(*)::int AS count FROM information_schema.tables WHERE table_schema='public'\");console.log(JSON.stringify(r[0].count));", 'web')
            require(count == 0, 'FRESH_DATABASE_REQUIRED')
            app.node("require('child_process').execFileSync('pnpm',['exec','prisma','db','push','--skip-generate'],{stdio:'inherit'});", 'web', timeout=120)
            app.db("await p.user.createMany({data:['teacher','other','admin'].map(id=>({id,email:id+'@example.invalid',role:id==='admin'?'ADMIN':'USER'}))});console.log('true');", 'web')
            app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120'], timeout=160)
            require(app.snapshot()['healthy'], 'INITIAL_HEALTH_FAILED')
            passed('full-api-original-entrypoint-and-private-runner-health')
            body = dict(clientRequestId=str(uuid.uuid4()), context=dict(kind='student', id='absent'), request='synthetic admission check')
            for user in ('teacher', 'other', 'admin'):
                access = app.http(user, '/agent/access')
                require(access['status'] == 200 and access['body']['visible'] is False and access['body']['canStart'] is False, 'CLOSED_ACCESS_FAILED')
                require(app.http(user, '/agent/runs', 'POST', body)['status'] == 403, 'CLOSED_CREATION_FAILED')
            require(app.http(None, '/agent/access')['status'] == 401, 'AUTH_REQUIRED')
            passed('closed-default-blocks-new-tasks-including-admin')
            opened = dict(app.initial, rolloutMode='allowlist', userIds=['teacher'], runnerToken=secrets.token_hex(32), egressToken=secrets.token_hex(32))
            require(configure.apply(app, app.request(opened))['status'] == 'configured', 'ALLOWLIST_APPLY_FAILED')
            require(app.http('teacher', '/agent/access')['body']['canStart'] is True, 'ALLOWLIST_ACCESS_FAILED')
            denied_context = app.http('teacher', '/agent/runs', 'POST', body)
            require(denied_context['status'] == 404, 'ADMITTED_OWNERSHIP_CHECK_FAILED')
            for user in ('other', 'admin'):
                require(app.http(user, '/agent/runs', 'POST', body)['status'] == 403, 'ALLOWLIST_BYPASS')
            app.token_probe(app.initial, False); app.token_probe(opened, True)
            passed('actual-allowlist-and-token-rotation-only-recreates-api-runner')
            # Synthetic historical row, never a claim that a model generated it.
            history = app.db("const s=await p.agentSession.create({data:{userId:'teacher',title:'synthetic history',context:{kind:'student',id:'absent'}}});const r=await p.agentRun.create({data:{sessionId:s.id,userId:'teacher',clientRequestId:crypto.randomUUID(),requestHash:'synthetic',request:'synthetic',context:{kind:'student',id:'absent'},status:'waiting_input',clarification:'synthetic input needed',passwordVersion:0,runtimeCiphertext:Buffer.from('not used by cancellation'),runtimeCipherVersion:1}});await p.agentSession.update({where:{id:s.id},data:{activeRunId:r.id}});console.log(JSON.stringify({sessionId:s.id,runId:r.id}));")
            closed = dict(opened, rolloutMode='closed', userIds=[])
            require(configure.apply(app, app.request(closed))['status'] == 'configured', 'CLOSE_APPLY_FAILED')
            access = app.http('teacher', '/agent/access')['body']
            require(access['visible'] and not access['canStart'] and access['canResume'], 'CLOSED_HISTORY_ACCESS_FAILED')
            require(app.http('teacher', '/agent/sessions/'+history['sessionId'])['status'] == 200, 'HISTORY_LOST')
            require(app.http('other', '/agent/sessions/'+history['sessionId'])['status'] == 404, 'HISTORY_ACCOUNT_LEAK')
            require(app.http('teacher', '/agent/runs', 'POST', body)['status'] == 403, 'CLOSE_NEW_TASK_FAILED')
            passed('close-preserves-owned-history-and-blocks-new-work')
            # Persist a cancellation via the real HTTP handler while its lease
            # prevents processing, then stop API and expire the synthetic lease.
            app.db("await p.agentRun.update({where:{id:"+json.dumps(history['runId'])+"},data:{leaseToken:'synthetic-previous-process',leaseUntil:new Date(Date.now()+60000)}});console.log('true');")
            require(app.http('teacher', '/agent/runs/'+history['runId']+'/cancel', 'POST', {})['status'] == 202, 'CANCEL_HTTP_FAILED')
            app.command(app.compose_argv()+['stop', '--timeout', '30', 'api'], timeout=45)
            app.db("await p.agentRun.update({where:{id:"+json.dumps(history['runId'])+"},data:{leaseUntil:new Date(0)}});console.log('true');", 'web')
            app.command(app.compose_argv()+['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', 'api'], timeout=150)
            deadline = time.monotonic()+20
            while True:
                final = app.db("const r=await p.agentRun.findUniqueOrThrow({where:{id:"+json.dumps(history['runId'])+"},include:{session:true}});console.log(JSON.stringify({status:r.status,activeRunId:r.session.activeRunId,leaseToken:r.leaseToken}));")
                if final['status'] == 'cancelled' and final['activeRunId'] is None and final['leaseToken'] is None: break
                require(time.monotonic() < deadline, 'COORDINATOR_RESTART_FAILED'); time.sleep(0.5)
            require(app.snapshot()['healthy'], 'RESTART_HEALTH_FAILED')
            passed('durable-http-cancel-completes-after-full-api-restart-while-closed')
            # Inject failure before new API starts; real runner already changed.
            original_command = app.command
            failed = False
            def fail_api_once(argv, **kwargs):
                nonlocal failed
                if not failed and 'up' in argv and argv[-1] == 'api':
                    failed = True
                    raise ValueError('INJECTED_API_START_FAILURE')
                return original_command(argv, **kwargs)
            app.command = fail_api_once
            restored = configure.apply(app, app.request(opened))
            app.command = original_command
            require(restored.get('code') == 'CODEX_CONFIGURATION_ROLLBACK_RESTORED' and app.snapshot()['healthy'], 'ACTUAL_ROLLBACK_FAILED')
            require(app.http('teacher', '/agent/access')['body']['canStart'] is False, 'ROLLBACK_REOPENED_ACCOUNT')
            passed('api-start-failure-restores-files-and-actual-runtime-health')
            fired = False
            def crash_after_settings(event):
                nonlocal fired
                if not fired and event == 'write:'+app.settings_path.name:
                    fired = True
                    raise InjectedCrash()
            app.fault = crash_after_settings
            try: configure.apply(app, app.request(opened))
            except InjectedCrash: pass
            else: raise ValueError('CRASH_NOT_INJECTED')
            app.fault = None
            require(configure.status(app)['status'] == 'recovery_required', 'PENDING_NOT_RECORDED')
            require(configure.recover(app)['status'] == 'restored' and app.snapshot()['healthy'], 'ACTUAL_CRASH_RECOVERY_FAILED')
            passed('interrupted-two-file-transition-recovers-real-containers')
            require(configure.apply(app, app.request(opened))['status'] == 'configured', 'REOPEN_FOR_COMMIT_TEST_FAILED')
            def crash_after_commit(event):
                if event == 'write:'+configure.PENDING and json.loads((app.STATE/configure.PENDING).read_bytes())['phase'] == 'committed':
                    raise InjectedCrash()
            app.fault = crash_after_commit
            try: configure.apply(app, app.request(closed))
            except InjectedCrash: pass
            else: raise ValueError('COMMIT_CRASH_NOT_INJECTED')
            app.fault = None
            before = configure.identities(app, app.inspect())
            require(configure.recover(app).get('recoveredCommit') is True, 'COMMITTED_RECOVERY_FAILED')
            require(configure.identities(app, app.inspect()) == before, 'COMMITTED_RECOVERY_RESTARTED_SERVICES')
            require(app.http('teacher', '/agent/access')['body']['canStart'] is False, 'COMMITTED_RECOVERY_REOPENED_ACCOUNT')
            passed('committed-revocation-recovery-never-reopens-or-restarts')
            report['status'] = 'passed'
        except BaseException as error:
            report['status'] = 'failed'
            report['error'] = configure.error_code(error)
            if getattr(app, 'last_error', None): report['commandDiagnostic'] = app.last_error
            raise
        finally:
            app.fault = None
            app.command = Application.command.__get__(app)
            if started:
                app.command(app.compose_argv()+['down', '--volumes', '--remove-orphans', '--timeout', '30'], timeout=90)
            remaining = app.command(docker+['ps', '-aq', '--filter', 'label=com.docker.compose.project='+project]).strip()
            networks = app.command(docker+['network', 'ls', '-q', '--filter', 'label=com.docker.compose.project='+project]).strip()
            report['testResourcesRemoved'] = not remaining and not networks
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
