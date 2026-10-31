"""Versioned runner packaging, downgrade refusal and executable-free image inspection."""
import copy
import hashlib
import os
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import cnb_controller as controller
import cnb_release as contract
import tencent_registry as registry
from test_cnb_build import builder
import test_cnb_release as release_tests

fixture = release_tests.fixture


RUNTIME = dict(schemaVersion=1, protocol='teaching-codex-plan-v1', sdkVersion='0.165.0')


def package_files():
    package = dict(name='@teaching-ai-agent/codex-runtime', dependencies={'@openai/codex-sdk': '0.165.0'})
    sdk = dict(name='@openai/codex-sdk', version='0.165.0')
    lock = dict(packages={'': package, 'node_modules/@openai/codex-sdk': sdk})
    return {contract.RUNTIME_ROOT + name: contract.canonical(value) for name, value in
            [('release.json', RUNTIME), ('package.json', package), ('package-lock.json', lock)]} | {
            contract.RUNTIME_ROOT + 'Dockerfile': b'FROM node:22-bookworm-slim\n'}


def manifest():
    value = fixture()
    value.update(schemaVersion=3, runtime=copy.deepcopy(RUNTIME))
    value['images'][contract.RUNTIME_KIND] = dict(ref=contract.REGISTRY + '/codex-runtime@sha256:' + 'e'*64, id='sha256:'+'f'*64)
    value['timings'].update({'codex-runtime': 4, 'total': 10})
    return value


