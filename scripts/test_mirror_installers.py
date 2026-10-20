import contextlib
from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import mirror_installers as MIRROR

VERSION = "1.2.3"
TAG = "desktop-v" + VERSION
SOURCE = "shukezhinao/edu-agent"
PUBLIC = "wonderl17/edu-agent-release"
PUBLIC_SHA = "b" * 40
SENTINEL = "PRIVATE_DIAGNOSTIC_AND_CREDENTIAL_SENTINEL"


def release(assets=(), *, draft=False):
    return {"id": 42, "tag_name": TAG, "draft": draft, "prerelease": False,
            "body": SENTINEL, "target_commitish": "a" * 40, "assets": list(assets)}


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.temporary = Path(self.directory.name)
        self.names = tuple("ShukeZhinao-" + VERSION + "-" + platform
                           for platform in ("mac-arm64.pkg", "mac-x64.pkg", "win-x64.exe"))
        self.bodies = {name: ("installer:" + name).encode() for name in self.names}
        self.assets = [{"name": name, "size": len(body),
                        "digest": "sha256:" + hashlib.sha256(body).hexdigest(), "state": "uploaded"}
                       for name, body in self.bodies.items()]
        self.source = release(deepcopy(self.assets))
        self.source["assets"].append({"name": "private-source-and-logs.zip", "size": 10,
                                      "digest": SENTINEL, "state": "uploaded"})
        self.destination = None
        self.calls = []
        self.commands = []
        self.download_change = None
        self.upload_change = None
        self.process_failure = None
        self.parameters = {"source_repository": SOURCE, "version": VERSION,
                           "source_token": "private_fixture_token", "public_repository": PUBLIC,
                           "public_sha": PUBLIC_SHA, "public_token": "public_fixture_token",
                           "temporary": self.temporary}
        api = patch.object(MIRROR, "github_request", side_effect=self.github)
        process = patch.object(MIRROR.subprocess, "Popen", side_effect=self.child)
        self.api, self.process = api.start(), process.start()
        self.addCleanup(api.stop)
        self.addCleanup(process.stop)

    def github(self, token, method, path, body=None, **keywords):
        self.calls.append((token, method, path, deepcopy(body), keywords))
        if path == f"/repos/{SOURCE}/releases/tags/{TAG}":
            self.assertEqual(token, self.parameters["source_token"])
            self.assertEqual(method, "GET")
            return deepcopy(self.source)
        self.assertEqual(token, self.parameters["public_token"])
        base = f"/repos/{PUBLIC}/releases"
        if method == "GET" and path == base + "/tags/" + TAG:
            self.assertEqual(keywords, {"allow_not_found": True})
            return deepcopy(self.destination) if self.destination and not self.destination["draft"] else None
        if method == "GET" and path == base + "?per_page=100&page=1":
            return [] if self.destination is None else [deepcopy(self.destination)]
        if method == "GET" and self.destination and path == base + "/" + str(self.destination["id"]):
            return deepcopy(self.destination)
        if method == "POST" and path == base:
            self.assertIsNone(self.destination)
            self.destination = {"id": 99, "assets": [], **deepcopy(body)}
            return deepcopy(self.destination)
        if method == "PATCH" and self.destination and path == base + "/" + str(self.destination["id"]):
            self.destination.update(body)
            return deepcopy(self.destination)
        self.fail("Unexpected API request")

    def child(self, command, **keywords):
        self.commands.append((command, keywords))
        self.assertEqual(command[:1], ["gh"])
        self.assertEqual(keywords["stdout"], subprocess.DEVNULL)
        self.assertEqual(keywords["stderr"], subprocess.DEVNULL)
        self.assertEqual(keywords["stdin"], subprocess.DEVNULL)
        self.assertTrue(keywords["start_new_session"])
        self.assertEqual(stat.S_IMODE(Path(keywords["cwd"]).stat().st_mode), 0o700)
        environment = keywords["env"]
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("PUBLIC_GH_TOKEN", environment)
        self.assertNotIn("RELEASE_APP_PRIVATE_KEY", environment)
        self.assertNotIn("MACOS_CERTIFICATE_PASSWORD", environment)
        self.assertNotIn(SENTINEL, str(environment))
        self.assertEqual(environment["GH_HOST"], "github.com")
        self.assertEqual(environment.get("HOME"), os.environ.get("HOME"))
        self.assertEqual(environment["GH_CONFIG_DIR"], str(Path(keywords["cwd"]) / "gh-config"))
        self.assertNotIn("--clobber", command)
        self.assertNotIn(self.parameters["source_token"], command)
        self.assertNotIn(self.parameters["public_token"], command)
        if command[1:3] == ["release", "download"]:
            self.assertEqual(environment["GH_TOKEN"], self.parameters["source_token"])
            self.assertEqual(command[command.index("--repo") + 1], SOURCE)
            patterns = [command[index + 1] for index, item in enumerate(command) if item == "--pattern"]
            self.assertEqual(patterns, list(self.names))
            downloads = Path(command[command.index("--dir") + 1])
            for name, body in self.bodies.items():
                (downloads / name).write_bytes(body)
            if self.download_change:
                self.download_change(downloads)
        elif command[1:3] == ["release", "upload"]:
            self.assertEqual(environment["GH_TOKEN"], self.parameters["public_token"])
            self.assertEqual(command[command.index("--repo") + 1], PUBLIC)
            self.assertTrue(self.destination["draft"])
            files = command[4:command.index("--repo")]
            for file in files:
                name = Path(file).name
                self.assertIn(name, self.names)
                self.assertEqual(Path(file).read_bytes(), self.bodies[name])
                self.destination["assets"].append(deepcopy(next(asset for asset in self.assets if asset["name"] == name)))
            if self.upload_change:
                self.upload_change()
        else:
            self.fail("Unexpected command")
        child = MagicMock()
        child.wait.return_value = 0
        if self.process_failure:
            child.wait.side_effect = self.process_failure
        return child

    def mirror(self, **changes):
        return MIRROR.mirror_installers(**{**self.parameters, **changes})

    def assert_no_public_write(self):
        self.assertFalse(any(method != "GET" for _token, method, _path, _body, _keywords in self.calls))

    def assert_clean(self):
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_only_verified_installers_are_published_with_public_commit_and_static_notes(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        secrets = {"PUBLIC_GH_TOKEN": SENTINEL, "GITHUB_TOKEN": SENTINEL,
                   "RELEASE_APP_PRIVATE_KEY": SENTINEL, "MACOS_CERTIFICATE_PASSWORD": SENTINEL}
        with patch.dict(os.environ, secrets), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertIsNone(self.mirror())
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertEqual([Path(asset["name"]).name for asset in self.destination["assets"]], list(self.names))
        self.assertFalse(self.destination["draft"])
        self.assertEqual(self.destination["target_commitish"], PUBLIC_SHA)
        self.assertEqual(self.destination["make_latest"], "legacy")
        self.assertNotIn(SENTINEL, self.destination["body"])
        self.assertEqual(self.destination["name"], "数科智脑 v" + VERSION)
        self.assertEqual(len(self.commands), 2)
        self.assert_clean()

    def test_source_release_and_installer_metadata_fail_closed(self):
        changes = (
            lambda source: source.update(draft=True),
            lambda source: source.update(prerelease=True),
            lambda source: source.update(tag_name="desktop-v9.9.9"),
            lambda source: source["assets"].pop(0),
            lambda source: source["assets"].append(deepcopy(source["assets"][0])),
            lambda source: source["assets"][0].update(size=0),
            lambda source: source["assets"][0].update(size=True),
            lambda source: source["assets"][0].update(size=MIRROR.MAX_ASSET_BYTES + 1),
            lambda source: source["assets"][0].update(digest=None),
            lambda source: source["assets"][0].update(digest="sha1:" + "a" * 64),
            lambda source: source["assets"][0].update(state="new"),
            lambda source: source["assets"][0].update(name="../" + self.names[0]),
        )
        original = deepcopy(self.source)
        for change in changes:
            with self.subTest(change=change):
                self.source = deepcopy(original)
                change(self.source)
                with self.assertRaises(MIRROR.MirrorInstallersError) as failure:
                    self.mirror()
                self.assertEqual(str(failure.exception), "Installer mirror failed")
                self.assert_no_public_write()
                self.assertFalse(self.commands)
                self.assert_clean()

    def test_download_bytes_size_shape_and_symlinks_are_checked_before_public_mutation(self):
        def symlink(downloads):
            (downloads / self.names[0]).unlink()
            (downloads / self.names[0]).symlink_to(downloads / self.names[1])
        changes = (
            lambda downloads: (downloads / self.names[0]).write_bytes(b"x" * len(self.bodies[self.names[0]])),
            lambda downloads: (downloads / self.names[0]).write_bytes(b"short"),
            lambda downloads: (downloads / self.names[0]).unlink(),
            lambda downloads: (downloads / "source.zip").write_text(SENTINEL),
            symlink,
        )
        for change in changes:
            with self.subTest(change=change):
                self.download_change = change
                with self.assertRaises(MIRROR.MirrorInstallersError):
                    self.mirror()
                self.assert_no_public_write()
                self.assert_clean()

    def test_remote_uploaded_hash_is_verified_before_publishing(self):
        self.upload_change = lambda: self.destination["assets"][0].update(digest="sha256:" + "0" * 64)
        with self.assertRaises(MIRROR.MirrorInstallersError):
            self.mirror()
        self.assertTrue(self.destination["draft"])
        self.assertFalse(any(method == "PATCH" for _token, method, _path, _body, _keywords in self.calls))
        self.assert_clean()

    def test_identical_published_release_is_idempotent_and_different_assets_are_never_clobbered(self):
        self.destination = release(deepcopy(self.assets))
        self.mirror()
        self.assert_no_public_write()
        self.assertEqual(len(self.commands), 1)
        self.destination["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(MIRROR.MirrorInstallersError):
            self.mirror()
        self.assert_no_public_write()
        self.assertEqual(len(self.commands), 1)
        self.assert_clean()

    def test_partial_interrupted_draft_reuses_identical_assets_and_uploads_missing_installers(self):
        self.destination = release(deepcopy(self.assets[:1]), draft=True)
        self.mirror()
        self.assertFalse(any(method == "POST" for _token, method, _path, _body, _keywords in self.calls))
        self.assertFalse(self.destination["draft"])
        upload = self.commands[1][0]
        self.assertEqual([Path(file).name for file in upload[4:upload.index("--repo")]], list(self.names[1:]))
        self.assert_clean()

    def test_unexpected_or_duplicate_public_assets_are_rejected_without_mutation(self):
        for asset in ({"name": "logs.txt"}, deepcopy(self.assets[0])):
            with self.subTest(asset=asset):
                self.destination = release([*deepcopy(self.assets), asset])
                with self.assertRaises(MIRROR.MirrorInstallersError):
                    self.mirror()
                self.assert_no_public_write()
                self.assertFalse(self.commands)

    def test_cancellation_and_timeout_stop_process_group_and_remove_downloads(self):
        for failure in (KeyboardInterrupt(), SystemExit(143), subprocess.TimeoutExpired("gh", 1800)):
            with self.subTest(failure=failure), patch.object(MIRROR, "stop_process") as stop:
                self.process_failure = failure
                error = type(failure) if not isinstance(failure, Exception) else MIRROR.MirrorInstallersError
                with self.assertRaises(error):
                    self.mirror()
                stop.assert_called_once()
                stop.call_args.args[0].wait.assert_called_once_with(timeout=MIRROR.COMMAND_TIMEOUT_SECONDS)
                self.assert_no_public_write()
                self.assert_clean()

    def test_api_errors_are_fixed_and_do_not_print_private_details(self):
        self.api.side_effect = RuntimeError(SENTINEL)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(MIRROR.MirrorInstallersError) as failure:
                self.mirror()
        self.assertEqual(str(failure.exception), "Installer mirror failed")
        self.assertIsNone(failure.exception.__cause__)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_invalid_inputs_never_reach_api_or_processes(self):
        for changes in ({"source_repository": "untrusted/source"}, {"public_repository": SOURCE},
                        {"public_sha": "main"}, {"version": "1.2.3/path"}, {"version": "01.2.3"},
                        {"source_token": "injected\ntoken"}, {"public_token": ""}):
            with self.subTest(changes=changes), self.assertRaises(MIRROR.MirrorInstallersError):
                self.mirror(**changes)
        self.api.assert_not_called()
        self.process.assert_not_called()

    def test_draft_lookup_is_paginated_duplicate_safe_and_bounded(self):
        other = {"tag_name": "other"}
        for responses, expected in (([None, [other] * 100, [release(draft=True)]], "found"),
                                    ([None, [release(draft=True)] * 2], "error"),
                                    ([None, [*([other] * 99), release(draft=True)], [release(draft=True)]], "error"),
                                    ([None, *([[other] * 100] * 10)], "error")):
            with self.subTest(expected=expected), patch.object(MIRROR, "github_request", side_effect=deepcopy(responses)) as api:
                if expected == "found":
                    self.assertEqual(MIRROR._destination_release("token", "/releases", TAG)["tag_name"], TAG)
                    self.assertEqual(api.call_args_list[-1].args[2], "/releases?per_page=100&page=2")
                else:
                    with self.assertRaises(ValueError):
                        MIRROR._destination_release("token", "/releases", TAG)
                self.assertLessEqual(api.call_count, 11)


if __name__ == "__main__":
    unittest.main()
