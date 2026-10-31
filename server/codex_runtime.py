"""Fixed Codex services and networks; root-owned settings never enter a build."""
import copy
import json
import re
from urllib.parse import urlsplit

PROJECT = 'teaching-production'
SERVICES = ('codex-agent-runtime',)
NETWORKS = ('codex-api',)
REGISTRY = 'docker.cnb.cool/shukezhinao/edu-agent-cloud-build/codex-runtime@sha256:'
PROTOCOL = 'teaching-codex-plan-v1'
API_KEYS = ('AGENT_WORKFLOWS_ENABLED', 'AGENT_ROLLOUT_MODE', 'AGENT_ROLLOUT_USER_IDS',
            'CODEX_AGENT_RUNNER_URL', 'CODEX_AGENT_RUNNER_TOKEN', 'CODEX_AGENT_EGRESS_ENABLED',
            'CODEX_AGENT_EGRESS_TOKEN', 'CODEX_AGENT_ALLOWED_MODEL_BASE_URLS', 'CODEX_AGENT_EGRESS_BIND_HOST')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def settings(value):
    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'runnerToken', 'egressToken',
            'modelBaseUrls', 'rolloutMode', 'userIds'}, 'CODEX_CONFIGURATION_FIELDS')
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1, 'CODEX_CONFIGURATION_VERSION')
    for name in ('runnerToken', 'egressToken'):
        require(isinstance(value[name], str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value[name]), 'CODEX_SERVICE_TOKEN_INVALID')
    require(value['runnerToken'] != value['egressToken'], 'CODEX_DISTINCT_TOKENS_REQUIRED')
    require(value['rolloutMode'] in ('closed', 'allowlist'), 'CODEX_PILOT_MODE_REQUIRED')
    ids = value['userIds']
    require(isinstance(ids, list) and len(ids) <= 20 and all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', x) for x in ids)
            and len(ids) == len(set(ids)), 'CODEX_PILOT_ACCOUNTS_INVALID')
    require(value['rolloutMode'] != 'allowlist' or bool(ids), 'CODEX_PILOT_ACCOUNTS_REQUIRED')
    urls = value['modelBaseUrls']
    require(isinstance(urls, list) and 1 <= len(urls) <= 8, 'CODEX_MODEL_ENDPOINTS_REQUIRED')
    for text in urls:
        require(isinstance(text, str) and len(text) <= 512, 'CODEX_MODEL_ENDPOINT_INVALID')
        url = urlsplit(text)
        require(url.scheme == 'https' and not url.username and not url.password and not url.query and not url.fragment and
                url.hostname and re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', url.hostname) and
                '.' in url.hostname and not re.fullmatch(r'[0-9.]+', url.hostname) and url.netloc == url.hostname and
                re.fullmatch(r'/[A-Za-z0-9_/-]*', url.path) and not text.endswith('/'), 'CODEX_MODEL_ENDPOINT_INVALID')
    require(len(urls) == len(set(urls)), 'CODEX_MODEL_ENDPOINT_DUPLICATE')
    return copy.deepcopy(value)


def load_settings(ops):
    path = ops.ROOT / 'environments/codex-runtime.json'
    ops.safe_file(path)
    require(path.stat().st_size <= 16384, 'CODEX_CONFIGURATION_SIZE')
    return settings(json.loads(path.read_bytes()))


def runtime_identity(value):
    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'protocol', 'sdkVersion'} and
            type(value['schemaVersion']) is int and value['schemaVersion'] == 1 and value['protocol'] == PROTOCOL and
            isinstance(value['sdkVersion'], str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value['sdkVersion']), 'CODEX_RUNTIME_PROTOCOL')
    return value


