"""Controller observation fault injection, without network or real publication."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import tencent_production as target


class ControllerReconnectTests(unittest.TestCase):
    def setUp(self):
        self.base = {"sourceSha": "a" * 40, "version": "0.0.134"}
        self.metadata = {"bytes": 7, "sha256": "b" * 64}
        self.receipt = dict(self.base, status="release_status", state="deployed", bundleBytes=7,
                            bundleSha256="b" * 64, verification={"healthy": True}, recoveryRequired=False)

    def test_only_readonly_transport_failures_retry_with_a_finite_budget(self):
        for errors, success in (([target.TransportFailure(44), self.receipt], True),
                                ([target.TransportFailure(44)] * 3, False),
                                ([target.Failure(44)], False), ([SystemExit(143)], False)):
            with self.subTest(success=success), patch.object(target, "remote", side_effect=errors) as remote, \
                    patch.object(target.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
                if success:
                    self.assertEqual(target.original_release_status([], {}, None, self.base, metadata=self.metadata), self.receipt)
                else:
                    with self.assertRaises((target.Failure, SystemExit)):
                        target.original_release_status([], {}, None, self.base, metadata=self.metadata)
                self.assertLessEqual(remote.call_count, 3)
                self.assertLessEqual(sleep.call_count, 2)
                self.assertTrue(all(c.args[3]["action"] == "release-status" for c in remote.call_args_list))

    def test_unhealthy_wrong_or_incomplete_package_is_never_accepted(self):
        for fields in ({"verification": {"healthy": False}}, {"verification": "wrong"}, {"sourceSha": "c" * 40},
                       {"version": "0.0.135"}, {"bundleBytes": True}, {"bundleSha256": "d" * 64},
                       {"state": "identity_conflict"}, {"recoveryRequired": None}, {"recoveryRequired": "false"}):
            with self.subTest(fields=fields), patch.object(target, "remote", return_value=dict(self.receipt, **fields)), \
                    self.assertRaises(target.Failure):
                target.original_release_status([], {}, None, self.base, metadata=self.metadata)

    def test_unconfirmed_write_is_never_automatically_replayed(self):
        request = dict(self.base, action="deploy", bundleBytes=7, bundleSha256="b" * 64)
        for state in ("pending", "staged", "failed", "absent"):
            with self.subTest(state=state), patch.object(target, "remote", side_effect=[
                    target.TransportFailure(44), dict(self.receipt, state=state)]) as remote, \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(target.Failure):
                target.apply_original_package([], {}, None, request)
            self.assertEqual([c.args[3]["action"] for c in remote.call_args_list], ["deploy", "release-status"])

    def test_live_cleanup_gets_a_bounded_readonly_observation_window(self):
        pending = dict(self.receipt, state="pending", recoveryRequired=True, ownerActive=True)
        for responses, completed in (([pending, self.receipt], True), ([pending] * 3, False)):
            with self.subTest(completed=completed), patch.object(target, "remote", side_effect=responses) as remote, \
                    patch.object(target.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                if completed:
                    self.assertEqual(target.original_release_status([], {}, None, self.base, metadata=self.metadata)["state"], "deployed")
                else:
                    with self.assertRaises(target.Failure) as failure:
                        target.original_release_status([], {}, None, self.base, metadata=self.metadata)
                    self.assertEqual(failure.exception.code, 48)
                self.assertLessEqual(remote.call_count, 3)
                self.assertTrue(all(c.args[3]["action"] == "release-status" for c in remote.call_args_list))

    def test_malformed_success_is_unknown_not_proof_to_repeat_a_write(self):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.returncode = 0
        for output in (b"", b'{"status":', b"[]"):
            with self.subTest(output=output), patch.object(target.subprocess, "Popen", return_value=Process()), \
                    patch.object(target, "read_with_open_input", return_value=output), \
                    patch.object(target, "stop_process"), self.assertRaises(target.TransportFailure):
                target.remote([], {}, io.BytesIO(), {"action": "deploy"})

    def test_drain_errors_are_visible_but_private_details_are_not(self):
        safe = dict(status="failed", code="DEPLOY_FAILED_ROLLBACK_RESTORED", phase="drain",
                    causeCode="API_DRAIN_CONNECTION_TIMEOUT", detail="PRIVATE_SENTINEL")
        self.assertEqual(target.receiver_failure(json.dumps(safe)), (safe["code"], "drain"))
        self.assertEqual(target.receiver_cause(json.dumps(safe)), "API_DRAIN_CONNECTION_TIMEOUT")
        self.assertIsNone(target.receiver_cause(json.dumps(dict(safe, causeCode="PRIVATE_SENTINEL"))))
        self.assertIsNone(target.receiver_failure(json.dumps(dict(safe, phase="PRIVATE_SENTINEL"))))

    def test_only_explicit_legacy_action_refusal_allows_legacy_resume_preflight(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed), patch.object(target, "remote", side_effect=target.UnsupportedReleaseStatus(44)), \
                    contextlib.redirect_stdout(io.StringIO()):
                if allowed:
                    self.assertIsNone(target.original_release_status([], {}, None, self.base,
                        previous_ticket="d" * 32, allow_legacy=True))
                else:
                    with self.assertRaises(target.UnsupportedReleaseStatus):
                        target.original_release_status([], {}, None, self.base, metadata=self.metadata)
        with patch.object(target, "remote", side_effect=target.Failure(44)), self.assertRaises(target.Failure):
            target.original_release_status([], {}, None, self.base, previous_ticket="d" * 32, allow_legacy=True)

    def test_lost_write_on_legacy_receiver_is_unknown_and_never_replayed(self):
        with patch.object(target, "remote", side_effect=[target.TransportFailure(44), target.UnsupportedReleaseStatus(44)]) as remote, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(target.Failure) as failure:
            target.apply_original_package([], {}, None, dict(self.base, action="deploy", bundleBytes=7, bundleSha256="b" * 64))
        self.assertEqual(failure.exception.code, 48)
        self.assertEqual([c.args[3]["action"] for c in remote.call_args_list], ["deploy", "release-status"])

    def test_legacy_wire_refusal_is_distinct_from_authentication_and_other_refusals(self):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.returncode = 1
            def communicate(self, **kwargs):
                return json.dumps({"status": "failed", "code": "ACTION_REFUSED", "phase": "protocol"}).encode(), None
        with patch.object(target.subprocess, "Popen", return_value=Process()), \
                patch.object(target, "stop_process"), self.assertRaises(target.UnsupportedReleaseStatus):
            target.remote([], {}, io.BytesIO(), dict(self.base, action="release-status"))


if __name__ == "__main__":
    unittest.main()
