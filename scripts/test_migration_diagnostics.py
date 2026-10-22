import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import migration_diagnostics as diagnostics


class MigrationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "receipt.json"
        self.value = {"schemaVersion": 1, "phase": "identity", "state": "failure", "records": [{"phase": "identity", "state": "start"}],
            "identity": {"accountState": "unset", "projectMatches": True, "credentialFileType": "external_account", "impersonationTargetMatches": True},
            "command": {"tool": "gcloud", "exitCode": 0}}
        self.environment = {"GITHUB_ACTIONS": "true", "RELEASE_OPERATION": "migration-bootstrap", "GCP_PRODUCTION_PROJECT_ID": diagnostics.PROJECT,
            "GITHUB_RUN_ID": "39723631401", "GITHUB_RUN_ATTEMPT": "2"}

    def write(self):
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)

    def test_only_whitelisted_metadata_can_reach_fixed_private_bucket(self):
        sentinel = "SECRET_PAYLOAD_MUST_NOT_BE_PERSISTED"
        self.value["stderr"] = sentinel
        self.value["identity"]["token"] = sentinel
        self.value["records"][0]["command"] = sentinel
        self.write()
        child = Mock(pid=123, wait=Mock(return_value=0))
        with patch.object(diagnostics.subprocess, "Popen", return_value=child) as process:
            self.assertTrue(diagnostics.upload_failure_receipt(self.path, self.root, self.environment))
        command = process.call_args.args[0]
        self.assertIn(f"--project={diagnostics.PROJECT}", command)
        self.assertIn(f"gs://{diagnostics.PROJECT}_cloudbuild/migration-diagnostics/39723631401/2.log", command)
        output = self.root / "migration-diagnostics-upload.json"
        self.assertNotIn(sentinel, output.read_text())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(output.read_text()), diagnostics.normalized_receipt(self.path))

    def test_other_modes_projects_and_invalid_paths_never_spawn_upload(self):
        self.write()
        for changes in ({"RELEASE_OPERATION": "cloud"}, {"GCP_PRODUCTION_PROJECT_ID": "source"},
                        {"GITHUB_RUN_ID": "../../other"}, {"GITHUB_RUN_ATTEMPT": "0"}):
            with patch.object(diagnostics.subprocess, "Popen") as process:
                self.assertFalse(diagnostics.upload_failure_receipt(self.path, self.root, {**self.environment, **changes}))
                process.assert_not_called()

    def test_invalid_values_permissions_and_symlinks_are_rejected(self):
        for key, value in (("phase", "PRIVATE_SECRET"), ("records", [{"phase": "identity", "state": "PRIVATE_SECRET"}])):
            previous = self.value[key]; self.value[key] = value; self.write()
            with self.assertRaises(ValueError): diagnostics.normalized_receipt(self.path)
            self.value[key] = previous
        self.write(); self.path.chmod(0o644)
        with self.assertRaises(ValueError): diagnostics.normalized_receipt(self.path)
        self.path.chmod(0o600)
        link = self.root / "link"; link.symlink_to(self.path)
        with self.assertRaises(OSError): diagnostics.normalized_receipt(link)

    def test_upload_failure_preserves_private_execution_failure_result(self):
        self.write()
        with patch.object(diagnostics.subprocess, "Popen", return_value=Mock(wait=Mock(return_value=1))):
            self.assertFalse(diagnostics.upload_failure_receipt(self.path, self.root, self.environment))

    def build_fixture(self, data=b"PERMISSION_DENIED: missing build permission"):
        self.path = self.root / "migration-diagnostics.json"
        self.value.update(phase="build-api", state="failure", command={"tool": "gcloud", "exitCode": 1})
        self.write()
        self.error = self.root / diagnostics.BUILD_ERROR_FILE
        self.error.write_bytes(data)
        self.error.chmod(0o600)

    def test_build_error_uses_only_fixed_private_destination_and_silent_upload(self):
        self.build_fixture()
        child = Mock(pid=123, wait=Mock(return_value=0))
        with patch.object(diagnostics.subprocess, "Popen", return_value=child) as process:
            self.assertTrue(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path))
        command = process.call_args.args[0]
        self.assertIn(f"gs://{diagnostics.PROJECT}_cloudbuild/migration-diagnostics/39723631401/2.build-submit.stderr.log", command)
        for flag in (f"--project={diagnostics.PROJECT}", f"--billing-project={diagnostics.PROJECT}", "--no-clobber", "--quiet"):
            self.assertIn(flag, command)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(process.call_args.kwargs[stream], diagnostics.subprocess.DEVNULL)
        child.wait.assert_called_once_with(timeout=20)
        output = self.root / "migration-build-error-upload.log"
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.read_bytes(), self.error.read_bytes())

    def test_build_upload_rejects_wrong_environment_and_receipt_gate(self):
        self.build_fixture()
        with patch.object(diagnostics.subprocess, "Popen") as process:
            for changes in ({"GITHUB_ACTIONS": "false"}, {"RELEASE_OPERATION": "cloud"},
                            {"GCP_PRODUCTION_PROJECT_ID": "source"}, {"GITHUB_RUN_ID": "../other"},
                            {"GITHUB_RUN_ATTEMPT": "0"}):
                self.assertFalse(diagnostics.upload_failure_build_error(self.error, self.root, {**self.environment, **changes}, self.path))
            for changes in ({"phase": "source-upload"}, {"state": "success"}, {"command": {"tool": "git", "exitCode": 1}},
                            {"command": {"tool": "gcloud", "exitCode": 0}}):
                original = dict(self.value); self.value.update(changes); self.write()
                self.assertFalse(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path))
                self.value = original
            process.assert_not_called()

    def test_build_upload_rejects_arbitrary_paths_permissions_links_and_sizes(self):
        self.build_fixture()
        with patch.object(diagnostics.subprocess, "Popen") as process:
            for filename in ("private.log", "arbitrary.log"):
                file = self.root / filename; file.write_text("must never upload"); file.chmod(0o600)
                self.assertFalse(diagnostics.upload_failure_build_error(file, self.root, self.environment, self.path))
            self.assertFalse(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.root / "other.json"))
            for mode in (0o644, 0o400):
                self.error.chmod(mode)
                self.assertFalse(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path))
            self.error.chmod(0o600)
            for data in (b"", b"x" * 8193):
                self.error.write_bytes(data)
                self.assertFalse(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path))
            self.error.unlink(); self.error.symlink_to(self.root / "private.log")
            with self.assertRaises(OSError): diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path)
            self.error.unlink(); self.error.mkdir()
            self.assertFalse(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path))
            self.error.rmdir(); os.link(self.root / "private.log", self.error)
            self.assertFalse(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path))
            process.assert_not_called()

    def test_build_error_redacts_tokens_assignments_pem_env_values_and_controls(self):
        secrets = ["eyJheader.payload.signature", "ya29.synthetic-token", "ghp_synthetic_token", "github_pat_synthetic_token",
                   "AIza" + "x" * 35, "AKIA" + "A" * 16, "ASIA" + "B" * 16,
                   "synthetic-bearer", "c3ludGhldGljLWJhc2lj", "assignment-value", "json-value", "pem-secret",
                   "ENV_SENTINEL_do_not_persist", "credential-value", "userinfo-password"]
        data = ("\x1b[31mPermission denied\x1b[0m\x00\x01\u202e\n" + "\n".join(secrets[:7]) +
                "\nBearer synthetic-bearer\nBasic c3ludGhldGljLWJhc2lj\n" +
                "\tBearer\tsynthetic-tab-token\nAuthorization:\tBearer\tsynthetic-auth-tab-token\n" +
                'api_key=assignment-value\n{"password":"json-value"}\ncredential=credential-value\n' +
                "-----BEGIN PRIVATE KEY-----\npem-secret\n-----END PRIVATE KEY-----\n" +
                "unlabelled ENV_SENTINEL_do_not_persist\npostgres://user:userinfo-password@example.invalid/db\n")
        self.build_fixture(data.encode())
        environment = {**self.environment, "SOME_SECRET": secrets[12]}
        with patch.object(diagnostics.subprocess, "Popen", return_value=Mock(wait=Mock(return_value=0))):
            self.assertTrue(diagnostics.upload_failure_build_error(self.error, self.root, environment, self.path))
        value = (self.root / "migration-build-error-upload.log").read_text()
        self.assertIn("Permission denied", value)
        self.assertIn("[REDACTED]", value)
        for secret in secrets: self.assertNotIn(secret, value)
        self.assertNotIn("synthetic-tab-token", value)
        self.assertNotIn("synthetic-auth-tab-token", value)
        for control in ("\x1b", "\x00", "\x01", "\u202e"): self.assertNotIn(control, value)

    def test_build_error_output_is_bounded_valid_utf8_after_redaction(self):
        data = ("密" * 2730).encode() + b"xy"
        self.build_fixture(data)
        with patch.object(diagnostics.subprocess, "Popen", return_value=Mock(wait=Mock(return_value=0))):
            self.assertTrue(diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path))
        output = (self.root / "migration-build-error-upload.log").read_bytes()
        self.assertLessEqual(len(output), 8192); output.decode("utf-8", errors="strict")
        output = diagnostics.sanitized_build_error(b"x" * 8192, {"SYNTHETIC_SECRET": "x"})
        self.assertLessEqual(len(output), 8192)
        self.assertNotIn(b"x", output)
        # The private JS channel may end at an incomplete UTF-8 code point.
        self.assertEqual(diagnostics.sanitized_build_error(b"failure\xe5", {}), b"failure")

    def test_build_upload_timeout_stops_process_group_and_propagates_cancellation(self):
        self.build_fixture()
        child = Mock(pid=123, wait=Mock(side_effect=[diagnostics.subprocess.TimeoutExpired("gcloud", 20), 0]))
        with patch.object(diagnostics.subprocess, "Popen", return_value=child), patch.object(diagnostics.os, "killpg") as kill:
            with self.assertRaises(diagnostics.subprocess.TimeoutExpired):
                diagnostics.upload_failure_build_error(self.error, self.root, self.environment, self.path)
        kill.assert_called_once_with(123, diagnostics.signal.SIGTERM)


if __name__ == "__main__": unittest.main()