def extension(image, runtime, config):
    """Merge into the existing release override. No ports, volumes or data credentials."""
    config = settings(config); runtime_identity(runtime)
    require(isinstance(image, str) and re.fullmatch(re.escape(REGISTRY) + '[0-9a-f]{64}', image), 'CODEX_IMAGE_SCOPE')
    common = dict(image=image, init=True, read_only=True, user='1000:1000', cap_drop=['ALL'],
                  security_opt=['no-new-privileges:true'], restart='unless-stopped', stop_grace_period='15s')
    runner = dict(common, cpus=1, mem_limit='768m', pids_limit=128,
                  tmpfs=['/tmp:rw,noexec,nosuid,size=128m'], networks={'codex-api': None},
                  command=['node', 'server.mjs'], environment={
                      'NODE_ENV': 'production', 'HOST': '0.0.0.0', 'PORT': '4986',
                      'CODEX_AGENT_RUNNER_TOKEN': config['runnerToken'],
                      'CODEX_AGENT_ALLOWED_MODEL_HOSTS': ','.join(sorted({urlsplit(x).hostname for x in config['modelBaseUrls']})),
                      'CODEX_AGENT_RUNNER_CONCURRENCY': '1', 'CODEX_AGENT_EGRESS_REQUIRED': 'true',
                      'CODEX_AGENT_EGRESS_URL': 'http://codex-agent-egress:4987/responses',
                      'CODEX_AGENT_EGRESS_TOKEN': config['egressToken']})
    for service, port in ((runner, 4986),):
        expected = json.dumps(runtime, sort_keys=True, separators=(',', ':'))
        code = ("fetch('http://127.0.0.1:"+str(port)+"/health').then(async r=>{const v=await r.json();"
                "const e="+expected+";if(!r.ok||!v.ok||!v.runtime||Object.keys(e).some(k=>e[k]!==v.runtime[k]))process.exit(1)})"
                ".catch(()=>process.exit(1))")
        service['healthcheck'] = dict(test=['CMD', 'node', '-e', code], interval='15s', timeout='5s', start_period='10s', retries=3)
    return dict(services={'api': {'networks': {'codex-api': {'aliases':['codex-agent-egress']}}, 'environment': {
        'AGENT_WORKFLOWS_ENABLED': 'true', 'AGENT_ROLLOUT_MODE': config['rolloutMode'],
        'AGENT_ROLLOUT_USER_IDS': ','.join(config['userIds']),
        'CODEX_AGENT_RUNNER_URL': 'http://codex-agent-runtime:4986', 'CODEX_AGENT_RUNNER_TOKEN': config['runnerToken'],
        'CODEX_AGENT_EGRESS_ENABLED':'true', 'CODEX_AGENT_EGRESS_BIND_HOST':'codex-agent-egress', 'CODEX_AGENT_EGRESS_TOKEN':config['egressToken'],
        'CODEX_AGENT_ALLOWED_MODEL_BASE_URLS':','.join(config['modelBaseUrls'])}},
        SERVICES[0]: runner}, networks={name: {
            'name': PROJECT + '_' + name, 'driver': 'bridge', 'internal': True} for name in NETWORKS})


def strip_extension(base, candidate, expected):
    """Validate resolved Compose against a separately rendered trusted extension.

    Returns the base-shaped view for the existing image/web-only release policy.
    Callers must derive expected from root settings + verified image metadata,
    never from candidate environment or candidate network declarations.
    """
    candidate = copy.deepcopy(candidate)
    present = set(candidate['services']) & set(SERVICES)
    if not present:
        return candidate
    require(present == set(SERVICES), 'CODEX_RUNTIME_REQUIRED')
    for name in SERVICES:
        require(candidate['services'][name] == expected['services'][name], 'CODEX_SERVICE_POLICY')
        del candidate['services'][name]
    for name in NETWORKS:
        require(candidate.get('networks', {}).get(name) == expected['networks'][name], 'CODEX_NETWORK_POLICY')
        del candidate['networks'][name]
    api = candidate['services']['api']; original = base['services']['api']
    require(api.get('networks', {}).get('codex-api') == expected['services']['api']['networks']['codex-api'] and
            'codex-api' in api.get('networks', {}), 'CODEX_API_NETWORK')
    del api['networks']['codex-api']
    for key in API_KEYS:
        require(api.get('environment', {}).get(key) == expected['services']['api']['environment'][key], 'CODEX_API_CONFIGURATION')
        if key in original.get('environment', {}): api['environment'][key] = original['environment'][key]
        else: del api['environment'][key]
    # Other mutations remain visible to the existing complete-config comparison.
    return candidate


def inspect_isolation(container, name, expected):
    """Actual Docker state, in addition to Compose intent. Does not echo credentials."""
    require(name in SERVICES, 'CODEX_CONTAINER_SCOPE')
    host = container['HostConfig']; config = container['Config']; service = expected['services'][name]
    require(host.get('ReadonlyRootfs') is True and host.get('Privileged') is False and config.get('User') == '1000:1000' and
            host.get('CapDrop') == ['ALL'] and not host.get('CapAdd') and not host.get('Binds') and
            not host.get('PortBindings') and not host.get('Devices') and not host.get('DeviceRequests') and
            host.get('PidMode', '') == '' and host.get('IpcMode') == 'private' and
            host.get('SecurityOpt') == ['no-new-privileges:true'], 'CODEX_ACTUAL_ISOLATION')
    require(config.get('Cmd') == service['command'] and not config.get('Entrypoint'), 'CODEX_ACTUAL_COMMAND')
    require(set(container['NetworkSettings']['Networks']) == {PROJECT+'_'+x for x in service['networks']}, 'CODEX_ACTUAL_NETWORKS')
    require(all(m.get('Type') == 'tmpfs' and m.get('Destination') == '/tmp' for m in container.get('Mounts', [])), 'CODEX_ACTUAL_MOUNTS')
    require(set(host.get('Tmpfs', {})) == {'/tmp'} and host['PidsLimit'] == service['pids_limit'] and
            host['Memory'] == int(service['mem_limit']) and host['NanoCpus'] == int(float(service['cpus'])*1e9), 'CODEX_ACTUAL_LIMITS')
    env = dict(item.split('=', 1) for item in config.get('Env', []))
    allowed_base = {'PATH', 'NODE_VERSION', 'YARN_VERSION'}
    require(all(env.get(key) == value for key, value in service['environment'].items()) and
            set(env) <= set(service['environment']) | allowed_base, 'CODEX_ACTUAL_CREDENTIAL_SCOPE')