class RuntimeContractTests(unittest.TestCase):
    def test_old_and_new_manifests_have_exact_distinct_image_contracts(self):
        for value in (fixture(), manifest()):
            self.assertIs(contract.validate_manifest(value, 'a'*40, '0.0.126'), value)
        for change in (
            lambda v: v['images'].pop('codex-runtime'),
            lambda v: v['timings'].pop('codex-runtime'),
            lambda v: v.pop('runtime'),
            lambda v: v.update(schemaVersion=2),
            lambda v: v['runtime'].update(sdkVersion='^0.165.0'),
            lambda v: v['runtime'].update(protocol='free-agent'),
            lambda v: v['runtime'].update(schemaVersion=True),
            lambda v: v['images']['codex-runtime'].update(ref=contract.REGISTRY+'/codex-runtime:latest'),
            lambda v: v['images']['codex-runtime'].update(ref=contract.REGISTRY+'/api@sha256:'+'e'*64),
        ):
            value = manifest(); change(value)
            with self.assertRaises((ValueError, TypeError)): contract.validate_manifest(value, 'a'*40, '0.0.126')
        legacy = fixture(); legacy['images']['codex-runtime'] = manifest()['images']['codex-runtime']
        with self.assertRaisesRegex(ValueError, 'image set'): contract.validate_manifest(legacy, 'a'*40, '0.0.126')

    def test_explicit_source_opt_in_binds_declared_and_locked_sdk(self):
        files = package_files()
        self.assertEqual(contract.source_runtime(files.get), RUNTIME)
        old = dict(files); old.pop(contract.RUNTIME_FILES[0])
        self.assertIsNone(contract.source_runtime(old.get))
        for name in ('package.json', 'package-lock.json', 'Dockerfile'):
            broken = dict(files); broken.pop(contract.RUNTIME_ROOT + name)
            with self.assertRaises(ValueError): contract.source_runtime(broken.get)
        for key in ('', 'node_modules/@openai/codex-sdk'):
            broken = dict(files); lock = json.loads(broken[contract.RUNTIME_FILES[2]])
            if key: lock['packages'][key]['version'] = '0.164.0'
            else: lock['packages'][key]['dependencies']['@openai/codex-sdk'] = '^0.165.0'
            broken[contract.RUNTIME_FILES[2]] = contract.canonical(lock)
            with self.assertRaisesRegex(ValueError, 'dependency identity'): contract.source_runtime(broken.get)

    def test_source_symlink_cannot_opt_in_or_read_outside_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); context = root / contract.RUNTIME_ROOT; context.mkdir(parents=True)
            other = root/'other.json'; other.write_bytes(contract.canonical(RUNTIME))
            (context/'release.json').symlink_to(other)
            with self.assertRaisesRegex(ValueError, 'source file'): contract.filesystem_runtime(root)

    def test_required_runner_uses_isolated_context_and_existing_cache_but_is_not_optional(self):
        for runtime, count in ((None, 3), (RUNTIME, 4)):
            jobs = builder.image_jobs('a'*40, '0.0.126', 'd'*32, '/private', 'benchmark-abcdef012345', runtime)
            self.assertEqual(len(jobs), count)
            if runtime is None: continue
            kind, command, _metadata = jobs[-1]
            self.assertEqual(kind, 'codex-runtime')
            self.assertIn('--push', command); self.assertNotIn('--output=type=cacheonly', command)
            self.assertEqual(command[-1], 'tools/codex-agent-runtime')
            self.assertEqual(command[command.index('--file')+1], 'tools/codex-agent-runtime/Dockerfile')
            self.assertIn('io.shukezhinao.codex.sdk-version=0.165.0', command)
            self.assertIn('type=registry,ref='+contract.REGISTRY+'/cache-codex-runtime:main', command)
            exports = [command[i+1] for i, x in enumerate(command) if x == '--cache-to']
            self.assertEqual(len(exports), 1)
            self.assertIn('cache-codex-runtime:benchmark-abcdef012345,', exports[0])
            self.assertNotIn('ignore-error', exports[0])

    def test_controller_refuses_a_green_build_that_omits_or_changes_required_runtime(self):
        for actual in (fixture(), manifest()):
            actual = copy.deepcopy(actual)
            if actual['schemaVersion'] == 3: actual['runtime']['sdkVersion'] = '0.164.0'
            def api(_token, method, path, value=None):
                if path == '': return dict(visibility_level='Private', path=contract.REPO)
                if path.endswith('/start'):
                    actual['requestId'] = value['env']['RELEASE_REQUEST_ID']
                    return dict(success=True, sn='synthetic-build')
                if '/status/' in path: return dict(status='success')
                if '/git/commit-annotations/' in path:
                    return [dict(key='tencent-'+actual['requestId'], value=contract.canonical(actual).decode())]
                return []
            with tempfile.TemporaryDirectory() as directory, patch.object(controller, 'api', side_effect=api), \
                 patch.object(controller, 'mirror', return_value=('c'*40, 'b'*40, RUNTIME)):
                with self.assertRaisesRegex(ValueError, 'source tree mismatch'):
                    controller.build(None, 'a'*40, '0.0.126', Path(directory), {}, None, 'SYNTHETIC', None, lambda _: None)
                self.assertFalse((Path(directory)/'registry-manifest.json').exists())

    def test_source_handoff_reads_committed_runtime_contract_and_ignores_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; source.mkdir(); remote = root/'remote.git'
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=source, stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-q'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.com')
            for name, value in package_files().items():
                path = source/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            git('add', '.'); git('commit', '-qm', 'runtime'); sha = git('rev-parse', 'HEAD')
            (source/contract.RUNTIME_FILES[0]).write_bytes(contract.canonical(dict(RUNTIME, sdkVersion='0.999.0')))
            subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
            private = root/'private'; private.mkdir()
            child = release_tests.production.safe_environment(dict(os.environ), private)
            def run(command, env, log, **kw):
                command = [str(remote) if x == 'https://cnb.cool/'+contract.REPO+'.git' else x for x in command]
                return release_tests.production.run(command, env, log, **kw)
            with (root/'log').open('w+b') as log:
                _snapshot, _tree, runtime = controller.mirror(source, sha, '0.0.126', private, child, log, 'SYNTHETIC', run)
            self.assertEqual(runtime, RUNTIME)

    def test_schema_three_resume_preserves_all_four_digests_and_runtime_metadata(self):
        value = manifest(); raw = contract.canonical(value); writes = []
        def require(ok, code):
            if not ok: raise ValueError(code)
        c = types.SimpleNamespace(ops=types.SimpleNamespace(fixed_env=lambda: {'PRODUCTION_ACTIVATED': 'true'}),
            require=require, Refused=ValueError, OPS=Path('/synthetic'), atomic=lambda p,v: writes.append(v))
        h = dict(sourceRepository=contract.SOURCE, sourceSha='a'*40, version='0.0.126', registryManifest=value,
                 bundleBytes=len(raw), bundleSha256=hashlib.sha256(raw).hexdigest())
        first = registry.prepare(c, h); original = copy.deepcopy(writes[-1])
        second = registry.prepare(c, {k:v for k,v in h.items() if k != 'registryManifest'}, original)
        self.assertEqual(writes[-1]['registryManifest'], value)
        self.assertEqual(second['bundleSha256'], first['bundleSha256'])
        self.assertNotEqual(first['transferTicket'], second['transferTicket'])
        # Manifest identity is authoritative on resume: a changed SDK changes the payload hash.
        original['registryManifest']['runtime']['sdkVersion'] = '0.166.0'
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'): registry.prepare(c, h, original)

    def test_target_without_activation_support_refuses_before_writing_candidate(self):
        release_tests.ReceiverTests.setUpClass(); receiver = release_tests.ReceiverTests.receiver
        with patch.object(receiver, 'run_dir', Path('/synthetic')), \
             patch.object(receiver, 'atomic') as atomic, patch.object(receiver, 'command') as command:
            with self.assertRaisesRegex(receiver.Refused, 'CODEX_RUNTIME_ACTIVATION_NOT_INSTALLED'):
                receiver.candidate_preflight({}, manifest(), Path('/synthetic/web'))
            atomic.assert_not_called(); command.assert_not_called()


