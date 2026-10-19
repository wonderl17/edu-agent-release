import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import release_auth as AUTH


SENTINEL = "PRIVATE_KEY_AND_RESPONSE_SENTINEL_6a7e"
PEM = "-----BEGIN PRIVATE KEY-----\n" + SENTINEL + "\n-----END PRIVATE KEY-----\n"


class AuthTests(unittest.TestCase):
    def test_api_json_and_empty_responses_use_nonredirecting_bearer_request(self):
        response = MagicMock()
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(AUTH, "build_opener", return_value=opener) as factory:
            response.read.return_value = b'{"id": 9}'
            self.assertEqual(AUTH.github_request("fixture_token", "POST", "/fixture", {"safe": True}), {"id": 9})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.github.com/fixture")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture_token")
            self.assertEqual(json.loads(request.data), {"safe": True})
            self.assertIsInstance(factory.call_args.args[0], AUTH.NoRedirect)
            response.read.return_value = b""
            self.assertIsNone(AUTH.github_request("fixture_token", "DELETE", "/installation/token"))
        self.assertIsNone(AUTH.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_api_rejects_unsafe_input_and_hides_private_errors(self):
        with patch.object(AUTH, "build_opener") as factory:
            for path in ("https://evil.invalid", "//evil.invalid", "/api\n" + SENTINEL, "/api#fragment", "/api\\host"):
                with self.assertRaises(AUTH.ReleaseAuthError) as failure:
                    AUTH.github_request("fixture_token", "GET", path)
                self.assertEqual(str(failure.exception), "GitHub API request failed")
            factory.assert_not_called()
        with patch.object(AUTH, "build_opener", side_effect=RuntimeError(SENTINEL)):
            with self.assertRaises(AUTH.ReleaseAuthError) as failure:
                AUTH.github_request("fixture_token", "GET", "/fixture")
            self.assertNotIn(SENTINEL, str(failure.exception))

    def test_jwt_key_permissions_cleanup_and_signing_output_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            def process(command, **keywords):
                key = Path(command[-1])
                paths.append(key)
                self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(key.parent.stat().st_mode), 0o700)
                self.assertEqual(key.read_text(), PEM)
                self.assertNotIn(PEM, command)
                self.assertNotIn("RELEASE_APP_PRIVATE_KEY", keywords["env"])
                self.assertTrue(keywords["start_new_session"])
                self.assertEqual(keywords["stderr"], subprocess.PIPE)
                child = MagicMock()
                child.returncode = 0
                child.communicate.return_value = (b"fixture-signature", SENTINEL.encode())
                return child
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"RUNNER_TEMP": temporary, "RELEASE_APP_ID": "5379199", "RELEASE_APP_PRIVATE_KEY": PEM}), patch.object(AUTH.subprocess, "Popen", side_effect=process), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                token = AUTH.app_jwt()
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
            self.assertFalse(paths[0].exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])
            header, body, signature = token.split(".")
            self.assertEqual(json.loads(base64.urlsafe_b64decode(header + "=="))["alg"], "RS256")
            claims = json.loads(base64.urlsafe_b64decode(body + "=="))
            self.assertEqual(claims["iss"], "5379199")
            self.assertEqual(claims["exp"] - claims["iat"], 600)
            self.assertEqual(base64.urlsafe_b64decode(signature + "=="), b"fixture-signature")

    def test_jwt_cancellation_stops_openssl_and_removes_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            child = MagicMock()
            child.communicate.side_effect = KeyboardInterrupt()
            with patch.dict(os.environ, {"RUNNER_TEMP": temporary, "RELEASE_APP_ID": "5379199", "RELEASE_APP_PRIVATE_KEY": PEM}), patch.object(AUTH.subprocess, "Popen", return_value=child), patch.object(AUTH, "stop_process") as stop:
                with self.assertRaises(KeyboardInterrupt):
                    AUTH.app_jwt()
                stop.assert_called_once_with(child)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_installation_tokens_are_restricted_to_requested_repository_and_permission(self):
        for repository in AUTH.REPOSITORIES:
            for permission in ("read", "write"):
                with patch.object(AUTH, "app_jwt", return_value="fixture_jwt"), patch.object(AUTH, "github_request", side_effect=[{"id": 42}, {"token": "ghs_fixture_token"}]) as api:
                    self.assertEqual(AUTH.installation_token(repository, permission), "ghs_fixture_token")
                    self.assertEqual(api.call_args_list[0].args, ("fixture_jwt", "GET", f"/repos/{repository}/installation"))
                    self.assertEqual(api.call_args_list[1].args, ("fixture_jwt", "POST", "/app/installations/42/access_tokens", {"repositories": ["edu-agent"], "permissions": {"contents": permission}}))

    def test_invalid_repository_and_private_response_errors_never_escape(self):
        with patch.object(AUTH, "app_jwt") as jwt:
            with self.assertRaises(AUTH.ReleaseAuthError):
                AUTH.installation_token("untrusted/source")
            jwt.assert_not_called()
        with patch.object(AUTH, "app_jwt", return_value="fixture_jwt"), patch.object(AUTH, "github_request", side_effect=RuntimeError(SENTINEL)):
            with self.assertRaises(AUTH.ReleaseAuthError) as failure:
                AUTH.installation_token("shukezhinao/edu-agent")
            self.assertEqual(str(failure.exception), "Installation authentication failed")

    def test_long_opaque_installation_token_with_dots_and_hyphens_is_supported(self):
        # Installation tokens are opaque; do not assume a prefix or fixed length.
        opaque = "x" * 128 + "." + "y" * 129 + "-" + "z" * 131
        self.assertEqual(len(opaque), 390)
        with patch.object(AUTH, "app_jwt", return_value="fixture_jwt"), patch.object(AUTH, "github_request", side_effect=[{"id": 42}, {"token": opaque}]):
            token = AUTH.installation_token("shukezhinao/edu-agent", "read")
        self.assertEqual(token, opaque)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(AUTH, "build_opener", return_value=opener):
            self.assertEqual(AUTH.github_request(token, "GET", "/fixture"), {})
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer " + opaque)

    def test_both_token_boundaries_reject_whitespace_and_header_injection(self):
        for token in ("opaque.with-hyphen\n", "opaque\rInjected: header", "opaque token", "\topaque", ""):
            with patch.object(AUTH, "app_jwt", return_value="fixture_jwt"), patch.object(AUTH, "github_request", side_effect=[{"id": 42}, {"token": token}]):
                with self.assertRaises(AUTH.ReleaseAuthError):
                    AUTH.installation_token("shukezhinao/edu-agent", "read")
            with patch.object(AUTH, "build_opener") as factory:
                with self.assertRaises(AUTH.ReleaseAuthError):
                    AUTH.github_request(token, "GET", "/fixture")
                factory.assert_not_called()

    def test_refresh_cli_outputs_only_token_and_silent_failure(self):
        for failure in (False, True):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(AUTH, "installation_token", side_effect=RuntimeError(SENTINEL) if failure else None, return_value="ghs_new_token") as token, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(AUTH.main(["--token-for", "shukezhinao/edu-agent"]), 1 if failure else 0)
            token.assert_called_once_with("shukezhinao/edu-agent", "write")
            self.assertEqual(stdout.getvalue(), "" if failure else "ghs_new_token")
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
