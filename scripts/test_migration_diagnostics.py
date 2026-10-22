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


if __name__ == "__main__": unittest.main()
