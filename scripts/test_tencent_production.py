import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import tencent_production as target


class ProductionBoundaryTests(unittest.TestCase):
    def environment(self, **changes):
        result = {"RELEASES_ENABLED": "true", "PUBLIC_REPOSITORY": target.PUBLIC, "GITHUB_REF": "refs/heads/main",
                  "GITHUB_SHA": "a" * 40, "TENCENT_OPERATION": "verify"}
        result.update(changes)
        return result

    def test_only_main_and_fixed_public_repository(self):
        self.assertEqual(target.configuration(self.environment()), ("verify", ""))
        for update in ({"GITHUB_REF": "refs/pull/1/merge"}, {"PUBLIC_REPOSITORY": "other/repo"},
                       {"TENCENT_SOURCE_SHA": "main"}, {"GITHUB_SHA": "short"},
                       {"TENCENT_OPERATION": "cloud"}):
            with self.subTest(update=update), self.assertRaises(target.Failure):
                target.configuration(self.environment(**update))

    def test_manual_deploy_requires_both_authorization_and_enable(self):
        env = self.environment(TENCENT_OPERATION="deploy")
        with self.assertRaises(target.Failure):
            target.configuration(env)
        env["TENCENT_CONFIRM_RELEASE"] = "yes"
        with self.assertRaises(target.Failure):
            target.configuration(env)
        env["TENCENT_PRODUCTION_ENABLED"] = "true"
        self.assertEqual(target.configuration(env)[0], "deploy")

    def test_resume_requires_exact_source_authorization_and_a_bound_prior_ticket(self):
        env = self.environment(TENCENT_OPERATION="resume", TENCENT_CONFIRM_RELEASE="yes",
            TENCENT_PRODUCTION_ENABLED="true", TENCENT_SOURCE_SHA="b" * 40,
            TENCENT_PREVIOUS_TRANSFER_TICKET="c" * 32)
        self.assertEqual(target.configuration(env), ("resume", "b" * 40))
        for changes in ({"TENCENT_SOURCE_SHA": ""}, {"TENCENT_PREVIOUS_TRANSFER_TICKET": "../escape"},
                        {"TENCENT_CONFIRM_RELEASE": "no"}, {"TENCENT_PRODUCTION_ENABLED": "false"},
                        {"TENCENT_OPERATION": "deploy"}):
            with self.subTest(changes=changes), self.assertRaises(target.Failure):
                target.configuration(dict(env, **changes))

    def test_automatic_cannot_inject_a_source_ref(self):
        with self.assertRaises(target.Failure):
            target.configuration(self.environment(TENCENT_OPERATION="automatic",
                TENCENT_PRODUCTION_ENABLED="true", TENCENT_SOURCE_SHA="b" * 40))

    def test_failed_attempt_prevents_repeating_scheduled_release(self):
        remote = {"version": "0.0.125", "lastAttemptedVersion": "0.0.127"}
        self.assertFalse(target.should_deploy("automatic", "0.0.127", remote, "0.0.125"))
        self.assertFalse(target.should_deploy("automatic", "0.0.126", remote, "0.0.125"))
        self.assertTrue(target.should_deploy("automatic", "0.0.128", remote, "0.0.125"))
        self.assertTrue(target.should_deploy("deploy", "0.0.127", remote, "0.0.125"))

    def test_initial_version_is_not_automatically_redeployed(self):
        self.assertFalse(target.should_deploy("automatic", "0.0.125", {"version": "0.0.125"}, "0.0.125"))
        for bad in (None, "main", "0.0.125;echo injected", "01.0.0"):
            with self.subTest(value=bad), self.assertRaises(target.Failure):
                target.version_tuple(bad)

    def test_verified_current_source_is_not_rebuilt_or_replaced(self):
        remote = {"version": "0.0.125", "currentSourceSha": "a" * 40}
        self.assertFalse(target.should_deploy("deploy", "0.0.125", remote, "0.0.125", "a" * 40))
        self.assertTrue(target.should_deploy("deploy", "0.0.125", remote, "0.0.125", "b" * 40))

    def test_unhealthy_or_missing_runtime_proof_stops_before_source_access(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(RUNNER_TEMP=directory,
                TENCENT_PRODUCTION_DEPLOY_SSH_KEY="-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic",
                TENCENT_PRODUCTION_SSH_KNOWN_HOSTS=target.HOST+" ssh-ed25519 AAAATEST")
            for health in (False, None):
                with self.subTest(health=health), \
                        patch.object(target, "remote", return_value={"status": "verified", "configValid": True, "healthy": health}), \
                        patch.object(target, "installation_token") as source, \
                        self.assertRaises(target.Failure):
                    target.execute(env)
                source.assert_not_called()

    def test_disabled_scheduler_does_not_contact_any_target(self):
        with patch.object(target, "remote", side_effect=AssertionError("unexpected SSH")):
            target.execute(self.environment(TENCENT_OPERATION="automatic"))

    def test_verify_checks_cnb_without_starting_a_build_or_recording_attempt(self):
        def clone(sha, token, source, child, log):
            (source / 'package.json').write_text('{"version":"0.0.126"}')
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), tempfile.TemporaryDirectory() as directory:
                env = self.environment(RUNNER_TEMP=directory, TENCENT_SOURCE_SHA='b'*40, TENCENT_CNB_TOKEN='CNB_SENTINEL')
                with patch.object(target, 'ssh_configuration', return_value=[]), \
                     patch.object(target, 'remote', return_value=dict(status='verified', configValid=True, healthy=True)) as remote, \
                     patch.object(target, 'installation_token', return_value='APP_SENTINEL'), \
                     patch.object(target, 'clone_source', side_effect=clone), \
                     patch.object(target, 'verify_cnb_access', side_effect=ValueError('unavailable') if unavailable else None) as verify, \
                     patch.object(target, 'build_cnb') as build, patch.object(target, 'github_request') as github:
                    if unavailable:
                        with self.assertRaises(ValueError): target.execute(env)
                    else:
                        target.execute(env)
                verify.assert_called_once_with('CNB_SENTINEL')
                build.assert_not_called()
                self.assertEqual([c.args[3]['action'] for c in remote.call_args_list], ['verify'])
                github.assert_called_once_with('APP_SENTINEL', 'DELETE', '/installation/token')

    def test_runtime_source_rejects_legacy_cos_before_release_side_effects(self):
        sha = 'b' * 40
        def clone(_sha, _token, source, _child, _log):
            (source / 'package.json').write_text('{"version":"0.0.126"}')
            marker = source / target.RUNTIME_FILES[0]
            marker.parent.mkdir(parents=True)
            marker.write_text('{}')
        def github(_token, method, path, *args):
            if method == 'GET' and path.endswith('/commits/main'):
                return {'sha': sha}
            if method == 'DELETE':
                return None
            raise AssertionError('deployment metadata must not be created')
        for operation in ('verify', 'stage', 'deploy', 'automatic'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                env = self.environment(RUNNER_TEMP=directory, TENCENT_OPERATION=operation,
                    TENCENT_BUILD_BACKEND='cos', TENCENT_PRODUCTION_ENABLED='true',
                    TENCENT_PRODUCTION_AUTOMATIC_ENABLED='true', TENCENT_CONFIRM_RELEASE='yes')
                if operation != 'automatic':
                    env['TENCENT_SOURCE_SHA'] = sha
                with patch.object(target, 'ssh_configuration', return_value=[]), \
                     patch.object(target, 'remote', return_value=dict(status='verified', configValid=True,
                         healthy=True, version='0.0.125', currentSourceSha='c'*40)) as remote, \
                     patch.object(target, 'installation_token', return_value='APP_SENTINEL'), \
                     patch.object(target, 'clone_source', side_effect=clone), \
                     patch.object(target, 'github_request', side_effect=github), \
                     patch.object(target, 'build_bundle') as bundle, \
                     patch.object(target, 'report_usage'), \
                     self.assertRaises(target.UnsupportedBuildBackend):
                    target.execute(env)
                self.assertEqual([call.args[3]['action'] for call in remote.call_args_list], ['verify'])
                bundle.assert_not_called()

    def test_legacy_cos_and_cnb_opt_in_remain_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            target.require_supported_build_backend('cos', source)
            marker = source / target.RUNTIME_FILES[0]
            marker.parent.mkdir(parents=True)
            marker.write_text('{}')
            target.require_supported_build_backend('cnb', source)
            with self.assertRaises(target.UnsupportedBuildBackend):
                target.require_supported_build_backend('cos', source)
            with patch.object(target, 'prepare_build_disk') as disk, \
                 self.assertRaises(target.UnsupportedBuildBackend):
                target.build_bundle(source, 'b'*40, '0.0.126', source, {}, None)
            disk.assert_not_called()
            marker.unlink()
            marker.symlink_to('missing.json')
            with self.assertRaises(target.UnsupportedBuildBackend):
                target.require_supported_build_backend('cos', source)
            with self.assertRaises(target.UnsupportedBuildBackend):
                target.require_supported_build_backend('unknown', source)

    def test_resume_reuses_prior_ticket_even_if_cos_cannot_build_runtime(self):
        sha, previous, ticket = 'b'*40, 'd'*32, 'e'*32
        def clone(_sha, _token, source, _child, _log):
            (source / 'package.json').write_text('{"version":"0.0.126"}')
            marker = source / target.RUNTIME_FILES[0]
            marker.parent.mkdir(parents=True)
            marker.write_text('{}')
        def remote(_ssh, _child, _log, request, *args):
            action = request['action']
            if action == 'verify':
                return dict(status='verified', configValid=True, healthy=True,
                            version='0.0.125', currentSourceSha='c'*40)
            if action == 'record-attempt': return {'status': 'attempt_recorded'}
            if action == 'resume-transfer':
                self.assertEqual(request['previousTransferTicket'], previous)
                return dict(status='transfer_resumed', transferTicket=ticket,
                            bundleBytes=7, bundleSha256='f'*64)
            if action == 'deploy':
                self.assertEqual(request['transferTicket'], ticket)
                return dict(status='deployed', sourceSha=sha, version='0.0.126')
            raise AssertionError(action)
        def github(_token, method, path, *args):
            if method == 'POST' and path.endswith('/deployments'): return {'id': 7}
            if method in ('POST', 'DELETE'): return None
            raise AssertionError((method, path))
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(RUNNER_TEMP=directory, TENCENT_OPERATION='resume',
                TENCENT_BUILD_BACKEND='cos', TENCENT_PRODUCTION_ENABLED='true',
                TENCENT_CONFIRM_RELEASE='yes', TENCENT_SOURCE_SHA=sha,
                TENCENT_PREVIOUS_TRANSFER_TICKET=previous, PUBLIC_GH_TOKEN='PUBLIC_SENTINEL')
            with patch.object(target, 'ssh_configuration', return_value=[]), \
                 patch.object(target, 'remote', side_effect=remote), \
                 patch.object(target, 'installation_token', return_value='APP_SENTINEL'), \
                 patch.object(target, 'clone_source', side_effect=clone), \
                 patch.object(target, 'github_request', side_effect=github), \
                 patch.object(target, 'build_bundle') as bundle, \
                 patch.object(target, 'report_usage'):
                target.execute(env)
            bundle.assert_not_called()

    def test_same_version_override_is_narrow_and_never_used_for_upgrades(self):
        current = {"version": "0.0.125", "currentSourceSha": "a" * 40}
        override = target.release_request("deploy", "b" * 40, "0.0.125", current)
        self.assertIs(override["allowSameVersionSourceChange"], True)
        self.assertEqual(override["expectedCurrentSourceSha"], "a" * 40)
        self.assertEqual(target.release_request("resume", "b" * 40, "0.0.125", current), override)
        for operation, version, sha in (("automatic", "0.0.126", "b" * 40),
                                        ("deploy", "0.0.126", "b" * 40),
                                        ("deploy", "0.0.125", "a" * 40)):
            request = target.release_request(operation, sha, version, current)
            self.assertEqual(set(request), {"sourceSha", "version"})
        with self.assertRaises(target.Failure):
            target.release_request("deploy", "b" * 40, "0.0.124", current)

    def test_build_disk_must_remain_sufficient_after_bounded_sdk_cleanup(self):
        with patch.object(target.shutil, "disk_usage", return_value=SimpleNamespace(free=31 * 1024**3)), \
                patch.object(target, "run") as command:
            with self.assertRaises(target.Failure):
                target.prepare_build_disk(Path("/private"), {}, None)
            self.assertEqual(command.call_args.args[0],
                ["sudo", "rm", "-rf", "/usr/local/lib/android", "/usr/share/dotnet"])
        with patch.object(target.shutil, "disk_usage", return_value=SimpleNamespace(free=33 * 1024**3)), \
                patch.object(target, "run") as command:
            target.prepare_build_disk(Path("/private"), {}, None)
            command.assert_not_called()

    def test_docker_manifest_ids_are_not_confused_with_saved_config_ids(self):
        sha = "a" * 40
        raw = json.dumps({"os": "linux", "architecture": "amd64", "config": {
            "Labels": {"org.opencontainers.image.revision": sha}}}).encode()
        images = {kind: {"tag": "teaching-ai-production/" + kind + ":" + sha,
                         "id": "sha256:" + "b" * 64} for kind in ("api", "worker")}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.tar"
            with tarfile.open(path, "w") as archive:
                manifest = json.dumps([{"Config": kind + ".json", "RepoTags": [images[kind]["tag"]]}
                                       for kind in images]).encode()
                for name, body in [("manifest.json", manifest), ("api.json", raw), ("worker.json", raw)]:
                    member = tarfile.TarInfo(name); member.size = len(body)
                    archive.addfile(member, io.BytesIO(body))
            result = target.saved_config_ids(path, images, sha)
            self.assertEqual(result["api"]["id"], "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertNotEqual(result["api"]["id"], images["api"]["id"])
            with self.assertRaises(target.Failure):
                target.saved_config_ids(path, images, "c" * 40)

    def transfer_ticket(self, sha, size=7):
        ticket = "b" * 32
        return {"status": "transfer_prepared", "transferTicket": ticket,
                "uploadUrl": f"https://{target.TRANSFER_BUCKET}.cos.ap-beijing.myqcloud.com/teaching-production/releases/{sha}/{ticket}.tar?q-signature=PRIVATE_SENTINEL",
                "uploadHeaders": {"Content-Type": "application/octet-stream", "Content-Length": str(size)}}

    def test_private_transfer_streams_only_to_the_bound_cos_object(self):
        sha = "a" * 40
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return b""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle"; path.write_bytes(b"payload")
            def upload(request, timeout):
                self.assertEqual(request.method, "PUT")
                self.assertEqual(request.host, target.TRANSFER_BUCKET + ".cos.ap-beijing.myqcloud.com")
                self.assertEqual(request.get_header("Content-length"), "7")
                self.assertEqual(request.data.read(4) + request.data.read(4) + request.data.read(4), b"payload")
                return Response()
            with patch.object(target, "urlopen", side_effect=upload):
                self.assertEqual(target.upload_bundle(self.transfer_ticket(sha), path, sha, {"bytes": 7}), "b" * 32)
            for change in ({"uploadUrl": "https://evil.example/upload"},
                           {"uploadUrl": self.transfer_ticket(sha)["uploadUrl"].replace(".cos.ap-beijing.", ".cos.accelerate.")},
                           {"uploadHeaders": {"Authorization": "unexpected"}},
                           {"transferTicket": "../escape"}):
                ticket = dict(self.transfer_ticket(sha), **change)
                with self.subTest(change=change), patch.object(target, "urlopen") as network, self.assertRaises(target.Failure):
                    target.upload_bundle(ticket, path, sha, {"bytes": 7})
                network.assert_not_called()
            with patch.object(target, "urlopen", side_effect=SystemExit(143)), self.assertRaises(SystemExit):
                target.upload_bundle(self.transfer_ticket(sha), path, sha, {"bytes": 7})

    def test_private_build_cannot_inherit_credentials_or_action_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.environment(PATH="/bin", HOME=directory, RELEASE_APP_PRIVATE_KEY="app-secret",
                TENCENT_PRODUCTION_DEPLOY_SSH_KEY="ssh-secret", PUBLIC_GH_TOKEN="gh-secret",
                GOOGLE_APPLICATION_CREDENTIALS="gcp-secret", BASH_ENV="evil", NODE_OPTIONS="evil",
                GITHUB_OUTPUT="/public/actions/output")
            child = target.safe_environment(env, root)
            self.assertEqual(child["PATH"], "/bin")
            for name in ("RELEASE_APP_PRIVATE_KEY", "TENCENT_PRODUCTION_DEPLOY_SSH_KEY", "PUBLIC_GH_TOKEN",
                         "GOOGLE_APPLICATION_CREDENTIALS", "BASH_ENV", "NODE_OPTIONS"):
                self.assertNotIn(name, child)
            for name in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"):
                self.assertTrue(Path(child[name]).is_relative_to(root))
                self.assertEqual(Path(child[name]).stat().st_mode & 0o777, 0o600)

    def test_strict_host_pinning_and_no_ssh_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"TENCENT_PRODUCTION_DEPLOY_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate",
                   "TENCENT_PRODUCTION_SSH_KNOWN_HOSTS": target.HOST + " ssh-ed25519 AAAATEST"}
            args = target.ssh_configuration(env, root)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("IdentitiesOnly=yes", args)
            self.assertIn("ServerAliveInterval=30", args)
            self.assertIn("ServerAliveCountMax=3", args)
            self.assertEqual(args[-1], target.USER + "@" + target.HOST)
            self.assertEqual((root / "deploy.key").stat().st_mode & 0o777, 0o600)
            self.assertTrue((root / "deploy.key").read_bytes().endswith(b"\n"))
            # -G only renders OpenSSH's effective configuration; it does not connect.
            result = subprocess.run([args[0], "-G", *args[1:]], capture_output=True, text=True, check=True)
            self.assertIn("serveraliveinterval 30\n", result.stdout)
            self.assertIn("serveralivecountmax 3\n", result.stdout)
        for host in ("*", "shukezhinao.321weiqi.com", "49.233.166.202,other"):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(target.Failure):
                target.ssh_configuration(dict(env, TENCENT_PRODUCTION_SSH_KNOWN_HOSTS=host + " ssh-ed25519 AA"), Path(directory))

    def test_nonzero_receiver_response_reports_only_fixed_safe_codes(self):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.returncode = 1

        samples = [
            ({"status": "failed", "code": "PRIVATE_COMMAND_FAILED", "phase": "stage-registry-pull", "detail": "SECRET_SENTINEL"}, True),
            ({"status": "failed", "code": "SECRET_SENTINEL", "phase": "stage-registry-pull"}, False),
            ({"status": "failed", "code": "PRIVATE_COMMAND_FAILED", "phase": "SECRET_SENTINEL"}, False),
        ]
        for body, recognized in samples:
            with self.subTest(recognized=recognized, body=body):
                output, log = io.StringIO(), io.BytesIO()
                payload = json.dumps(body).encode()
                with patch.object(target.subprocess, "Popen", return_value=Process()), \
                     patch.object(target, "read_with_open_input", return_value=payload), \
                     patch.object(target, "stop_process"), \
                     patch("sys.stdout", output), self.assertRaises(target.Failure) as caught:
                    target.remote([], {}, log, {"action": "stage"})
                self.assertEqual(caught.exception.code, 44)
                visible = output.getvalue() + log.getvalue().decode()
                self.assertNotIn("SECRET_SENTINEL", visible)
                self.assertEqual("PRIVATE_COMMAND_FAILED" in visible, recognized)

    def test_source_history_check_precedes_checkout_and_token_stays_in_environment(self):
        calls = []
        with patch.object(target, "run", side_effect=lambda cmd, env, *_args, **_kw: calls.append((cmd, env))):
            target.clone_source("a" * 40, "private-token", Path("/private"), {"PATH": "/bin"}, None)
        commands = [call[0] for call in calls]
        ancestor = commands.index(["git", "merge-base", "--is-ancestor", "a" * 40, "refs/remotes/origin/main"])
        self.assertLess(ancestor, len(commands) - 1)
        self.assertFalse(any("private-token" in str(cmd) for cmd in commands))
        self.assertEqual(calls[0][1]["GIT_CONFIG_VALUE_0"], "/dev/null")

    def test_no_gcp_command_or_oidc_permissions_in_workflow(self):
        workflow = (Path(__file__).parent.parent / ".github/workflows/tencent-production.yml").read_text()
        self.assertNotIn("id-token:", workflow)
        self.assertNotIn("google-github-actions/", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("actions/cache", workflow)
        self.assertIn("group: tencent-production-release", workflow)


if __name__ == "__main__":
    unittest.main()