def trusted_extension(ops, image, approved_settings=None):
    require(isinstance(image, str) and re.fullmatch(re.escape(REGISTRY)+'[0-9a-f]{64}', image), 'CODEX_IMAGE_SCOPE')
    actual = json.loads(ops.run(ops.DOCKER+['image','inspect',image]))[0]
    config = actual.get('Config', {}); labels = config.get('Labels', {})
    require(image in actual.get('RepoDigests', []) and actual.get('Os') == 'linux' and actual.get('Architecture') == 'amd64' and
            config.get('User') == 'node' and config.get('Cmd') == ['node','server.mjs'] and not config.get('Entrypoint'), 'CODEX_IMAGE_IDENTITY')
    identity = runtime_identity(dict(schemaVersion=1, protocol=labels.get('io.shukezhinao.codex.protocol'),
                                     sdkVersion=labels.get('io.shukezhinao.codex.sdk-version')))
    return extension(image, identity, load_settings(ops) if approved_settings is None else settings(approved_settings))


def normalize_extension(ops, value, api_image):
    value = copy.deepcopy(value)
    value['name'] = PROJECT
    value['services']['api']['image'] = api_image
    return json.loads(ops.run(ops.DOCKER+['compose','--project-name',PROJECT,'-f','-',
                                        'config','--format','json'], data=json.dumps(value).encode()))


def strip_release(ops, base, merged, approved_settings=None):
    present = set(merged['services']) & set(SERVICES)
    if not present:
        return copy.deepcopy(merged)
    require(present == set(SERVICES), 'CODEX_RUNTIME_REQUIRED')
    image = merged['services'][SERVICES[0]].get('image')
    value = trusted_extension(ops, image, approved_settings)
    expected = normalize_extension(ops, value, base['services']['api']['image'])
    return strip_extension(base, merged, expected)


def inspect_api(container, merged):
    """Detect a stale API token/admission environment after a partial restart."""
    expected = merged['services']['api']
    actual = dict(item.split('=', 1) for item in container['Config'].get('Env', []))
    require(all(actual.get(key) == expected['environment'][key] for key in API_KEYS), 'CODEX_ACTUAL_API_CONFIGURATION')
    require(set(container['NetworkSettings']['Networks']) ==
            {merged['networks'][name]['name'] for name in expected['networks']}, 'CODEX_ACTUAL_API_NETWORKS')


def inspect_networks(ops, merged):
    if not (set(merged['services']) & set(SERVICES)):
        return
    actual = json.loads(ops.run(ops.DOCKER+['network','inspect',*[PROJECT+'_'+name for name in NETWORKS]]))
    by_name = {network['Name']:network for network in actual}
    require(set(by_name) == {PROJECT+'_'+name for name in NETWORKS}, 'CODEX_ACTUAL_NETWORK_SET')
    for name in NETWORKS:
        network = by_name[PROJECT+'_'+name]
        require(network.get('Driver') == 'bridge' and network.get('Internal') is True and
                network.get('Labels',{}).get('com.docker.compose.project') == PROJECT, 'CODEX_ACTUAL_NETWORK_POLICY')


def probe(ops, api_id, identity):
    """Check both private endpoints from the API without credentials or model calls."""
    runtime_identity(identity)
    require(isinstance(api_id, str) and re.fullmatch('[0-9a-f]{12,64}', api_id), 'CODEX_API_IDENTITY')
    script = ("const expected="+json.dumps(identity)+";"
        "const get=async u=>{const r=await fetch(u,{signal:AbortSignal.timeout(5000)});"
        "if(!r.ok)throw new Error('health');return r.json()};"
        "const [runner,egress]=await Promise.all([get('http://codex-agent-runtime:4986/health'),"
        "get('http://codex-agent-egress:4987/health')]);"
        "if(!runner.ok||Object.keys(expected).some(k=>runner.runtime?.[k]!==expected[k])||"
        "!egress.ok||egress.protocol!=='teaching-codex-egress-v1')process.exit(1);"
        "const denied=await fetch('http://codex-agent-egress:4987/responses',"
        "{method:'POST',body:'{}',signal:AbortSignal.timeout(5000)});"
        "if(denied.status!==401)process.exit(1);"
        "console.log(JSON.stringify({runtime:runner.runtime,egressProtocol:egress.protocol,unauthorizedRequestDenied:true}))")
    value = json.loads(ops.run(ops.DOCKER+['exec','-i',api_id,'node','--input-type=module'],
                              data=script.encode(), timeout=15))
    require(value == dict(runtime=identity, egressProtocol='teaching-codex-egress-v1', unauthorizedRequestDenied=True),
            'CODEX_PRIVATE_HEALTH_MISMATCH')
    return value