class RuntimeImageTests(unittest.TestCase):
    def inspect(self, root, change=None, fault=None):
        values = { 'release': copy.deepcopy(RUNTIME),
                   'package': json.loads(package_files()[contract.RUNTIME_FILES[1]]),
                   'sdk': dict(name='@openai/codex-sdk', version='0.165.0') }
        if change: change(values)
        commands = []
        def run(args):
            commands.append(args)
            if args[0] == 'create': return ('a'*64+'\n').encode()
            self.assertEqual(args[0], 'cp')
            path = Path(args[-1]); key = path.stem
            if fault and key == 'sdk': return fault(path)
            path.write_bytes(contract.canonical(values[key])); return b''
        try:
            return contract.inspect_runtime_image(RUNTIME, manifest()['images']['codex-runtime']['ref'], root, run,
                                                  lambda args: commands.append(args))
        finally:
            self.assertEqual(commands[-1], ['rm', 'a'*64])
            self.assertFalse((root/'codex-runtime-metadata').exists())
            self.assertFalse(any(c[0] in ('start', 'run', 'exec') for c in commands))

    def test_saved_image_metadata_matches_actual_installed_sdk_without_starting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.inspect(Path(directory)), dict(protocol=RUNTIME['protocol'], sdkVersion='0.165.0', installedPackageVerified=True))

    def test_mismatched_package_or_protocol_does_not_get_a_verification_receipt(self):
        for change in (lambda v: v['sdk'].update(version='0.164.0'),
                       lambda v: v['release'].update(protocol='free-agent'),
                       lambda v: v['package']['dependencies'].update({'@openai/codex-sdk': '^0.165.0'})):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'dependency identity'): self.inspect(Path(directory), change)

    def test_malformed_or_symlink_metadata_and_cancellation_still_remove_candidate_container(self):
        def cancel(_): raise KeyboardInterrupt()
        for fault, exception in ((lambda p: p.write_bytes(b'x'*16385), ValueError),
                                 (lambda p: p.symlink_to('/etc/passwd'), ValueError),
                                 (lambda p: p.write_bytes(b'invalid json'), ValueError), (cancel, KeyboardInterrupt)):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(exception): self.inspect(Path(directory), fault=fault)

    def test_target_checks_config_before_creating_candidate_or_copying_files(self):
        image = dict(Config=dict(User='node', WorkingDir='/app', Cmd=['node', 'server.mjs'], Entrypoint=None,
                                 Labels={label: RUNTIME[key] for key, label in contract.RUNTIME_LABELS.items()}))
        def require(ok, code):
            if not ok: raise ValueError(code)
        c = types.SimpleNamespace(require=require, Refused=ValueError)
        for change in (lambda v: v['Config'].update(User='root'),
                       lambda v: v['Config'].update(Cmd=['node', 'other.mjs']),
                       lambda v: v['Config'].update(Entrypoint=['sh']),
                       lambda v: v['Config']['Labels'].update({'io.shukezhinao.codex.sdk-version':'0.164.0'})):
            bad = copy.deepcopy(image); change(bad)
            with patch.object(registry, 'inspect_runtime_image') as inspect:
                with self.assertRaisesRegex(ValueError, 'CONFIG_MISMATCH'):
                    registry.runtime_identity(c, RUNTIME, manifest()['images']['codex-runtime'], bad)
                inspect.assert_not_called()


