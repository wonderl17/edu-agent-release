"""Offline safety boundaries for the full-image shutdown acceptance harness."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('api_image_shutdown',ROOT/'server/api_image_shutdown.py')
acceptance=importlib.util.module_from_spec(spec);spec.loader.exec_module(acceptance)


class ImageShutdownSafety(unittest.TestCase):
    def app(self, root):
        image=dict(Id='sha256:'+'a'*64,Config=dict(Entrypoint=acceptance.API_ENTRYPOINT,Cmd=acceptance.API_COMMAND))
        return acceptance.Harness(root,['docker'],dict(api=image,postgres=dict(Id='sha256:'+'b'*64),redis=dict(Id='sha256:'+'c'*64)),'api-shutdown-'+'d'*16)

    def test_disposable_stack_keeps_api_entrypoint_and_no_external_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=self.app(Path(temporary));config=json.loads(app.compose.read_text());services=config['services']
            self.assertNotIn('entrypoint',services['api']);self.assertNotIn('command',services['api'])
            self.assertTrue(all(network['internal'] for network in config['networks'].values()))
            self.assertTrue(all(not service.get('ports') and not service.get('privileged') for service in services.values()))
            self.assertLessEqual(sum(service['cpus'] for service in services.values()),2)
            self.assertNotIn('volumes',services['postgres']);self.assertNotIn('volumes',services['redis'])
            self.assertEqual(services['api']['volumes'],[str(app.fixture)+':/fixture:ro'])
            self.assertEqual(services['api']['environment']['CODEX_AGENT_EGRESS_ENABLED'],'false')

    def test_api_identity_rejects_replaced_entrypoint_production_mount_or_wrong_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=self.app(Path(temporary))
            item=dict(Image=app.images['api']['Id'],Config=dict(Entrypoint=acceptance.API_ENTRYPOINT,Cmd=acceptance.API_COMMAND,
                Labels={'teaching.shutdown.acceptance':app.project,'com.docker.compose.project':app.project}),
                HostConfig=dict(RestartPolicy=dict(Name='no')),Mounts=[dict(Source=str(app.fixture),Destination='/fixture',RW=False)])
            app.verify_api(item)
            for mutation in ('entrypoint','mount','project','privilege','port','image'):
                wrong=copy.deepcopy(item)
                if mutation=='entrypoint':wrong['Config']['Entrypoint']=['node']
                if mutation=='mount':wrong['Mounts'][0]['Source']='/opt/teaching-ai-production'
                if mutation=='project':wrong['Config']['Labels']['com.docker.compose.project']='teaching-production'
                if mutation=='privilege':wrong['HostConfig']['Privileged']=True
                if mutation=='port':wrong['HostConfig']['PortBindings']={'4999/tcp':[{'HostPort':'4999'}]}
                if mutation=='image':wrong['Image']='sha256:'+'e'*64
                with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'ORIGINAL_API_IDENTITY_REQUIRED'):app.verify_api(wrong)

    def test_helper_adaptation_only_changes_declared_scope_constants(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=self.app(Path(temporary));source=ROOT/'server/api_drain.py';helper=acceptance.Helper(app,source)
            original=source.read_text();adapted=helper.path.read_text()
            for row in reversed(helper.substitutions):
                self.assertEqual(adapted.count(row['after']),row['count'])
                adapted=adapted.replace(row['after'],row['before'])
            self.assertEqual(adapted,original)
            self.assertNotIn('/opt/teaching-ai-production',helper.path.read_text())
            self.assertIn("['stop','--time','30'",helper.path.read_text())
            self.assertIn("state.get('ExitCode')==(137 if forced else 0)",helper.path.read_text())

    def test_changed_helper_contract_is_refused_before_running_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);app=self.app(root);source=root/'changed-helper.py'
            source.write_text((ROOT/'server/api_drain.py').read_text().replace("RUN=Path('/run/teaching-production')","RUN=Path('/unexpected')"))
            with self.assertRaisesRegex(ValueError,'HELPER_ADAPTER_SOURCE_CHANGED'):acceptance.Helper(app,source)

    def test_fixture_javascript_is_syntactically_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name,source in [('upstream',acceptance.UPSTREAM),('clients',acceptance.CLIENTS)]:
                path=Path(temporary)/(name+'.cjs');path.write_text(source)
                subprocess.run(['node','--check',str(path)],check=True,capture_output=True)

    def runtime(self,flag='true'):
        marker=dict(protocolVersion=2,role='api',entrySha256='e'*64,controlSha256='c'*64)
        return dict(flag=flag,manifest=marker,entrySha256='e'*64,controlSha256='c'*64)

    def test_runtime_requires_exact_api_manifest_and_actual_bundle_hashes(self):
        value=self.runtime()
        self.assertTrue(acceptance.validate_admission_runtime(value,'true'))
        for change in (
            lambda v:v['manifest'].update(protocolVersion=True),
            lambda v:v['manifest'].update(protocolVersion=1),
            lambda v:v['manifest'].update(role='worker'),
            lambda v:v['manifest'].update(unreviewed=True),
            lambda v:v['manifest'].pop('controlSha256'),
            lambda v:v['manifest'].update(entrySha256='E'*64),
            lambda v:v.update(controlSha256='d'*64),
            lambda v:v.update(flag='false'),
        ):
            wrong=copy.deepcopy(value);change(wrong)
            with self.subTest(wrong=wrong),self.assertRaises(ValueError):acceptance.validate_admission_runtime(wrong,'true')

    def test_only_true_legacy_absence_can_skip_gate_seed(self):
        for flag in (None,'false'):
            value=dict(flag=flag,manifest=None,entrySha256=None,controlSha256=None)
            self.assertFalse(acceptance.validate_admission_runtime(value,flag))
            with tempfile.TemporaryDirectory() as temporary:
                app=self.app(Path(temporary));app.images['api']['Config']['Env']=[] if flag is None else ['DEPLOYMENT_ADMISSION_ENABLED='+flag]
                with patch.object(app,'node',return_value=json.dumps(value).encode()),patch.object(app,'db') as db:
                    self.assertFalse(app.prepare_admission()['isolatedGateSeeded']);db.assert_not_called()
        absent=dict(flag='true',manifest=None,entrySha256=None,controlSha256=None)
        with self.assertRaisesRegex(ValueError,'MARKER_REQUIRED'):acceptance.validate_admission_runtime(absent,'true')
        # An invalid marker is never legacy, even when the flag is disabled.
        wrong=self.runtime('false');wrong['manifest']['role']='worker'
        with self.assertRaises(ValueError):acceptance.validate_admission_runtime(wrong,'false')
        for env in (['DEPLOYMENT_ADMISSION_ENABLED=yes'],['DEPLOYMENT_ADMISSION_ENABLED=true']*2):
            with self.assertRaises(ValueError):acceptance.image_admission_flag(dict(Config=dict(Env=env)))

    def test_probe_hashes_real_files_and_refuses_tampered_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);entry=b'api entry bytes';control=b'control CLI bytes'
            (root/'server.mjs').write_bytes(entry);(root/'deployment-admission-control.mjs').write_bytes(control)
            marker=dict(protocolVersion=2,role='api',entrySha256=hashlib.sha256(entry).hexdigest(),controlSha256=hashlib.sha256(control).hexdigest())
            (root/'deployment-admission-runtime.json').write_text(json.dumps(marker))
            script=acceptance.ADMISSION_RUNTIME_PROBE.replace("'/app/apps/api/dist'",json.dumps(str(root)))
            env=dict(os.environ,DEPLOYMENT_ADMISSION_ENABLED='true')
            def probe():return json.loads(subprocess.check_output(['node','--input-type=commonjs'],input=script.encode(),env=env))
            self.assertTrue(acceptance.validate_admission_runtime(probe(),'true'))
            (root/'deployment-admission-control.mjs').write_bytes(control+b' tampered')
            with self.assertRaisesRegex(ValueError,'BUNDLE_MISMATCH'):acceptance.validate_admission_runtime(probe(),'true')
            (root/'deployment-admission-runtime.json').unlink()
            with self.assertRaisesRegex(ValueError,'MARKER_REQUIRED'):acceptance.validate_admission_runtime(probe(),'true')
            env.pop('DEPLOYMENT_ADMISSION_ENABLED')
            self.assertFalse(acceptance.validate_admission_runtime(probe(),None))

    def test_enabled_v2_seeds_only_fresh_isolated_database_and_checks_actual_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=self.app(Path(temporary));app.images['api']['Config']['Env']=['DEPLOYMENT_ADMISSION_ENABLED=true']
            self.assertNotIn('DEPLOYMENT_ADMISSION_ENABLED',app.env) # Preserve candidate image ENV.
            sources=[]
            def db(source,name='shutdown_acceptance'):
                sources.append(source)
                prefix="const writes=[];const p={$queryRawUnsafe:async()=>[{name:"+json.dumps(name)+"}],deploymentGate:{create:async({data})=>{writes.push(data);if(data.id!=='production'||data.state!=='accepting'||data.generation!==0n)throw Error('WRONG_SEED');return data}}};"
                output=json.loads(subprocess.check_output(['node','--input-type=commonjs'],input=(prefix+"(async()=>{"+source+"})().catch(e=>console.log(JSON.stringify({error:e.message,writes})))").encode()))
                if 'error' in output:
                    self.assertEqual(output['writes'],[])
                    raise ValueError(output['error'])
                return output
            with patch.object(app,'node',return_value=json.dumps(self.runtime()).encode()),patch.object(app,'db',side_effect=db):
                self.assertEqual(app.prepare_admission(),dict(protocolVersion=2,enabled=True,isolatedGateSeeded=True))
            self.assertIn('deploymentGate.create(',sources[0]);self.assertNotIn('upsert',sources[0])
            with patch.object(app,'node',return_value=json.dumps(self.runtime()).encode()),patch.object(app,'db',side_effect=lambda s:db(s,'production')):
                with self.assertRaisesRegex(ValueError,'ISOLATED_ADMISSION_DATABASE_REQUIRED'):app.prepare_admission()

    def test_seed_scope_refuses_production_url_project_or_mounted_database_before_write(self):
        for mutation in ('host','database','project','compose_database','volume'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as temporary:
                app=self.app(Path(temporary));app.images['api']['Config']['Env']=['DEPLOYMENT_ADMISSION_ENABLED=true']
                if mutation=='host':app.env['DATABASE_URL']=app.env['DATABASE_URL'].replace('@postgres:', '@production:')
                if mutation=='database':app.env['DATABASE_URL']=app.env['DATABASE_URL'].replace('/shutdown_acceptance?', '/production?')
                if mutation=='project':app.project='teaching-production'
                if mutation in ('compose_database','volume'):
                    config=json.loads(app.compose.read_text())
                    if mutation=='compose_database':config['services']['postgres']['environment']['POSTGRES_DB']='production'
                    else:config['services']['postgres']['volumes']=['/opt/teaching-ai-production:/var/lib/postgresql/data']
                    app.compose.write_text(json.dumps(config))
                with patch.object(app,'node',return_value=json.dumps(self.runtime()).encode()),patch.object(app,'db') as db:
                    with self.assertRaisesRegex(ValueError,'ISOLATED_ADMISSION_DATABASE_REQUIRED'):app.prepare_admission()
                    db.assert_not_called()


if __name__=='__main__':unittest.main()
