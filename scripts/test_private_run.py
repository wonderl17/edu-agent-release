#!/usr/bin/env python3
"""Exercise output/file-command isolation without credentials or deployment."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


RUNNER = Path(__file__).with_name("private-run.py")
SENTINEL = "PRIVATE_SOURCE_AND_SECRET_SENTINEL_73d2"
FILE_COMMANDS = ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE")


class PrivateRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner_temp = self.root / "runner"
        self.runner_temp.mkdir()
        self.source = self.runner_temp / "source"
        self.source.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        node = self.bin / "node"
        node.write_text(f"#!{sys.executable}\nimport runpy, sys\nrunpy.run_path(sys.argv[1], run_name='__main__')\n")
        node.chmod(0o700)
        (self.source / "scripts").mkdir()
        (self.source / "scripts/ci-release-cloud.mjs").write_text(self.child_script())
        (self.source / "scripts/ci-release-desktop.sh").write_text(
            'printf "%s\\n" "' + SENTINEL + '"\n'
            'printf "%s\\n" "' + SENTINEL + '" >&2\n'
            'printf "%s" "$1" > mode.txt\n'
        )
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Release fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", "https://github.com/shukezhinao/edu-agent.git")
        self.git("add", ".")
        self.git("commit", "-m", "Fixture")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/main", self.sha)
        self.environment = dict(os.environ)
        self.environment.update({
            "RUNNER_TEMP": str(self.runner_temp),
            "RELEASE_SOURCE_DIR": str(self.source),
            "SOURCE_REPOSITORY": "shukezhinao/edu-agent",
            "SOURCE_SHA": self.sha,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "DEBUG": SENTINEL,
            "ACTIONS_STEP_DEBUG": "true",
            "ACTIONS_RUNNER_DEBUG": "true",
            "GIT_TRACE": "1",
            "GIT_TRACE2_EVENT": SENTINEL,
            "NODE_OPTIONS": "--trace-warnings",
            "CLOUDSDK_CORE_LOG_HTTP": "true",
        })
        self.command_files = {}
        for name in FILE_COMMANDS:
            path = self.root / name
            path.write_text("original")
            self.environment[name] = str(path)
            self.command_files[name] = path

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.source, check=True, capture_output=True, text=True).stdout

    def child_script(self):
        return f"""
import json, os, pathlib, stat, sys
print({SENTINEL!r})
print('::error::' + {SENTINEL!r}, file=sys.stderr)
print('::add-mask::' + {SENTINEL!r})
names = {FILE_COMMANDS!r}
for name in names:
    pathlib.Path(os.environ[name]).write_text('LEAK=' + {SENTINEL!r} + '\\n')
private = pathlib.Path(os.environ['GITHUB_ENV']).parent
record = {{
  'directory_mode': stat.S_IMODE(private.stat().st_mode),
  'log_mode': stat.S_IMODE((private / 'private.log').stat().st_mode),
  'file_modes': [stat.S_IMODE(pathlib.Path(os.environ[name]).stat().st_mode) for name in names],
  'file_parents': [str(pathlib.Path(os.environ[name]).parent) for name in names],
  'debug': [name for name in ('DEBUG', 'ACTIONS_STEP_DEBUG', 'ACTIONS_RUNNER_DEBUG', 'GIT_TRACE', 'GIT_TRACE2_EVENT', 'NODE_OPTIONS', 'CLOUDSDK_CORE_LOG_HTTP') if name in os.environ],
}}
pathlib.Path('observed.json').write_text(json.dumps(record))
if os.environ.get('FIXTURE_WAIT'):
    import time
    pathlib.Path('child.pid').write_text(str(os.getpid()))
    time.sleep(60)
sys.exit(int(os.environ.get('FIXTURE_EXIT', '0')))
"""

    def invoke(self, mode="cloud", **changes):
        environment = {**self.environment, **changes}
        result = subprocess.run([sys.executable, str(RUNNER), mode], env=environment, capture_output=True, text=True)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.runner_temp.glob("release-private-*")), [])
        for path in self.command_files.values():
            self.assertEqual(path.read_text(), "original")
        self.assertTrue(self.source.exists(), "Source is retained for workflow always() cleanup")
        return result

    def test_success_output_and_workflow_commands_are_isolated(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Release stage: cloud.\nRelease stage complete.\n")
        record = json.loads((self.source / "observed.json").read_text())
        self.assertEqual(record["directory_mode"], 0o700)
        self.assertEqual(record["log_mode"], 0o600)
        self.assertEqual(record["file_modes"], [0o600] * len(FILE_COMMANDS))
        self.assertEqual(len(set(record["file_parents"])), 1)
        self.assertEqual(record["debug"], [])
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o700)

    def test_failure_output_and_workflow_commands_are_isolated(self):
        result = self.invoke(FIXTURE_EXIT="7")
        self.assertEqual(result.returncode, 22)
        self.assertEqual(result.stdout, "Release stage: cloud.\nRelease stage failed (code=22).\n")

    def test_verify_does_not_execute_release_and_resets_branch_to_main(self):
        self.git("checkout", "--detach", self.sha)
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.source / "observed.json").exists())
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")

    def test_desktop_and_resume_pass_only_fixed_mode(self):
        for mode in ("desktop", "resume"):
            result = self.invoke(mode)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((self.source / "mode.txt").read_text(), mode)

    def test_arbitrary_repository_and_bad_sha_are_rejected(self):
        for changes in ({"SOURCE_REPOSITORY": "untrusted/repository"}, {"SOURCE_SHA": SENTINEL}):
            self.assertEqual(self.invoke(**changes).returncode, 20)

    def test_wrong_head_is_rejected(self):
        self.assertEqual(self.invoke(SOURCE_SHA="0" * 40).returncode, 21)

    def test_wrong_origin_is_rejected(self):
        self.git("remote", "set-url", "origin", "https://example.invalid/private.git")
        self.assertEqual(self.invoke().returncode, 21)

    def test_commit_outside_origin_main_is_rejected(self):
        (self.source / "extra.txt").write_text("unreleased")
        self.git("add", ".")
        self.git("commit", "-m", "Not on remote main")
        other_sha = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(self.invoke(SOURCE_SHA=other_sha).returncode, 21)

    def test_source_outside_runner_temp_is_rejected(self):
        self.assertEqual(self.invoke(RELEASE_SOURCE_DIR=str(self.root)).returncode, 20)

    def test_unknown_mode_never_echoes_the_input(self):
        result = self.invoke(SENTINEL)
        self.assertEqual(result.returncode, 20)
        self.assertEqual(result.stdout, "Release stage rejected (code=20).\n")

    def test_missing_entry_fails_without_traceback(self):
        (self.source / "scripts/ci-release-cloud.mjs").unlink()
        self.assertEqual(self.invoke().returncode, 22)

    def test_cancellation_stops_private_process_and_cleans_logs(self):
        process = subprocess.Popen(
            [sys.executable, str(RUNNER), "cloud"],
            env={**self.environment, "FIXTURE_WAIT": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        marker = self.source / "child.pid"
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("Release fixture exited before cancellation")
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        private_pid = int(marker.read_text())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(stdout, "Release stage: cloud.\nRelease stage cancelled (code=143).\n")
        self.assertEqual(stderr, "")
        with self.assertRaises(ProcessLookupError):
            os.kill(private_pid, 0)
        self.assertEqual(list(self.runner_temp.glob("release-private-*")), [])


if __name__ == "__main__":
    unittest.main()
