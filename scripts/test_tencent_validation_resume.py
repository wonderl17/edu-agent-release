"""Execution-level original-package resume tests; all remote operations are mocked."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tencent_validation as target


class OriginalPackageResumeTests(unittest.TestCase):
    SOURCE_SHA = "c" * 40
    CURRENT_SHA = "b" * 40
    OLD_TICKET = "d" * 32
    NEW_TICKET = "e" * 32
    BUNDLE_HASH = "f" * 64
    PRIVATE_SENTINEL = "SYNTHETIC_PRIVATE_SENTINEL_MUST_STAY_PRIVATE"

    def execute_fixture(self, *, healthy=True, resume_result=None, cancelled=False):
        events, remote_calls, git_calls, source_paths = [], [], [], []
        stdout = io.StringIO()
        token = self.PRIVATE_SENTINEL + "_APP"
        env = {
            "PUBLIC_REPOSITORY": target.PUBLIC,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "a" * 40,
            "TENCENT_OPERATION": "resume",
            "TENCENT_SOURCE_SHA": self.SOURCE_SHA,
            "TENCENT_PREVIOUS_TRANSFER_TICKET": self.OLD_TICKET,
            "TENCENT_CONFIRM_RELEASE": "yes",
            "TENCENT_VALIDATION_ENABLED": "true",
            "TENCENT_VALIDATION_BASELINE_VERSION": "0.0.125",
            "TENCENT_VALIDATION_DEPLOY_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\n" + self.PRIVATE_SENTINEL,
            "TENCENT_VALIDATION_SSH_KNOWN_HOSTS": target.HOST + " ssh-ed25519 SYNTHETIC",
            "PUBLIC_GH_TOKEN": self.PRIVATE_SENTINEL + "_PUBLIC",
            "GITHUB_RUN_ID": "123456",
        }

        def auth(repository, permission):
            self.assertEqual((repository, permission), (target.SOURCE, "read"))
            events.append("app-read-auth")
            return token

        def git(command, child, log, *, cwd=None, **_kwargs):
            self.assertEqual(command[0], "git")
            self.assertNotIn(token, str(command))
            self.assertNotIn("PUBLIC_GH_TOKEN", child)
            self.assertNotIn("TENCENT_VALIDATION_DEPLOY_SSH_KEY", child)
            git_calls.append(command)
            log.write(self.PRIVATE_SENTINEL.encode())
            if command[1] == "merge-base":
                events.append("main-ancestor-checked")
                self.assertEqual(command, ["git", "merge-base", "--is-ancestor", self.SOURCE_SHA, "refs/remotes/origin/main"])
            if command[1] == "checkout":
                events.append("source-checked-out")
                source_paths.append(cwd)
                (cwd / "package.json").write_text(json.dumps({"version": "0.0.125"}))
            return b""

        def github(credential, method, path, body=None):
            if method == "DELETE" and path == "/installation/token":
                self.assertEqual(credential, token)
                events.append("app-token-revoked")
                return None
            self.assertEqual(credential, env["PUBLIC_GH_TOKEN"])
            self.assertTrue(path.startswith("/repos/" + target.PUBLIC + "/deployments"))
            if path.endswith("/statuses"):
                events.append("deployment-status-" + body["state"])
                return {}
            self.assertEqual(method, "POST")
            self.assertEqual(body["payload"]["source_sha"], self.SOURCE_SHA)
            events.append("deployment-created")
            return {"id": 123}

        def remote(_ssh, child, _log, request, payload=None):
            self.assertNotIn("PUBLIC_GH_TOKEN", child)
            self.assertNotIn("TENCENT_VALIDATION_DEPLOY_SSH_KEY", child)
            remote_calls.append(dict(request))
            action = request["action"]
            events.append(action)
            if action == "verify":
                return {"status": "verified", "configValid": True, "healthy": healthy,
                        "version": "0.0.125", "currentSourceSha": self.CURRENT_SHA,
                        "lastAttemptedVersion": "0.0.125"}
            if action == "record-attempt":
                return {"status": "attempt_recorded"}
            if action == "resume-transfer":
                self.assertEqual(request["previousTransferTicket"], self.OLD_TICKET)
                self.assertEqual(request["sourceSha"], self.SOURCE_SHA)
                self.assertEqual(request["version"], "0.0.125")
                if cancelled:
                    raise SystemExit(143)
                return resume_result if resume_result is not None else {
                    "status": "transfer_resumed", "transferTicket": self.NEW_TICKET,
                    "bundleBytes": 7, "bundleSha256": self.BUNDLE_HASH}
            if action == "deploy":
                self.assertIsNone(payload)
                self.assertEqual(request["sourceSha"], self.SOURCE_SHA)
                self.assertEqual(request["version"], "0.0.125")
                self.assertEqual(request["transferTicket"], self.NEW_TICKET)
                self.assertEqual(request["bundleBytes"], 7)
                self.assertEqual(request["bundleSha256"], self.BUNDLE_HASH)
                self.assertIs(request["allowSameVersionSourceChange"], True)
                self.assertEqual(request["expectedCurrentSourceSha"], self.CURRENT_SHA)
                return {"status": "deployed", "sourceSha": self.SOURCE_SHA, "version": "0.0.125"}
            if action == "diagnostics":
                self.assertIsNotNone(payload)
                return {"status": "diagnostics_saved"}
            raise AssertionError("Unexpected remote action")

        failure = None
        with tempfile.TemporaryDirectory(prefix="resume-controller-offline-") as directory:
            env["RUNNER_TEMP"] = directory
            with contextlib.redirect_stdout(stdout), \
                    patch.object(target, "installation_token", side_effect=auth) as authentication, \
                    patch.object(target, "run", side_effect=git), \
                    patch.object(target, "github_request", side_effect=github), \
                    patch.object(target, "remote", side_effect=remote), \
                    patch.object(target, "build_bundle", side_effect=AssertionError("Resume rebuilt source")) as build, \
                    patch.object(target, "upload_bundle", side_effect=AssertionError("Resume uploaded source")) as upload, \
                    patch.object(target.subprocess, "Popen", side_effect=AssertionError("Unexpected real subprocess")), \
                    patch.object(target, "urlopen", side_effect=AssertionError("Unexpected real HTTP")):
                try:
                    target.execute(env)
                except (target.Failure, SystemExit) as error:
                    failure = error
                build.assert_not_called()
                upload.assert_not_called()
                if not healthy:
                    authentication.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertTrue(all(not path.exists() for path in source_paths))
        self.assertNotIn(self.PRIVATE_SENTINEL, stdout.getvalue())
        return events, remote_calls, git_calls, failure

    def test_resume_validates_main_then_reuses_new_bound_ticket_without_build_or_upload(self):
        events, calls, git, failure = self.execute_fixture()
        self.assertIsNone(failure)
        self.assertEqual(events, ["verify", "app-read-auth", "main-ancestor-checked", "source-checked-out",
                                  "record-attempt", "deployment-created", "resume-transfer", "deploy",
                                  "deployment-status-success", "app-token-revoked"])
        self.assertEqual([call["action"] for call in calls], ["verify", "record-attempt", "resume-transfer", "deploy"])
        self.assertLess(git.index(["git", "merge-base", "--is-ancestor", self.SOURCE_SHA, "refs/remotes/origin/main"]),
                        git.index(["git", "checkout", "--quiet", "--detach", self.SOURCE_SHA]))
        self.assertIn(["git", "fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin", "main"], git)

    def test_unhealthy_target_does_not_access_private_source_or_reuse_any_package(self):
        events, calls, git, failure = self.execute_fixture(healthy=False)
        self.assertIsInstance(failure, target.Failure)
        self.assertEqual(failure.code, 44)
        self.assertEqual(events, ["verify", "diagnostics"])
        self.assertEqual(git, [])
        self.assertFalse(any(call["action"] in ("resume-transfer", "deploy") for call in calls))

    def test_invalid_resume_receipt_never_deploys_and_cleans_private_files(self):
        events, calls, _git, failure = self.execute_fixture(resume_result={
            "status": "transfer_resumed", "transferTicket": "../invalid", "bundleBytes": 7,
            "bundleSha256": self.BUNDLE_HASH})
        self.assertIsInstance(failure, target.Failure)
        self.assertEqual(failure.code, 47)
        self.assertNotIn("deploy", [call["action"] for call in calls])
        self.assertIn("deployment-status-error", events)
        self.assertEqual(events[-1], "app-token-revoked")

    def test_cancelled_resume_never_deploys_or_starts_diagnostic_transfer(self):
        events, calls, _git, failure = self.execute_fixture(cancelled=True)
        self.assertIsInstance(failure, SystemExit)
        self.assertEqual(failure.code, 143)
        self.assertNotIn("deploy", [call["action"] for call in calls])
        self.assertNotIn("diagnostics", [call["action"] for call in calls])
        self.assertIn("deployment-status-error", events)
        self.assertEqual(events[-1], "app-token-revoked")


if __name__ == "__main__":
    unittest.main()
