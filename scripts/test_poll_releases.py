#!/usr/bin/env python3
"""No-network tests of polling, strict metadata parsing, and attempt cursors."""

import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("poll_releases", Path(__file__).with_name("poll-releases.py"))
POLL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLL)
SOURCE = "shukezhinao/edu-agent"
SOURCE_SHA = "a" * 40
PUBLIC = "wonderl17/edu-agent-release"
SENTINEL = "PRIVATE_PAYLOAD_SENTINEL_97ad"


def package(version):
    return {"encoding": "base64", "content": base64.b64encode(json.dumps({"version": version, "private": SENTINEL}).encode()).decode()}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.config = {"sources": [
            {"repository": SOURCE, "automatic": True, "baseline": {"cloud": "0.0.121", "desktop": "0.0.121"}},
            {"repository": "wonderl17/edu-agent", "automatic": False, "baseline": {"cloud": "0.0.121", "desktop": "0.0.121"}},
        ]}
        self.environment = {"PUBLIC_REPOSITORY": PUBLIC, "PUBLIC_GH_TOKEN": "public-fixture-token", "GITHUB_SHA": "b" * 40}
        self.versions = {"cloud": "0.0.122", "desktop": "0.0.123"}
        self.attempts = {"cloud": [], "desktop": []}
        self.calls = []
        self.token_calls = []
        self.fail_dispatch = False

    def token(self, repository, permission):
        self.token_calls.append((repository, permission))
        return "private-read-fixture-token"

    def api(self, token, method, path, body=None):
        self.calls.append((token, method, path, copy.deepcopy(body)))
        if path == f"/repos/{SOURCE}/commits/main":
            return {"sha": SOURCE_SHA, "private": SENTINEL}
        if path == f"/repos/{SOURCE}/contents/package.json?ref={SOURCE_SHA}":
            return package(self.versions["cloud"])
        if path == f"/repos/{SOURCE}/contents/apps/desktop/package.json?ref={SOURCE_SHA}":
            return package(self.versions["desktop"])
        if method == "GET" and path.startswith(f"/repos/{PUBLIC}/deployments?"):
            operation = "cloud" if "task=release-cloud" in path else "desktop"
            return copy.deepcopy(self.attempts[operation])
        if method == "POST" and path == f"/repos/{PUBLIC}/deployments":
            operation = body["payload"]["operation"]
            attempt = {**copy.deepcopy(body), "id": 100 + len(self.attempts[operation])}
            self.attempts[operation].insert(0, attempt)
            return copy.deepcopy(attempt)
        if method == "POST" and path.endswith("/dispatches"):
            if self.fail_dispatch:
                raise RuntimeError(SENTINEL)
            return None
        if method == "POST" and path.endswith("/statuses"):
            return {"id": 500}
        self.fail("Unexpected API request")

    def run_poll(self):
        return POLL.poll(self.config, self.environment, self.api, self.token)

    def test_records_attempt_before_dispatch_for_both_components(self):
        self.assertEqual(self.run_poll(), 2)
        self.assertEqual(self.token_calls, [(SOURCE, "read")])
        mutations = [(path, body) for _token, method, path, body in self.calls if method == "POST"]
        self.assertEqual(len(mutations), 4)
        for index, operation in ((0, "cloud"), (2, "desktop")):
            self.assertEqual(mutations[index][0], f"/repos/{PUBLIC}/deployments")
            attempt = mutations[index][1]
            self.assertEqual(attempt["ref"], self.environment["GITHUB_SHA"])
            self.assertIs(attempt["auto_merge"], False)
            self.assertEqual(attempt["required_contexts"], [])
            self.assertEqual(attempt["payload"], {"source_repository": SOURCE, "source_sha": SOURCE_SHA, "version": self.versions[operation], "operation": operation})
            inputs = mutations[index + 1][1]["inputs"]
            self.assertEqual(inputs, {"source_repository": SOURCE, "source_sha": SOURCE_SHA, "operation": operation, "target": "all", "confirm_release": "yes"})
        self.assertNotIn(SENTINEL, json.dumps(mutations))

    def test_repeated_poll_does_not_dispatch_attempted_versions(self):
        self.assertEqual(self.run_poll(), 2)
        self.calls.clear()
        self.assertEqual(self.run_poll(), 0)
        self.assertFalse(any(method == "POST" for _token, method, _path, _body in self.calls))

    def test_baseline_suppresses_equal_and_older_versions(self):
        self.versions = {"cloud": "0.0.121", "desktop": "0.0.120"}
        self.assertEqual(self.run_poll(), 0)

    def test_failed_dispatch_is_recorded_and_not_automatically_retried(self):
        self.fail_dispatch = True
        with self.assertRaises(POLL.PollFailure) as failure:
            self.run_poll()
        self.assertEqual(failure.exception.code, 33)
        self.assertNotIn(SENTINEL, str(failure.exception))
        self.assertEqual(self.attempts["cloud"][0]["payload"]["version"], "0.0.122")
        self.assertEqual(self.calls[-1][3]["state"], "error")
        self.fail_dispatch = False
        self.calls.clear()
        self.assertEqual(self.run_poll(), 1)  # Independent desktop component still proceeds.
        dispatched = [body["inputs"]["operation"] for _token, method, path, body in self.calls if method == "POST" and path.endswith("/dispatches")]
        self.assertEqual(dispatched, ["desktop"])

    def test_config_validation_happens_before_credentials_or_network(self):
        self.config["sources"][1]["repository"] = "untrusted/source"
        with self.assertRaises(POLL.PollFailure) as failure:
            self.run_poll()
        self.assertEqual(failure.exception.code, 30)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.token_calls, [])

    def test_bad_private_version_is_fixed_failure_without_content(self):
        self.versions["cloud"] = SENTINEL
        with self.assertRaises(POLL.PollFailure) as failure:
            self.run_poll()
        self.assertEqual(failure.exception.code, 31)
        self.assertNotIn(SENTINEL, str(failure.exception))
        self.assertFalse(any(method == "POST" for _token, method, _path, _body in self.calls))

    def test_automatic_false_never_reads_or_dispatches_source(self):
        for source in self.config["sources"]:
            source["automatic"] = False
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.token_calls, [])

    def test_other_source_attempt_does_not_hide_new_version(self):
        self.attempts["cloud"] = [{"task": "release-cloud", "environment": POLL.ENVIRONMENT, "payload": {"source_repository": "wonderl17/edu-agent", "operation": "cloud", "version": "9.0.0"}}]
        self.assertEqual(self.run_poll(), 2)

    def test_manual_attempt_in_production_environment_also_prevents_duplicate(self):
        self.attempts["cloud"] = [{
            "task": "release-cloud", "environment": "production-cloud",
            "payload": {"source_repository": SOURCE, "operation": "cloud", "version": "0.0.122", "source_sha": SOURCE_SHA},
        }]
        self.assertEqual(self.run_poll(), 1)
        dispatched = [body["inputs"]["operation"] for _token, method, path, body in self.calls if method == "POST" and path.endswith("/dispatches")]
        self.assertEqual(dispatched, ["desktop"])
        self.assertTrue(all("environment=" not in path for _token, method, path, _body in self.calls if method == "GET" and "/deployments?" in path))

    def test_pagination_finds_older_matching_attempt(self):
        pages = []
        original_api = self.api
        def paginated(token, method, path, body=None):
            if method == "GET" and "task=release-cloud" in path:
                pages.append(path)
                if path.endswith("&page=1"):
                    return [{"task": "other", "payload": {}}] * 100
                return [{"task": "release-cloud", "environment": POLL.ENVIRONMENT, "payload": {"source_repository": SOURCE, "operation": "cloud", "version": "0.0.122"}}]
            return original_api(token, method, path, body)
        self.assertEqual(POLL.poll(self.config, self.environment, paginated, self.token), 1)
        self.assertEqual(len(pages), 2)

    def test_semver_precedence_and_strict_syntax(self):
        sequence = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        for left, right in zip(sequence, sequence[1:]):
            self.assertLess(POLL.compare_versions(left, right), 0)
            self.assertGreater(POLL.compare_versions(right, left), 0)
        self.assertEqual(POLL.compare_versions("1.0.0+build1", "1.0.0+build2"), 0)
        for invalid in ("01.0.0", "1.00.0", "1.0", "v1.0.0", "1.0.0-01", "1.0.0-", "1.0.0+", "1.0.0\n", "１.0.0"):
            with self.assertRaises(ValueError):
                POLL.parse_version(invalid)

    def test_cli_hides_private_exception_text_and_tracebacks(self):
        def fail_private(*_arguments, **_keywords):
            raise RuntimeError(SENTINEL)
        auth = types.SimpleNamespace(github_request=fail_private, installation_token=self.token)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"release_auth": auth}), patch.dict(os.environ, self.environment), patch.object(Path, "read_text", return_value=json.dumps(self.config)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(POLL.main(), 31)
        self.assertEqual(stdout.getvalue(), "Release polling started.\nRelease polling failed (code=31).\n")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
