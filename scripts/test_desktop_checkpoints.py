"""Trusted checkpoint extraction never opens a private stdout/log channel."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import desktop_checkpoints as checkpoints
import release


SENTINEL = "PRIVATE_CHECKPOINT_ARG_ENV_ERROR_SENTINEL_812c"
SCRIPTS = Path(__file__).parent.resolve()
PRELOAD = SCRIPTS / "desktop-checkpoints.cjs"
NODE = shutil.which("node")


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.channel = checkpoints.CheckpointFile(self.root)
        self.addCleanup(self.channel.close)

    def report(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            checkpoints.report_checkpoints(self.channel.path)
        self.assertNotIn(SENTINEL, output.getvalue())
        return output.getvalue()

    def test_permissions_and_unknown_partial_nonascii_records_are_discarded(self):
        self.assertEqual(self.channel.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.channel.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(checkpoints.validated_checkpoint_path(str(self.channel.path), self.root), self.channel.path)
        self.channel.path.write_bytes(b"D_VITE start\n" + SENTINEL.encode() + b"\nD_VITE " + SENTINEL.encode()
                                      + b"\nD_UNKNOWN success\nD_ENTRY failure extra\n\xff\nD_VITE failure\nD_ENTRY success")
        self.assertEqual(self.report(), "Desktop checkpoint: D_VITE start.\nDesktop checkpoint: D_VITE failure.\n")
        self.channel.path.chmod(0o644)
        self.assertIsNone(checkpoints.validated_checkpoint_path(str(self.channel.path), self.root))
        self.assertEqual(self.report(), "")

    def test_symlinks_foreign_paths_and_oversized_channels_are_rejected(self):
        target = self.root / "secret"
        target.write_text(SENTINEL)
        self.channel.path.unlink()
        self.channel.path.symlink_to(target)
        self.assertIsNone(checkpoints.validated_checkpoint_path(str(self.channel.path), self.root))
        self.assertEqual(self.report(), "")
        self.channel.path.unlink()
        self.channel.path.write_bytes(b"D_ENTRY start\n" * checkpoints.MAX_BYTES)
        self.channel.path.chmod(0o600)
        self.assertEqual(self.report(), "")
        self.assertIsNone(checkpoints.validated_checkpoint_path(str(target), self.root))

    def test_public_output_is_bounded(self):
        self.channel.path.write_bytes(b"D_ENTRY start\n" * 500 + b"D_VITE failure\n")
        output = self.report()
        self.assertEqual(len(output.splitlines()), checkpoints.MAX_RECORDS)
        self.assertTrue(output.endswith("Desktop checkpoint: D_VITE failure.\n"))

    def test_replaced_fifo_is_rejected_without_waiting_for_a_writer(self):
        self.channel.path.unlink()
        os.mkfifo(self.channel.path, 0o600)
        started = time.monotonic()
        self.assertEqual(self.report(), "")
        self.assertLess(time.monotonic() - started, 1)

    def test_controller_reports_after_child_failure_and_cleans_the_file(self):
        observed = []
        def fail(_command, environment):
            path = Path(environment[checkpoints.FILE_ENV])
            observed.append(path)
            self.assertNotEqual(str(path), SENTINEL)
            path.write_text("D_DEPENDENCIES start\nD_DEPENDENCIES failure\n" + SENTINEL + "\n")
            return 22
        output = io.StringIO()
        with patch.object(release, "run_process", side_effect=fail), contextlib.redirect_stdout(output):
            self.assertEqual(release.run_private_stage("desktop", {
                "RUNNER_TEMP": str(self.root), checkpoints.FILE_ENV: SENTINEL,
            }), 22)
        self.assertEqual(output.getvalue(), "Desktop checkpoint: D_DEPENDENCIES start.\nDesktop checkpoint: D_DEPENDENCIES failure.\n")
        self.assertFalse(observed[0].parent.exists())

    def test_cancellation_preserves_the_original_result_and_cleans_the_channel(self):
        observed = []
        def cancel(_command, environment):
            path = Path(environment[checkpoints.FILE_ENV])
            observed.append(path)
            path.write_text("D_PACKAGE start\n")
            raise release.ReleaseCancelled(143)
        output = io.StringIO()
        with patch.object(release, "run_process", side_effect=cancel), contextlib.redirect_stdout(output):
            with self.assertRaises(release.ReleaseCancelled) as error:
                release.run_private_stage("desktop", {"RUNNER_TEMP": str(self.root)})
        self.assertEqual(error.exception.code, 143)
        self.assertEqual(output.getvalue(), "Desktop checkpoint: D_PACKAGE start.\n")
        self.assertFalse(observed[0].parent.exists())

    def test_diagnostic_io_failure_is_nonblocking_and_cloud_does_not_opt_in(self):
        with patch.object(release, "CheckpointFile", side_effect=OSError(SENTINEL)), patch.object(release, "run_process", return_value=22) as run:
            self.assertEqual(release.run_private_stage("desktop", {"RUNNER_TEMP": str(self.root)}), 22)
            self.assertNotIn(checkpoints.FILE_ENV, run.call_args.args[1])
        with patch.object(release, "run_process", return_value=0) as run:
            self.assertEqual(release.run_private_stage("cloud", {checkpoints.FILE_ENV: SENTINEL}), 0)
            self.assertNotIn(checkpoints.FILE_ENV, run.call_args.args[1])

    @unittest.skipUnless(NODE, "Node is required for the actual preload")
    def test_preload_captures_esm_nested_failure_and_preserves_spawn_options(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        nested = self.root / "stage-sidecar-runtime.cjs"
        nested.write_text("if (!process.env.NODE_OPTIONS || !process.env.RELEASE_DESKTOP_CHECKPOINT_FILE) process.exit(99);\n"
                          "console.log(process.argv, process.env.PRIVATE_VALUE);\nprocess.exit(7);\n")
        package_manager = bin_dir / "pnpm"
        package_manager.write_text(f"#!{NODE}\nconst {{spawnSync}}=require('node:child_process');\n"
                                   f"process.exit(spawnSync(process.execPath,[{json.dumps(str(nested))},{json.dumps(SENTINEL)}],"
                                   "{env:{PRIVATE_VALUE:process.env.PRIVATE_VALUE},stdio:'inherit'}).status);\n")
        package_manager.chmod(0o700)
        native = bin_dir / "vpk"
        native.write_text(f"#!{sys.executable}\nimport json,os,sys\n"
                          "assert 'NODE_OPTIONS' not in os.environ\n"
                          "assert 'RELEASE_DESKTOP_CHECKPOINT_FILE' not in os.environ\n"
                          "if len(sys.argv) > 1: assert os.environ['PRIVATE_VALUE'] == sys.argv[-1]\n"
                          "assert os.getcwd() == os.environ['EXPECTED_CWD']\n"
                          "sys.stdout.write('native-ok')\n")
        native.chmod(0o700)
        entry = self.root / "ci-release-desktop.mjs"
        entry.write_text("import assert from 'node:assert/strict';\nimport {spawn,spawnSync} from 'node:child_process';\n"
                         f"const secret={json.dumps(SENTINEL)};\nconst original={{PRIVATE_VALUE:secret,EXPECTED_CWD:{json.dumps(str(self.root))},NODE_OPTIONS:secret}};\n"
                         f"const native=spawnSync({json.dumps(str(native))},['pack',secret],{{env:original,cwd:{json.dumps(str(self.root))},encoding:'utf8',input:'unused',timeout:5000}});\n"
                         "assert.equal(native.status,0,native.stderr);assert.equal(native.stdout,'native-ok');assert.equal(original.NODE_OPTIONS,secret);\n"
                         f"for (const action of ['download','pack','delta']) {{const result=spawnSync({json.dumps(str(native))},['[osx]','--yes','--skip-updates',action,secret],{{env:original,cwd:{json.dumps(str(self.root))},encoding:'utf8'}});assert.equal(result.status,0,result.stderr);}}\n"
                         f"for (const args of [undefined,null]) {{const result=spawnSync({json.dumps(str(native))},args,{{env:original,cwd:{json.dumps(str(self.root))},encoding:'utf8',stdio:'pipe',timeout:5000}});assert.equal(result.status,0,result.stderr);assert.equal(result.stdout,'native-ok');}}\n"
                         f"await new Promise((resolve,reject)=>{{const child=spawn({json.dumps(str(native))},undefined,{{env:original,cwd:{json.dumps(str(self.root))},stdio:'pipe'}});let text='';child.stdout.on('data',value=>text+=value);child.once('error',reject);child.once('close',code=>{{assert.equal(code,0);assert.equal(text,'native-ok');resolve();}});}});\n"
                         "assert.throws(()=>spawnSync('ignored',[],42),{code:'ERR_INVALID_ARG_TYPE'});\n"
                         "assert.throws(()=>spawnSync('ignored',[],[]),{code:'ERR_INVALID_ARG_TYPE'});\n"
                         "assert.throws(()=>spawn('ignored',[],[]),{code:'ERR_INVALID_ARG_TYPE'});\n"
                         f"const child=spawn({json.dumps(str(package_manager))},['install',secret],{{env:{{PRIVATE_VALUE:secret,NODE_OPTIONS:secret}},stdio:'inherit'}});\n"
                         "child.once('close',code=>{process.exitCode=code;});\n")
        result = subprocess.run([NODE, "--require", str(PRELOAD), str(entry)], env={
            **os.environ, checkpoints.FILE_ENV: str(self.channel.path),
        }, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)
        output = self.report()
        for record in ("D_ENTRY start", "D_VPK_PACK start", "D_VPK_PACK success", "D_VPK_DOWNLOAD success", "D_VPK_DELTA success", "D_DEPENDENCIES start", "D_SIDECAR failure", "D_DEPENDENCIES failure", "D_ENTRY failure"):
            self.assertIn("Desktop checkpoint: " + record + ".\n", output)
        self.assertLessEqual(self.channel.path.stat().st_size, checkpoints.MAX_BYTES)

    @unittest.skipUnless(NODE, "Node is required for the actual preload")
    def test_preload_io_failure_does_not_change_child_results(self):
        result = subprocess.run([NODE, "--require", str(PRELOAD), "-e",
                                 "require('node:child_process').spawnSync(process.execPath,['-e','process.exit(0)']); process.exit(9)"],
                                env={**os.environ, checkpoints.FILE_ENV: str(self.root / "missing" / "events")},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout + result.stderr, "")

    @unittest.skipUnless(NODE, "Node is required for the actual preload")
    def test_preload_replaced_fifo_never_blocks_the_build(self):
        self.channel.path.unlink()
        os.mkfifo(self.channel.path, 0o600)
        entry = self.root / "ci-release-desktop.mjs"
        entry.write_text("process.exitCode=9;\n")
        result = subprocess.run([NODE, "--require", str(PRELOAD), str(entry)],
                                env={**os.environ, checkpoints.FILE_ENV: str(self.channel.path)},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout + result.stderr, "")

    @unittest.skipUnless(NODE, "Node is required for the actual preload")
    def test_preload_file_is_bounded_under_repeated_events(self):
        entry = self.root / "ci-release-desktop.mjs"
        entry.write_text("import {spawnSync} from 'node:child_process';\nfor(let i=0;i<400;i++) spawnSync('/missing/vpk',['pack']);\nprocess.exitCode=7;\n")
        result = subprocess.run([NODE, "--require", str(PRELOAD), str(entry)],
                                env={**os.environ, checkpoints.FILE_ENV: str(self.channel.path)},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertLessEqual(self.channel.path.stat().st_size, checkpoints.MAX_BYTES)
        output = self.report()
        self.assertEqual(len(output.splitlines()), checkpoints.MAX_RECORDS)
        self.assertTrue(output.endswith("Desktop checkpoint: D_ENTRY failure.\n"))


if __name__ == "__main__":
    unittest.main()
