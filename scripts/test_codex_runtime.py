import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('codex_runtime', Path(__file__).resolve().parents[1]/'server/codex_runtime.py')
runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
IDENTITY = dict(schemaVersion=1, protocol='teaching-codex-plan-v1', sdkVersion='0.165.0')
IMAGE = runtime.REGISTRY+'a'*64


def configuration():
    return dict(schemaVersion=1, runnerToken='r'*40, egressToken='e'*40,
                modelBaseUrls=['https://model.example.com/v1'], rolloutMode='closed', userIds=[])


class RuntimePolicyTests(unittest.TestCase):
    def test_settings_require_distinct_capabilities_and_explicit_bounded_pilot(self):
        self.assertEqual(runtime.settings(configuration()), configuration())
        for change in (
            lambda x: x.update(rolloutMode='all'), lambda x: x.update(rolloutMode='allowlist'),
            lambda x: x.update(egressToken=x['runnerToken']), lambda x: x.update(runnerToken='short'),
            lambda x: x.update(modelApiKey='not-allowed'), lambda x: x.update(userIds=['a','a']),
            lambda x: x.update(userIds=['a']*21), lambda x: x.update(schemaVersion=True),
            lambda x: x.update(modelBaseUrls=['https://127.0.0.1/v1']),
            lambda x: x.update(modelBaseUrls=['https://model.example.com:444/v1']),
            lambda x: x.update(modelBaseUrls=['https://user:secret@model.example.com/v1']),
            lambda x: x.update(modelBaseUrls=['https://model.example.com/v1?key=secret']),
        ):
            value=configuration(); change(value)
            with self.assertRaises(ValueError): runtime.settings(value)

    def test_sdk_has_only_an_internal_network_and_reuses_the_existing_api_as_gateway(self):
        value=runtime.extension(IMAGE, IDENTITY, configuration())
        self.assertEqual(set(value['services']), {'api','codex-agent-runtime'})
        self.assertEqual(set(value['networks']), {'codex-api'})
        self.assertTrue(value['networks']['codex-api']['internal'])
        api=value['services']['api']; runner=value['services']['codex-agent-runtime']
        self.assertEqual(set(runner['networks']), {'codex-api'})
        self.assertEqual(api['networks']['codex-api']['aliases'], ['codex-agent-egress'])
        self.assertEqual(api['environment']['AGENT_ROLLOUT_MODE'],'closed')
        self.assertEqual(api['environment']['CODEX_AGENT_EGRESS_BIND_HOST'],'codex-agent-egress')
        self.assertEqual(runner['environment']['CODEX_AGENT_EGRESS_REQUIRED'],'true')
        self.assertNotEqual(api['environment']['CODEX_AGENT_EGRESS_TOKEN'],api['environment']['CODEX_AGENT_RUNNER_TOKEN'])
        self.assertFalse(runner.get('volumes')); self.assertFalse(runner.get('ports'))
        self.assertTrue(runner['read_only']); self.assertEqual(runner['user'],'1000:1000')
        self.assertEqual(runner['cap_drop'],['ALL'])
        self.assertNotIn('DATABASE_URL',runner['environment']); self.assertNotIn('REDIS_URL',runner['environment'])
        with self.assertRaisesRegex(ValueError,'IMAGE_SCOPE'): runtime.extension('codex:latest',IDENTITY,configuration())

    def candidate(self):
        expected=runtime.extension(IMAGE,IDENTITY,configuration())
        base=dict(services={'api':dict(image='api',environment={'DATABASE_URL':'synthetic'},networks={'app':None}),
                            'postgres':dict(image='postgres')},networks={'app':dict(name='app')})
        value=copy.deepcopy(base)
        value['services'].update({name:copy.deepcopy(expected['services'][name]) for name in runtime.SERVICES})
        value['services']['api']['environment'].update(expected['services']['api']['environment'])
        value['services']['api']['networks'].update(expected['services']['api']['networks'])
        value['networks'].update(expected['networks'])
        return base,value,expected

    def test_complete_config_policy_rejects_network_ports_credentials_and_unapproved_accounts(self):
        base,candidate,expected=self.candidate()
        self.assertEqual(runtime.strip_extension(base,candidate,expected),base)
        for change in (
            lambda c:c['networks']['codex-api'].update(internal=False),
            lambda c:c['services']['codex-agent-runtime'].update(ports=['4986:4986']),
            lambda c:c['services']['codex-agent-runtime']['networks'].update(app=None),
            lambda c:c['services']['codex-agent-runtime']['environment'].update(DATABASE_URL='secret'),
            lambda c:c['services']['codex-agent-runtime'].update(volumes=['/var/run/docker.sock:/sock']),
            lambda c:c['services']['api']['environment'].update(AGENT_ROLLOUT_MODE='all'),
            lambda c:c['services']['api']['environment'].update(AGENT_ROLLOUT_USER_IDS='unapproved'),
            lambda c:c['services']['codex-agent-runtime']['environment'].update(CODEX_AGENT_EGRESS_REQUIRED='false'),
        ):
            mutated=copy.deepcopy(candidate);change(mutated)
            with self.assertRaises(ValueError):runtime.strip_extension(base,mutated,expected)
        changed=copy.deepcopy(candidate);changed['services']['postgres']['image']='unapproved'
        self.assertNotEqual(runtime.strip_extension(base,changed,expected),base)

    def test_old_packages_need_no_runtime_settings_and_preserve_the_old_policy_view(self):
        base,_,_=self.candidate()
        self.assertEqual(runtime.strip_extension(base,base,None),base)

    def test_actual_api_must_use_current_tokens_admission_and_only_declared_networks(self):
        expected=runtime.extension(IMAGE,IDENTITY,configuration())
        expected['services']['api']['networks']['app']=None
        expected['networks']['app']={'name':'teaching-production_app'}
        actual=dict(Config=dict(Env=[key+'='+value for key,value in expected['services']['api']['environment'].items()]),
                    NetworkSettings=dict(Networks={'teaching-production_app':{},'teaching-production_codex-api':{}}))
        runtime.inspect_api(actual,expected)
        for key in ('CODEX_AGENT_RUNNER_TOKEN','CODEX_AGENT_EGRESS_TOKEN','AGENT_ROLLOUT_MODE','AGENT_ROLLOUT_USER_IDS'):
            changed=copy.deepcopy(actual)
            changed['Config']['Env']=[item if not item.startswith(key+'=') else key+'=old' for item in changed['Config']['Env']]
            with self.assertRaisesRegex(ValueError,'API_CONFIGURATION'):runtime.inspect_api(changed,expected)
        actual['NetworkSettings']['Networks']['unapproved']={}
        with self.assertRaisesRegex(ValueError,'API_NETWORKS'):runtime.inspect_api(actual,expected)

    def test_actual_limits_accept_compose_byte_strings_but_reject_missing_limits_and_extra_credentials(self):
        expected=runtime.extension(IMAGE,IDENTITY,configuration());name=runtime.SERVICES[0]
        service=expected['services'][name];service['mem_limit']='805306368'
        actual=dict(HostConfig=dict(ReadonlyRootfs=True,Privileged=False,CapDrop=['ALL'],SecurityOpt=['no-new-privileges:true'],
                    IpcMode='private',Tmpfs={'/tmp':'rw,noexec,nosuid,size=128m'},PidsLimit=128,Memory=805306368,NanoCpus=1000000000),
                    Config=dict(User='1000:1000',Cmd=['node','server.mjs'],Env=[k+'='+v for k,v in service['environment'].items()]),
                    Mounts=[],NetworkSettings=dict(Networks={runtime.PROJECT+'_'+n:{} for n in service['networks']}))
        runtime.inspect_isolation(actual,name,expected)
        for change in (lambda x:x['HostConfig'].update(Memory=0),lambda x:x['HostConfig'].update(NanoCpus=0),
                       lambda x:x['Config']['Env'].append('DATABASE_URL=secret')):
            value=copy.deepcopy(actual);change(value)
            with self.assertRaises(ValueError):runtime.inspect_isolation(value,name,expected)


if __name__ == '__main__': unittest.main()
