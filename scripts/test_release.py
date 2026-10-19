import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("release_controller", Path(__file__).with_name("release.py"))
CONTROLLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROLLER)
SENTINEL = "PRIVATE_AUTH_AND_SOURCE_SENTINEL_9e2f"
SOURCE_SHA = "a" * 40
SOURCE = "shukezhinao/edu-agent"


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "edu-agent-private-source"
        self.environment = {
            "RUNNER_TEMP": str(self.root), "SOURCE_REPOSITORY": SOURCE, "SOURCE_SHA": SOURCE_SHA,
            "RELEASE_OPERATION": "cloud", "RELEASE_TARGET": "all", "CONFIRM_RELEASE": "yes",
            "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "b" * 40,
            "PUBLIC_GH_TOKEN": "public_fixture_token", "PUBLIC_REPOSITORY": "wonderl17/edu-agent-release",
            "RELEASE_APP_ID": "5379199", "RELEASE_APP_PRIVATE_KEY": SENTINEL,
        }
        for name in ("DESKTOP_UPDATE_S3_ACCESS_KEY_ID", "DESKTOP_UPDATE_S3_SECRET_ACCESS_KEY", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "MACOS_CERTIFICATE_P12_BASE64", "MACOS_CERTIFICATE_PASSWORD"):
            self.environment[name] = SENTINEL
        self.calls = []
        self.children = []
        self.probes = []

    def clone(self, _repository, _sha, _token, source):
        (source / "apps/desktop").mkdir(parents=True)
        (source / "scripts").mkdir()
        (source / "package.json").write_text(json.dumps({"version": "0.0.122"}))
        (source / "apps/desktop/package.json").write_text(json.dumps({"version": "0.0.123"}))
        for entry in ("ci-release-cloud.mjs", "ci-release-desktop.sh"):
            (source / "scripts" / entry).write_text(SENTINEL)

    def api(self, token, method, path, body=None):
        self.calls.append((token, method, path, body))
        if method == "POST" and path.endswith("/deployments"):
            return {"id": 9}
        return None

    def child(self, command, environment, cwd=None):
        self.children.append((command, environment, cwd))
        return 0

    def invoke(self, **changes):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {**self.environment, **changes}, clear=True), patch.object(CONTROLLER, "installation_token", return_value="private_fixture_token") as token, patch.object(CONTROLLER, "clone_source", side_effect=self.clone), patch.object(CONTROLLER, "github_request", side_effect=self.api), patch.object(CONTROLLER, "run_process", side_effect=self.child), patch.object(CONTROLLER, "probe_build_log_access", side_effect=lambda env: self.probes.append(env)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = CONTROLLER.main()
        self.assertNotIn(SENTINEL, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        return code, stdout.getvalue(), token

    def test_controller_passes_refresh_helper_and_records_success_without_public_token(self):
        code, output, token = self.invoke(RELEASE_OPERATION="desktop")
        self.assertEqual(code, 0)
        self.assertEqual(output, "Release controller stage: auth.\nRelease controller stage: source.\nRelease controller stage: private-execution.\nPrivate stage exit code: 0.\nRelease controller complete.\n")
        token.assert_called_once_with(SOURCE, "read")
        child = self.children[0][1]
        self.assertEqual(child["GH_TOKEN"], "private_fixture_token")
        self.assertEqual(child["RELEASE_APP_PRIVATE_KEY"], SENTINEL)
        self.assertTrue(Path(child["RELEASE_TOKEN_REFRESH_HELPER"]).is_file())
        self.assertNotIn("PUBLIC_GH_TOKEN", child)
        self.assertFalse(self.source.exists())
        self.assertTrue(any(body and body.get("state") == "success" for _token, _method, _path, body in self.calls))
        self.assertEqual(self.calls[-1][1:3], ("DELETE", "/installation/token"))

    def test_verify_does_not_create_deployment_and_uses_read_permission(self):
        code, _output, token = self.invoke(RELEASE_OPERATION="verify")
        self.assertEqual(code, 0)
        token.assert_called_once_with(SOURCE, "read")
        self.assertFalse(any(method == "POST" for _token, method, _path, _body in self.calls))
        self.assertEqual(len(self.children), 2)
        self.assertEqual(self.children[0][0], [
            "gcloud", "run", "services", "describe", "teaching-api",
            "--project", "project-cd874991-605d-4143-a62", "--region", "asia-east1",
            "--format=value(metadata.name)",
        ])
        self.assertEqual(self.children[1][0][-1], "verify")
        self.assertEqual(len(self.probes), 1)
        self.assertFalse(any(part in ("build", "deploy", "submit") for command, _environment, _cwd in self.children for part in command))

    def test_cloud_access_probe_is_only_used_by_verify(self):
        for operation in ("cloud", "desktop", "resume"):
            self.children.clear()
            code, _output, token = self.invoke(RELEASE_OPERATION=operation)
            self.assertEqual(code, 0)
            token.assert_called_once_with(SOURCE, "read")
            self.assertFalse(any(command[0] == "gcloud" for command, _environment, _cwd in self.children))
        self.assertEqual(self.probes, [])

    def test_preexisting_workspace_is_preserved(self):
        self.source.mkdir()
        marker = self.source / "existing"
        marker.write_text("preserve")
        code, output, _token = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("withheld", output)
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual(self.children, [])

    def test_bad_source_and_branch_are_rejected_before_authentication(self):
        for changes in ({"SOURCE_REPOSITORY": "untrusted/source"}, {"GITHUB_REF": "refs/heads/untrusted"}, {"SOURCE_SHA": SENTINEL}):
            code, _output, token = self.invoke(**changes)
            self.assertEqual(code, 1)
            token.assert_not_called()

    def test_private_failure_and_cancellation_clean_owned_workspace(self):
        original = self.child
        for cancelled in (False, True):
            def failed(*_arguments, **_keywords):
                if cancelled:
                    raise CONTROLLER.ReleaseCancelled(143)
                raise RuntimeError(SENTINEL)
            self.child = failed
            code, output, _token = self.invoke()
            self.assertEqual(code, 143 if cancelled else 1)
            prefix = "Release controller stage: auth.\nRelease controller stage: source.\nRelease controller stage: private-execution.\n"
            self.assertEqual(output, prefix + ("Release controller cancelled.\n" if cancelled else "Release controller failed; private diagnostics were withheld.\n"))
            self.assertFalse(self.source.exists())
        self.child = original

    def test_private_wrapper_error_code_is_reported_without_private_output(self):
        def failed(*_arguments, **_keywords):
            return 22
        self.child = failed
        code, output, _token = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Private stage exit code: 22.\n", output)
        self.assertIn("Release controller failed; private diagnostics were withheld.", output)

    def test_verify_stage_is_reported_as_a_fixed_constant(self):
        code, output, _token = self.invoke(RELEASE_OPERATION="verify")
        self.assertEqual(code, 0)
        self.assertIn("Release controller stage: verify.\n", output)
        self.assertNotIn(SOURCE_SHA, output)
        self.assertNotIn(SOURCE, output)

    def test_clone_http_auth_is_only_in_environment_and_never_git_config(self):
        self.source.mkdir(mode=0o700)
        calls = []
        def git(command, environment, cwd=None):
            calls.append((command, dict(environment)))
            if command[1] in ("init", "remote"):
                return subprocess.run(command, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode
            return 0
        environment = {**os.environ, **self.environment, "GIT_CONFIG_VALUE_99": SENTINEL, "GIT_TRACE2": "1"}
        with patch.dict(os.environ, environment, clear=True), patch.object(CONTROLLER, "run_process", side_effect=git):
            CONTROLLER.clone_source(SOURCE, SOURCE_SHA, "private_fixture_token", self.source)
        encoded = base64.b64encode(b"x-access-token:private_fixture_token").decode()
        for command, environment in calls:
            self.assertNotIn(encoded, command)
            self.assertEqual(environment["GIT_CONFIG_KEY_0"], "http.https://github.com/" + SOURCE + ".git.extraheader")
            self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic " + encoded)
            for removed in ("RELEASE_APP_PRIVATE_KEY", "PUBLIC_GH_TOKEN", "R2_ACCESS_KEY_ID", "MACOS_CERTIFICATE_PASSWORD", "GIT_CONFIG_VALUE_99", "GIT_TRACE2"):
                self.assertNotIn(removed, environment)
        config = (self.source / ".git/config").read_text()
        self.assertNotIn(encoded, config)
        self.assertNotIn("extraheader", config)
        self.assertIn("https://github.com/" + SOURCE + ".git", config)
        # Exercise Git's actual URL-component matching without making a request.
        matched = subprocess.run(["git", "config", "--get-urlmatch", "http.extraheader", "https://github.com/" + SOURCE + ".git"], cwd=self.source, env=calls[0][1], capture_output=True, text=True, check=True)
        self.assertEqual(matched.stdout.strip(), "AUTHORIZATION: basic " + encoded)
        unrelated = subprocess.run(["git", "config", "--get-urlmatch", "http.extraheader", "https://github.com/unrelated/source.git"], cwd=self.source, env=calls[0][1], capture_output=True, text=True)
        self.assertEqual(unrelated.returncode, 1)
        self.assertEqual(unrelated.stdout, "")

    def test_process_cancellation_stops_entire_subprocess_group(self):
        for function in ("run_process", "run_probe_process"):
            with self.subTest(function=function):
                self.assert_process_cancellation(function)

    def assert_process_cancellation(self, function):
        child = self.root / "child.py"
        marker = self.root / (function + "-pids.json")
        child.write_text(
            "import json, os, pathlib, subprocess, sys, time\n"
            "nested = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(), nested.pid]))\n"
            f"print({SENTINEL!r}, flush=True)\n"
            f"print({SENTINEL!r}, file=sys.stderr, flush=True)\n"
            "time.sleep(60)\n"
        )
        controller = self.root / "controller.py"
        controller.write_text(
            "import os, signal, sys\n"
            f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
            "import release\n"
            "signal.signal(signal.SIGTERM, release.cancelled)\n"
            "try:\n"
            f"    release.{function}([sys.executable, {str(child)!r}], dict(os.environ))\n"
            "except release.ReleaseCancelled as error:\n"
            "    raise SystemExit(error.code)\n"
        )
        process = subprocess.Popen([sys.executable, str(controller)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        pids = json.loads(marker.read_text())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(stdout + stderr, "")
        for pid in pids:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), "Private process is still running")


class BuildLogProbeTests(unittest.TestCase):
    build_id = "8906b6b1-5b97-4b25-9f33-72a5bab28945"

    def invoke(self, results):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(CONTROLLER, "run_probe_process", side_effect=results) as run, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            CONTROLLER.probe_build_log_access({"PRIVATE_FIXTURE": SENTINEL})
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(SENTINEL, stdout.getvalue())
        return stdout.getvalue(), run

    def metadata(self):
        return 0, json.dumps([{"id": self.build_id, "private": SENTINEL}]).encode(), SENTINEL.encode()

    def test_available_reports_only_constant_and_does_not_capture_build_log_stdout(self):
        output, run = self.invoke([self.metadata(), (0, b"", SENTINEL.encode())])
        self.assertEqual(output, "Cloud Build log access: available.\n")
        self.assertEqual(run.call_args_list[0].kwargs, {"capture_stdout": True})
        self.assertEqual(run.call_args_list[1].kwargs, {})
        self.assertEqual(run.call_args_list[1].args[0], [
            "gcloud", "builds", "log", self.build_id, "--project", "project-cd874991-605d-4143-a62", "--region", "global",
        ])
        self.assertNotIn(self.build_id, output)

    def test_permission_failure_with_private_details_only_reports_allowlisted_category(self):
        for diagnostic in (b"ERROR: 403 " + SENTINEL.encode(), b"PermissionDenied: " + SENTINEL.encode(),
                           b"missing storage.objects.get " + SENTINEL.encode(),
                           b"You do not have permission " + SENTINEL.encode(),
                           b"Caller does not have permission " + SENTINEL.encode(),
                           b"You do not have read access " + SENTINEL.encode()):
            output, _run = self.invoke([self.metadata(), (1, b"", diagnostic)])
            self.assertEqual(output, "Cloud Build log access: permission-denied.\n")

    def test_unavailable_and_invalid_metadata_never_fail_verify_or_execute_untrusted_id(self):
        for result in ((1, SENTINEL.encode(), SENTINEL.encode()),
                       (0, SENTINEL.encode(), b""),
                       (0, json.dumps([{"id": SENTINEL}]).encode(), b""),
                       (0, b"[]", b""),
                       RuntimeError(SENTINEL)):
            output, run = self.invoke([result])
            self.assertEqual(output, "Cloud Build log access: unavailable.\n")
            self.assertEqual(run.call_count, 1)
        output, _run = self.invoke([self.metadata(), (1, b"", SENTINEL.encode())])
        self.assertEqual(output, "Cloud Build log access: unavailable.\n")

    def test_cancellation_propagates_without_printing_private_diagnostics(self):
        for results in ([CONTROLLER.ReleaseCancelled(143)], [self.metadata(), CONTROLLER.ReleaseCancelled(143)]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(CONTROLLER, "run_probe_process", side_effect=results), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(CONTROLLER.ReleaseCancelled) as caught:
                    CONTROLLER.probe_build_log_access({})
            self.assertEqual(caught.exception.code, 143)
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_process_output_is_private_and_bounded(self):
        code = f"import sys; sys.stdout.write({SENTINEL!r} * 10000); sys.stderr.write({SENTINEL!r} * 10000); sys.exit(1)"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result, captured, error = CONTROLLER.run_probe_process([sys.executable, "-c", code], dict(os.environ), capture_stdout=True)
        self.assertEqual(result, 1)
        self.assertEqual(len(captured), 8192)
        self.assertEqual(len(error), 8192)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        result, captured, error = CONTROLLER.run_probe_process([sys.executable, "-c", code], dict(os.environ))
        self.assertEqual(result, 1)
        self.assertEqual(captured, b"")
        self.assertEqual(len(error), 8192)


if __name__ == "__main__":
    unittest.main()
