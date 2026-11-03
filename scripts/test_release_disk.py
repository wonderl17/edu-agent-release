"""Offline disk-boundary and controller ordering checks; no builds or SSH."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tencent_production as controller


class ReleaseDiskTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'server/tencent_receiver.py'
        spec = importlib.util.spec_from_file_location('release_disk_test', path)
        self.receiver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.receiver)

    def test_backend_boundaries_cover_the_final_guard(self):
        r = self.receiver
        for backend, bundle_limit in [('cnb', 12000), ('cos', r.MAX_RELAY)]:
            required = r.MAX_IMAGES + bundle_limit + r.MAX_WEB + 2 * 1024**3
            with self.subTest(backend=backend):
                with patch.object(r.shutil, 'disk_usage', return_value=SimpleNamespace(free=required-1)), \
                     self.assertRaisesRegex(r.Refused, '^INSUFFICIENT_RELEASE_DISK$'):
                    r.build_preflight({'backend': backend})
                with patch.object(r.shutil, 'disk_usage', return_value=SimpleNamespace(free=required)):
                    result = r.build_preflight({'backend': backend})
                    self.assertEqual(result, dict(status='build_ready', availableBytes=required, requiredBytes=required))
                    self.assertEqual(r.require_release_disk(bundle_limit), result)
                    self.assertEqual(r.phase, 'build-preflight')
                # A successful earlier check cannot bypass the deployment guard.
                with patch.object(r.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), \
                     self.assertRaisesRegex(r.Refused, '^INSUFFICIENT_RELEASE_DISK$'):
                    r.require_release_disk(bundle_limit)

    def test_preflight_protocol_rejects_missing_or_unknown_backend_and_write_fields(self):
        r = self.receiver
        base = dict(protocol=r.PROTOCOL, action='build-preflight', sourceRepository=r.SOURCE,
                    sourceSha='a'*40, version='0.0.137', backend='cnb')
        def read(value):
            return r.header(io.BytesIO((json.dumps(value)+'\n').encode()))
        self.assertEqual(read(base), base)
        for value in [dict(base, backend='other'), dict(base, backend=None),
                      {k:v for k,v in base.items() if k != 'backend'}, dict(base, bundleBytes=1)]:
            with self.subTest(value=value), self.assertRaises(r.Refused): read(value)

    def test_low_disk_missing_helper_or_bad_response_stops_before_any_release_side_effect(self):
        for operation in ('stage', 'deploy', 'automatic'):
            for refusal in ('low-disk', 'old-helper', 'bad-response'):
                with self.subTest(operation=operation, refusal=refusal), tempfile.TemporaryDirectory() as directory:
                    events = []
                    env = dict(RUNNER_TEMP=directory, RELEASES_ENABLED='true', PUBLIC_REPOSITORY=controller.PUBLIC,
                               GITHUB_REF='refs/heads/main', GITHUB_SHA='a'*40, TENCENT_OPERATION=operation,
                               TENCENT_PRODUCTION_ENABLED='true', TENCENT_PRODUCTION_AUTOMATIC_ENABLED='true',
                               TENCENT_CONFIRM_RELEASE='yes', PUBLIC_GH_TOKEN='PUBLIC_SENTINEL')
                    if operation != 'automatic': env['TENCENT_SOURCE_SHA'] = 'b'*40
                    def clone(_sha, _token, source, *_):
                        (source / 'package.json').write_text('{"version":"0.0.137"}')
                    def remote(_ssh, _child, _log, request, *args):
                        events.append(request['action'])
                        if request['action'] == 'verify':
                            return dict(status='verified', configValid=True, healthy=True,
                                        version='0.0.136', currentSourceSha='c'*40)
                        self.assertEqual(request, dict(action='build-preflight', sourceSha='b'*40,
                                                       version='0.0.137', backend='cnb'))
                        if refusal == 'bad-response': return {'status': 'verified'}
                        raise controller.Failure(44)
                    def github(_token, method, path, *args):
                        if method == 'GET' and path.endswith('/commits/main'): return {'sha':'b'*40}
                        self.assertEqual((method, path), ('DELETE', '/installation/token'))
                    with contextlib.redirect_stdout(io.StringIO()), \
                         patch.object(controller, 'ssh_configuration', return_value=[]), \
                         patch.object(controller, 'remote', side_effect=remote), \
                         patch.object(controller, 'installation_token', return_value='TOKEN_SENTINEL'), \
                         patch.object(controller, 'clone_source', side_effect=clone), \
                         patch.object(controller, 'github_request', side_effect=github), \
                         patch.object(controller, 'report_usage') as usage, \
                         patch.object(controller, 'build_cnb') as cnb, \
                         patch.object(controller, 'build_bundle') as cos, \
                         self.assertRaises(controller.BuildPreflightFailure):
                        controller.execute(env)
                    self.assertEqual(events, ['verify', 'build-preflight'])
                    cnb.assert_not_called(); cos.assert_not_called()
                    self.assertEqual(usage.call_args.args[0], controller.not_used())

    def test_public_failure_category_preserves_capacity_reason(self):
        output = json.dumps(dict(status='failed', code='INSUFFICIENT_RELEASE_DISK', phase='build-preflight')).encode()
        self.assertEqual(controller.receiver_failure(output), ('INSUFFICIENT_RELEASE_DISK', 'build-preflight'))


if __name__ == '__main__': unittest.main()