class RuntimeProducerTests(unittest.TestCase):
    def test_receipt_is_published_only_after_required_image_bytes_pass_and_legacy_keeps_three_images(self):
        for include_runtime, corrupt in ((True, False), (True, True), (False, False)):
            with self.subTest(runtime=include_runtime, corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); source = root/'source'; source.mkdir()
                (source/'package.json').write_text('{"version":"0.0.126"}')
                (source/'changelog').mkdir()
                (source/'changelog/0.0.126.md').write_text('---\nversion: 0.0.126\nstatus: published\n---\nSynthetic release\n')
                if include_runtime:
                    for name, raw in package_files().items():
                        path=source/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(raw)
                (root/'.release-builder').mkdir()
                (root/'.release-builder/source.json').write_bytes(contract.canonical(dict(sourceRepository=contract.SOURCE,
                    sourceSha='a'*40, sourceTree='b'*40, version='0.0.126')))
                value = manifest() if include_runtime else fixture(); events=[]
                image_values = {'/app/release.json':RUNTIME,
                    '/app/package.json':json.loads(package_files()[contract.RUNTIME_FILES[1]]),
                    '/app/node_modules/@openai/codex-sdk/package.json':dict(name='@openai/codex-sdk',version='0.164.0' if corrupt else '0.165.0')}
                def run(args, **kwargs):
                    events.append(args)
                    if args[:2] == ['git','rev-parse']: return ('b'*40+'\n').encode()
                    if args[:2] == ['docker','create']: return (('1' if '/web@' in args[2] else '2')*64+'\n').encode()
                    if args[:2] == ['docker','cp']:
                        content = dict(version='0.0.126',buildSha='a'*40) if 'version.json' in args[2] else image_values[args[2].split(':',1)[1]]
                        Path(args[3]).write_bytes(contract.canonical(content))
                    return b''
                def build(jobs, *args):
                    self.assertEqual({job[0] for job in jobs}, set(value['images']))
                    return copy.deepcopy(value['images']), {key:1 for key in value['images']}
                published=[]
                def publish(request, **kwargs):
                    published.append(json.loads(request.data))
                    class Response:
                        status=200
                        def __enter__(self): return self
                        def __exit__(self,*_): pass
                    return Response()
                environment=dict(CNB_REPO_SLUG_LOWERCASE=contract.REPO,CNB_EVENT='api_trigger_tencent',
                    CNB_COMMIT='c'*40,RELEASE_REQUEST_ID='d'*32,CNB_TOKEN='SYNTHETIC')
                previous = Path.cwd()
                try:
                    os.chdir(root)
                    with patch.dict(os.environ,environment,clear=True), patch.object(builder,'run',side_effect=run), \
                         patch.object(builder,'build_images',side_effect=build), patch.object(builder,'urlopen',side_effect=publish), \
                         patch.object(builder.subprocess,'run') as cleanup, \
                         patch.object(builder,'warm_codex_runtime_cache',return_value='absent') as cache:
                        if corrupt:
                            with self.assertRaisesRegex(ValueError,'dependency identity'): builder.main()
                            self.assertEqual(published,[])
                        else:
                            builder.main()
                            self.assertEqual(len(published),1)
                            actual=json.loads(published[0]['annotations'][0]['value'])
                            contract.validate_manifest(actual,'a'*40,'0.0.126','c'*40,'d'*32)
                            self.assertEqual(actual['images'],value['images'])
                            self.assertEqual(actual.get('runtime'),RUNTIME if include_runtime else None)
                        if include_runtime:
                            cache.assert_not_called(); cleanup.assert_called_once()
                        else: cache.assert_called_once(); cleanup.assert_not_called()
                finally: os.chdir(previous)


if __name__ == '__main__': unittest.main()
