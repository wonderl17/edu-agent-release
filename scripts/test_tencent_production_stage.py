"""Offline Production staging: bootstrap is not a live release or a cloud attempt."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tencent_production as target


class ProductionStageTests(unittest.TestCase):
    SOURCE = "c" * 40
    TICKET = "d" * 32
    SENTINEL = "PRIVATE_STAGE_SENTINEL_DO_NOT_PUBLISH"

    def execute_fixture(self, *, operation="stage", target_result=None, stage_result=None, cancelled=False):
        events, calls, source_paths, records = [], [], [], []
        output = io.StringIO()
        env = {
            "RELEASES_ENABLED": "true", "PUBLIC_REPOSITORY": target.PUBLIC, "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
            "TENCENT_OPERATION": operation, "TENCENT_SOURCE_SHA": self.SOURCE,
            "TENCENT_CONFIRM_RELEASE": "yes", "TENCENT_PRODUCTION_ENABLED": "true",
            "TENCENT_PRODUCTION_DEPLOY_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\n" + self.SENTINEL,
            "TENCENT_PRODUCTION_SSH_KNOWN_HOSTS": target.HOST + " ssh-ed25519 SYNTHETIC",
            "PUBLIC_GH_TOKEN": self.SENTINEL + "_PUBLIC", "GITHUB_RUN_ID": "123456",
        }
        if operation == "resume":
            env["TENCENT_PREVIOUS_TRANSFER_TICKET"] = "e" * 32
        bootstrap = {"status": "verified", "configValid": True, "bootstrapReady": True,
                     "healthy": False, "phase": "bootstrap", "currentSourceSha": None,
                     "version": "0.0.125", "lastAttemptedVersion": "0.0.125"}

        def remote(_ssh, child, log, request, payload=None):
            self.assertNotIn("PUBLIC_GH_TOKEN", child)
            self.assertNotIn("TENCENT_PRODUCTION_DEPLOY_SSH_KEY", child)
            calls.append(dict(request)); events.append(request["action"])
            if request["action"] == "verify":
                return target_result if target_result is not None else bootstrap
            if request["action"] == "prepare-transfer":
                self.assertEqual(set(request), {"action", "sourceSha", "version", "bundleBytes", "bundleSha256"})
                return {"status": "transfer_prepared", "transferTicket": self.TICKET}
            if request["action"] == "stage":
                self.assertEqual(set(request), {"action", "sourceSha", "version", "bundleBytes", "bundleSha256", "transferTicket"})
                self.assertIsNone(payload)
                if cancelled:
                    raise SystemExit(143)
                result = {key: request[key] for key in ("sourceSha", "version", "bundleBytes", "bundleSha256", "transferTicket")}
                result["status"] = "staged"
                return dict(result, **(stage_result or {}))
            if request["action"] == "diagnostics":
                return {"status": "diagnostics_saved"}
            raise AssertionError("Stage attempted a service or database operation")

        def git(command, child, log, *, cwd=None, **kwargs):
            self.assertEqual(command[0], "git")
            log.write(self.SENTINEL.encode())
            if command[1] == "merge-base":
                self.assertEqual(command, ["git", "merge-base", "--is-ancestor", self.SOURCE, "refs/remotes/origin/main"])
                events.append("ancestor")
            if command[1] == "checkout":
                source_paths.append(cwd)
                (cwd / "package.json").write_text('{"version":"0.0.125"}')
                events.append("checkout")
            return b""

        def build(source, sha, version, private, child, log):
            self.assertEqual((sha, version), (self.SOURCE, "0.0.125"))
            events.append("build")
            path = private / "release-bundle.tar"; path.write_bytes(b"synthetic")
            return path

        def github(credential, method, path, body=None):
            if method == "DELETE":
                self.assertEqual(path, "/installation/token")
                events.append("token-revoked"); return {}
            self.assertEqual(credential, env["PUBLIC_GH_TOKEN"])
            if path.endswith("/statuses"):
                events.append("status-" + body["state"]); return {}
            records.append(body); events.append("metadata")
            return {"id": 123}

        failure = None
        with tempfile.TemporaryDirectory() as directory:
            env["RUNNER_TEMP"] = directory
            with contextlib.redirect_stdout(output), patch.object(target, "remote", side_effect=remote), \
                    patch.object(target, "installation_token", return_value=self.SENTINEL), \
                    patch.object(target, "run", side_effect=git), patch.object(target, "build_bundle", side_effect=build), \
                    patch.object(target, "upload_bundle", side_effect=lambda *_: events.append("upload") or self.TICKET), \
                    patch.object(target, "github_request", side_effect=github), \
                    patch.object(target.subprocess, "Popen", side_effect=AssertionError("Unexpected subprocess")), \
                    patch.object(target, "urlopen", side_effect=AssertionError("Unexpected network")):
                try:
                    target.execute(env)
                except (target.Failure, SystemExit) as error:
                    failure = error
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(all(not path.exists() for path in source_paths))
        self.assertNotIn(self.SENTINEL, output.getvalue())
        return events, calls, records, failure

    def test_bootstrap_can_stage_without_recording_cloud_attempt_or_touching_services(self):
        events, calls, records, failure = self.execute_fixture()
        self.assertIsNone(failure)
        self.assertEqual(events, ["verify", "ancestor", "checkout", "metadata", "build", "prepare-transfer",
                                  "upload", "stage", "status-success", "token-revoked"])
        self.assertEqual([c["action"] for c in calls], ["verify", "prepare-transfer", "stage"])
        self.assertEqual(records[0]["task"], "stage-tencent-production")
        self.assertEqual(records[0]["payload"]["operation"], "tencent-production-stage")
        self.assertFalse(records[0]["production_environment"])

    def test_bootstrap_is_never_a_normal_deploy_resume_or_verify_health_proof(self):
        for operation in ("verify", "deploy", "resume"):
            with self.subTest(operation=operation):
                events, calls, records, failure = self.execute_fixture(operation=operation)
                self.assertIsInstance(failure, target.Failure)
                self.assertEqual(failure.code, 44)
                self.assertNotIn("ancestor", events)
                self.assertEqual(records, [])
                self.assertFalse(any(c["action"] in ("stage", "record-attempt", "deploy", "resume-transfer") for c in calls))

    def test_stage_bootstrap_requires_all_exact_readiness_fields(self):
        for updates in ({"bootstrapReady": False}, {"configValid": False}, {"healthy": None},
                        {"phase": "running"}, {"currentSourceSha": self.SOURCE}):
            state = {"status": "verified", "configValid": True, "bootstrapReady": True, "healthy": False,
                     "phase": "bootstrap", "currentSourceSha": None, "version": "0.0.125"}
            with self.subTest(updates=updates):
                events, calls, records, failure = self.execute_fixture(target_result=dict(state, **updates))
                self.assertIsInstance(failure, target.Failure)
                self.assertNotIn("build", events)
                self.assertEqual(records, [])

    def test_stage_still_enforces_baseline_and_exact_receipt_bindings(self):
        state = {"status": "verified", "configValid": True, "bootstrapReady": True, "healthy": False,
                 "phase": "bootstrap", "currentSourceSha": None, "version": "0.0.126"}
        events, _calls, _records, failure = self.execute_fixture(target_result=state)
        self.assertIsInstance(failure, target.Failure)
        self.assertEqual(failure.code, 41)
        self.assertNotIn("build", events)
        for updates in ({"status": "deployed"}, {"sourceSha": "f" * 40}, {"version": "9.0.0"},
                        {"transferTicket": "f" * 32}, {"bundleBytes": 10}, {"bundleSha256": "f" * 64}):
            with self.subTest(updates=updates):
                events, _calls, _records, failure = self.execute_fixture(stage_result=updates)
                self.assertIsInstance(failure, target.Failure)
                self.assertIn("status-error", events)

    def test_stage_cancellation_is_propagated_without_diagnostic_upload(self):
        events, calls, _records, failure = self.execute_fixture(cancelled=True)
        self.assertIsInstance(failure, SystemExit)
        self.assertEqual(failure.code, 143)
        self.assertNotIn("diagnostics", [c["action"] for c in calls])
        self.assertIn("status-error", events)
        self.assertEqual(events[-1], "token-revoked")

    def test_stage_requires_exact_source_and_explicit_authorization(self):
        env = {"RELEASES_ENABLED": "true", "PUBLIC_REPOSITORY": target.PUBLIC, "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "TENCENT_OPERATION": "stage", "TENCENT_SOURCE_SHA": self.SOURCE,
               "TENCENT_CONFIRM_RELEASE": "yes", "TENCENT_PRODUCTION_ENABLED": "true"}
        self.assertEqual(target.configuration(env), ("stage", self.SOURCE))
        for changes in ({"TENCENT_SOURCE_SHA": ""}, {"TENCENT_CONFIRM_RELEASE": "no"},
                        {"TENCENT_PRODUCTION_ENABLED": "false"}, {"RELEASES_ENABLED": "false"}, {"TENCENT_PREVIOUS_TRANSFER_TICKET": self.TICKET}):
            with self.subTest(changes=changes), self.assertRaises(target.Failure):
                target.configuration(dict(env, **changes))

    def test_fixed_production_identity_and_old_release_cannot_request_gcp(self):
        self.assertEqual((target.PROTOCOL, target.USER, target.TRANSFER_BUCKET, target.SITE),
                         ("teaching-production-ci-v1", "teaching-production-ci", "teaching-production-releases-1331158445",
                          "https://shukezhinao.321weiqi.com"))
        root = Path(__file__).parent.parent
        production = (root / ".github/workflows/tencent-production.yml").read_text()
        self.assertIn("environment: tencent-production-release", production)
        self.assertNotIn("TENCENT_VALIDATION", production)
        self.assertNotIn("id-token:", production)
        self.assertNotIn("google-github-actions/", production)
        release = (root / ".github/workflows/release.yml").read_text()
        self.assertIn("options: [desktop, resume]", release)
        self.assertNotIn("google-github-actions/", release)
        self.assertNotIn("GCP_PRODUCTION_", release)


if __name__ == "__main__":
    unittest.main()
