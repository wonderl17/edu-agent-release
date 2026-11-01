"""Offline safety boundaries for the full-image shutdown acceptance harness."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


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


if __name__=='__main__':unittest.main()
