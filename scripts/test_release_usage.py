import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cnb_controller as controller
import release_usage as usage
from test_cnb_release import fixture


def state(status="success", **metrics):
    return {"status": status, "pipelinesStatus": {"PRIVATE_PIPELINE_ID": {
        "name": "PRIVATE_SOURCE_SENTINEL", "status": status,
        "metricCoreHours": 0.21, "metricDuration": 93807,
        "duration": 102344, "labels": [{"key": "cpus", "value": [8]}], **metrics}}}


class UsageTests(unittest.TestCase):
    def test_actual_billing_sums_every_pipeline_including_failed_ones(self):
        value = state()
        value["pipelinesStatus"]["second"] = state("error", metricCoreHours=0.5)["pipelinesStatus"]["PRIVATE_PIPELINE_ID"]
        result = usage.from_status(value)
        self.assertEqual(result, {"basis": "reported", "coreHours": 0.71, "billingSeconds": 187.614, "pipelines": 2})
        summary, line = usage.render(result, "deploy", "0.0.126", "failed")
        self.assertIn("0.7100 核时", summary)
        self.assertIn("0.4438%", summary)
        self.assertNotIn("PRIVATE_", summary + line)

    def test_estimate_uses_billing_duration_not_job_or_controller_duration(self):
        value = state(metricDuration=600000, metricCoreHours=None, duration=99999999)
        result = usage.from_status(value)
        self.assertEqual(result["basis"], "estimated")
        self.assertAlmostEqual(result["coreHours"], 8 / 6)
        self.assertEqual(result["billingSeconds"], 600)
        self.assertIn("估算", usage.render(result, "stage", "1.2.3", "success")[0])

    def test_unavailable_partial_or_malformed_metrics_never_become_zero(self):
        values = [None, {}, state("running"), {"status": []},
                  {"status": "success", "pipelinesStatus": {}},
                  state(metricCoreHours=-1), state(metricCoreHours=True),
                  state(metricCoreHours=float("nan")), state(metricCoreHours=float("inf")),
                  state(metricCoreHours="0.2"), state(metricCoreHours=None, metricDuration=None),
                  state(metricCoreHours=None, labels=[{"key": "cpus", "value": [True]}])]
        partial = state()
        partial["pipelinesStatus"]["second"] = {"status": "running"}
        values.append(partial)
        for value in values:
            with self.subTest(value=value):
                result = usage.from_status(value)
                self.assertEqual(result["basis"], "pending")
                self.assertIsNone(result["coreHours"])

    def test_zero_is_only_reported_when_explicit_or_no_build_was_started(self):
        self.assertEqual(usage.from_status(state(metricCoreHours=0, metricDuration=0))["coreHours"], 0)
        for operation in ("verify", "resume", "desktop"):
            self.assertIn("0.0000 核时", usage.render(usage.not_used(), operation, None, "success")[0])
        self.assertIn("待补录", usage.render(usage.pending(), "deploy", None, "cancelled")[0])

    def test_summary_is_bounded_and_rejects_untrusted_context(self):
        for version in ("PRIVATE_SECRET", "1.2.3\n::warning::secret", "1.2.3<script>"):
            with self.assertRaises(ValueError):
                usage.render(usage.not_used(), "deploy", version, "success")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            summary = root / "summary"
            environment = {"RUNNER_TEMP": str(root), "GITHUB_STEP_SUMMARY": str(summary)}
            messages = []
            usage.report(usage.from_status(state()), environment, "deploy", "1.2.3", "success", messages.append)
            self.assertIn("0.2100 核时", summary.read_text())
            self.assertNotIn("PRIVATE_", summary.read_text() + str(messages))
            untouched = root / "untouched"
            untouched.write_text("original")
            summary.unlink()
            summary.symlink_to(untouched)
            usage.report(usage.not_used(), environment, "resume", None, "failed", messages.append)
            self.assertEqual(untouched.read_text(), "original")
            with patch.object(usage.os, "open", side_effect=OSError("PRIVATE_SECRET")):
                usage.report(usage.not_used(), environment, "desktop", None, "success", messages.append)
            self.assertNotIn("PRIVATE_SECRET", str(messages))


class BuildAccountingTests(unittest.TestCase):
    def invoke(self, initial, final=None, interrupt=False, lost_trigger=False):
        calls, messages, result = [], [], usage.not_used()
        def api(token, method, path, value=None, **kwargs):
            calls.append((method, path, value, kwargs))
            if path == "":
                return {"visibility_level": "Private", "path": controller.REPO}
            if path.endswith("/start"):
                if lost_trigger:
                    raise TimeoutError("original trigger uncertainty")
                return {"success": True, "sn": "fixture-build"}
            if "/status/" in path:
                if kwargs:
                    if isinstance(final, Exception):
                        raise final
                    return final or initial
                return initial
            if "/commit-annotations/" in path:
                return [{"key": "tencent-" + "d" * 32, "value": json.dumps(fixture())}]
            return {}
        error = None
        with tempfile.TemporaryDirectory() as directory, patch.object(controller, "api", side_effect=api), \
                patch.object(controller, "mirror", return_value=("c" * 40, "b" * 40)), \
                patch.object(controller.uuid, "uuid4") as uuid, \
                patch.object(controller.time, "sleep", side_effect=KeyboardInterrupt if interrupt else None):
            uuid.return_value.hex = "d" * 32
            try:
                controller.build(None, "a" * 40, "0.0.126", Path(directory), {}, None, "SECRET", None,
                                 messages.append, usage=result, run_id="12345", run_attempt="2")
            except BaseException as failure:
                error = failure
        self.assertNotIn("SECRET", str(messages))
        return result, calls, error

    def test_success_and_failure_both_record_actual_cost_without_extra_poll(self):
        for status in ("success", "error"):
            result, calls, error = self.invoke(state(status))
            self.assertEqual(result["coreHours"], 0.21)
            self.assertEqual(result["basis"], "reported")
            self.assertEqual(len([c for c in calls if "/status/" in c[1]]), 1)
            self.assertEqual(next(c[2]["title"] for c in calls if c[1].endswith("/start")),
                             "Tencent Production 0.0.126 [GitHub 12345/2]")
            self.assertEqual(error is None, status == "success")

    def test_cancellation_stops_first_then_reads_accounting_once(self):
        result, calls, error = self.invoke(state("running"), state("cancel", metricCoreHours=0.1), interrupt=True)
        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertEqual(result["coreHours"], 0.1)
        self.assertEqual(calls[-2][0:2], ("POST", "/-/build/stop/fixture-build"))
        self.assertEqual(calls[-1][3], {"timeout": 10})

    def test_accounting_error_preserves_release_failure_and_marks_pending(self):
        result, calls, error = self.invoke(state("error", metricCoreHours=None, metricDuration=None), RuntimeError("SECRET"))
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), "CNB build failed")
        self.assertEqual(result["basis"], "pending")

    def test_uncertain_trigger_is_not_reported_as_zero_usage(self):
        result, calls, error = self.invoke(None, lost_trigger=True)
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(result["basis"], "pending")
