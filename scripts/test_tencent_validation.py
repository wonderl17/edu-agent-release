import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import tencent_validation as target


class ValidationBoundaryTests(unittest.TestCase):
    def environment(self, **changes):
        result = {"PUBLIC_REPOSITORY": target.PUBLIC, "GITHUB_REF": "refs/heads/main",
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
        env["TENCENT_VALIDATION_ENABLED"] = "true"
        self.assertEqual(target.configuration(env)[0], "deploy")

    def test_automatic_cannot_inject_a_source_ref(self):
        with self.assertRaises(target.Failure):
            target.configuration(self.environment(TENCENT_OPERATION="automatic",
                TENCENT_VALIDATION_ENABLED="true", TENCENT_SOURCE_SHA="b" * 40))

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

    def test_disabled_scheduler_does_not_contact_any_target(self):
        with patch.object(target, "remote", side_effect=AssertionError("unexpected SSH")):
            target.execute(self.environment(TENCENT_OPERATION="automatic"))

    def test_same_version_override_is_narrow_and_never_used_for_upgrades(self):
        current = {"version": "0.0.125", "currentSourceSha": "a" * 40}
        override = target.release_request("deploy", "b" * 40, "0.0.125", current)
        self.assertIs(override["allowSameVersionSourceChange"], True)
        self.assertEqual(override["expectedCurrentSourceSha"], "a" * 40)
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
        images = {kind: {"tag": "teaching-ai-validation/" + kind + ":" + sha,
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

    def test_private_build_cannot_inherit_credentials_or_action_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.environment(PATH="/bin", HOME=directory, RELEASE_APP_PRIVATE_KEY="app-secret",
                TENCENT_VALIDATION_DEPLOY_SSH_KEY="ssh-secret", PUBLIC_GH_TOKEN="gh-secret",
                GOOGLE_APPLICATION_CREDENTIALS="gcp-secret", BASH_ENV="evil", NODE_OPTIONS="evil",
                GITHUB_OUTPUT="/public/actions/output")
            child = target.safe_environment(env, root)
            self.assertEqual(child["PATH"], "/bin")
            for name in ("RELEASE_APP_PRIVATE_KEY", "TENCENT_VALIDATION_DEPLOY_SSH_KEY", "PUBLIC_GH_TOKEN",
                         "GOOGLE_APPLICATION_CREDENTIALS", "BASH_ENV", "NODE_OPTIONS"):
                self.assertNotIn(name, child)
            for name in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"):
                self.assertTrue(Path(child[name]).is_relative_to(root))
                self.assertEqual(Path(child[name]).stat().st_mode & 0o777, 0o600)

    def test_strict_host_pinning_and_no_ssh_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"TENCENT_VALIDATION_DEPLOY_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate",
                   "TENCENT_VALIDATION_SSH_KNOWN_HOSTS": target.HOST + " ssh-ed25519 AAAATEST"}
            args = target.ssh_configuration(env, root)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("IdentitiesOnly=yes", args)
            self.assertEqual(args[-1], target.USER + "@" + target.HOST)
            self.assertEqual((root / "deploy.key").stat().st_mode & 0o777, 0o600)
            self.assertTrue((root / "deploy.key").read_bytes().endswith(b"\n"))
        for host in ("*", "shukezhinao.321weiqi.com", "49.233.166.202,other"):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(target.Failure):
                target.ssh_configuration(dict(env, TENCENT_VALIDATION_SSH_KNOWN_HOSTS=host + " ssh-ed25519 AA"), Path(directory))

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
        workflow = (Path(__file__).parent.parent / ".github/workflows/tencent-validation.yml").read_text()
        self.assertNotIn("id-token:", workflow)
        self.assertNotIn("google-github-actions/", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("actions/cache", workflow)
        self.assertIn("group: tencent-validation-release", workflow)


if __name__ == "__main__":
    unittest.main()
