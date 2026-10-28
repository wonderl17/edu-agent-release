"""Opt-in same-source rebuilds retain the complete confirmed release path."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tencent_production as target


class CurrentReleaseRebuildTests(unittest.TestCase):
    SHA = "c" * 40
    VERSION = "0.0.131"

    def environment(self, **changes):
        env = dict(RELEASES_ENABLED="true", PUBLIC_REPOSITORY=target.PUBLIC,
                   GITHUB_REF="refs/heads/main", GITHUB_SHA="a" * 40,
                   TENCENT_OPERATION="deploy", TENCENT_SOURCE_SHA=self.SHA,
                   TENCENT_CONFIRM_RELEASE="yes", TENCENT_PRODUCTION_ENABLED="true",
                   TENCENT_BUILD_BACKEND="cnb", TENCENT_REBUILD_CURRENT="true",
                   TENCENT_CNB_BUILD_CONCURRENCY="2", TENCENT_CNB_TOKEN="PRIVATE_SENTINEL",
                   PUBLIC_GH_TOKEN="PRIVATE_SENTINEL", GITHUB_RUN_ID="123456", GITHUB_RUN_ATTEMPT="1")
        return dict(env, **changes)

    def test_rebuild_requires_explicit_confirmed_manual_deploy_and_full_source(self):
        self.assertEqual(target.configuration(self.environment()), ("deploy", self.SHA))
        changes = [{"TENCENT_OPERATION": op} for op in ("verify", "stage", "resume", "automatic")]
        changes += [{"TENCENT_SOURCE_SHA": value} for value in ("", "main", "c" * 39)]
        changes += [{"TENCENT_REBUILD_CURRENT": value} for value in ("yes", "1", "TRUE", "")]
        changes += [{name: "false"} for name in ("RELEASES_ENABLED", "TENCENT_PRODUCTION_ENABLED")]
        changes += [{"TENCENT_CONFIRM_RELEASE": "no"}]
        for update in changes:
            with self.subTest(update=update), self.assertRaises(target.Failure):
                target.configuration(self.environment(**update))

    def execute_fixture(self, *, enabled=True, target_changes=None, failure_at=None, final_changes=None):
        actions, output = [], io.StringIO()
        current = dict(status="verified", configValid=True, healthy=True, version=self.VERSION,
                       currentSourceSha=self.SHA, lastAttemptedVersion=self.VERSION)
        current.update(target_changes or {})
        bound = {}

        def clone(sha, token, source, child, log):
            self.assertEqual(sha, self.SHA)
            actions.append("trusted-source")
            (source / "package.json").write_text(json.dumps({"version": self.VERSION}))

        def github(token, method, path, body=None):
            if method == "DELETE":
                actions.append("revoke-token")
            elif path.endswith("/statuses"):
                actions.append("status-" + body["state"])
            else:
                actions.append("deployment-record")
                self.assertEqual(body["payload"]["source_sha"], self.SHA)
                return {"id": 123}

        def remote(ssh, child, log, request, payload=None):
            action = request["action"]
            actions.append(action)
            self.assertNotIn("PRIVATE_SENTINEL", str(child))
            self.assertNotIn("allowSameVersionSourceChange", request)
            if action == "verify":
                return current
            if action == "record-attempt":
                return {"status": "attempt_recorded"}
            if action == "prepare-registry":
                bound.update({key: request[key] for key in ("bundleBytes", "bundleSha256")})
                result = dict(status="transfer_prepared", transport="registry", transferTicket="d" * 32, **bound)
                if failure_at == "package":
                    result["bundleSha256"] = "e" * 64
                return result
            if action == "deploy":
                self.assertEqual(request["sourceSha"], self.SHA)
                self.assertEqual(request["version"], self.VERSION)
                self.assertEqual(request["transferTicket"], "d" * 32)
                for key, value in bound.items():
                    self.assertEqual(request[key], value)
                return dict(dict(status="deployed", sourceSha=self.SHA, version=self.VERSION), **(final_changes or {}))
            if action == "diagnostics":
                return {"status": "diagnostics_saved"}
            raise AssertionError(action)

        def build(source, sha, version, private, child, log, token, run, status, **options):
            actions.append("cnb-build")
            self.assertEqual((sha, version, options["concurrency"]), (self.SHA, self.VERSION, "2"))
            self.assertNotIn("PRIVATE_SENTINEL", str(child))
            if failure_at == "cancel":
                raise SystemExit(143)
            options["usage"].update(basis="reported", coreHours=0.1, billingSeconds=45, pipelines=1)
            manifest = private / "registry.json"
            manifest.write_text('{"fixture":true}')
            return manifest

        failure = None
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(RUNNER_TEMP=directory)
            if not enabled:
                del env["TENCENT_REBUILD_CURRENT"]
            with contextlib.redirect_stdout(output), \
                    patch.object(target, "ssh_configuration", return_value=[]), \
                    patch.object(target, "remote", side_effect=remote), \
                    patch.object(target, "installation_token", return_value="PRIVATE_SENTINEL"), \
                    patch.object(target, "clone_source", side_effect=clone), \
                    patch.object(target, "github_request", side_effect=github), \
                    patch.object(target, "build_cnb", side_effect=build), \
                    patch.object(target, "build_bundle", side_effect=AssertionError("Unexpected build fallback")), \
                    patch.object(target.subprocess, "Popen", side_effect=AssertionError("Unexpected subprocess")):
                try:
                    target.execute(env)
                except (target.Failure, SystemExit) as error:
                    failure = error
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertNotIn("PRIVATE_SENTINEL", output.getvalue())
        return actions, failure

    def test_default_still_skips_already_running_source_without_build_or_attempt(self):
        actions, failure = self.execute_fixture(enabled=False)
        self.assertIsNone(failure)
        self.assertEqual(actions, ["verify", "trusted-source", "revoke-token"])

    def test_explicit_rebuild_keeps_source_attempt_build_package_deploy_and_result_checks(self):
        actions, failure = self.execute_fixture()
        self.assertIsNone(failure)
        self.assertEqual(actions, ["verify", "trusted-source", "record-attempt", "deployment-record",
                                  "cnb-build", "prepare-registry", "deploy", "status-success", "revoke-token"])

    def test_target_identity_must_match_before_any_attempt_or_build(self):
        for update in ({"version": "0.0.130"}, {"version": "0.0.132"},
                       {"currentSourceSha": "b" * 40}, {"currentSourceSha": None}):
            with self.subTest(update=update):
                actions, failure = self.execute_fixture(target_changes=update)
                self.assertIsInstance(failure, target.Failure)
                self.assertEqual(failure.code, 41)
                self.assertNotIn("record-attempt", actions)
                self.assertNotIn("cnb-build", actions)

    def test_rebuild_still_requires_healthy_target_before_source_access(self):
        for update in ({"healthy": False}, {"healthy": None}, {"configValid": False}):
            with self.subTest(update=update):
                actions, failure = self.execute_fixture(target_changes=update)
                self.assertIsInstance(failure, target.Failure)
                self.assertEqual(failure.code, 44)
                self.assertNotIn("trusted-source", actions)
                self.assertNotIn("cnb-build", actions)

    def test_bad_package_or_completion_receipt_cannot_succeed(self):
        actions, failure = self.execute_fixture(failure_at="package")
        self.assertIsInstance(failure, target.Failure)
        self.assertNotIn("deploy", actions)
        for change in ({"status": "staged"}, {"version": "0.0.130"}, {"sourceSha": "b" * 40}):
            with self.subTest(change=change):
                actions, failure = self.execute_fixture(final_changes=change)
                self.assertIsInstance(failure, target.Failure)
                self.assertNotIn("status-success", actions)

    def test_rebuild_cancellation_propagates_without_deploy_or_diagnostics(self):
        actions, failure = self.execute_fixture(failure_at="cancel")
        self.assertIsInstance(failure, SystemExit)
        self.assertEqual(failure.code, 143)
        self.assertNotIn("deploy", actions)
        self.assertNotIn("diagnostics", actions)
        self.assertEqual(actions[-2:], ["status-error", "revoke-token"])


if __name__ == "__main__":
    unittest.main()
