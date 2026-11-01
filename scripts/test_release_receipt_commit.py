"""Crash/write fault injection: never erase the warning before a durable receipt."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


class FailureReceiptCommitTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "server/tencent_receiver.py"
        spec = importlib.util.spec_from_file_location("receipt_commit_receiver", source)
        self.r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.r)
        self.r.STATE = Path("/isolated/state")
        self.target = Path("/isolated/package")
        self.private_run = Path("/isolated/run")

    def test_successful_rollback_releases_guard_only_after_all_receipts_are_durable(self):
        events = []
        with patch.object(self.r, "atomic", side_effect=lambda path, value: events.append(("write", path))), \
                patch.object(self.r, "sync_directory", side_effect=lambda path: events.append(("durable", path))), \
                patch.object(self.r, "release_operation_clear", side_effect=lambda operation: events.append(("clear", operation))):
            self.r.record_failed_operation("operation", {"configurationRollbackSucceeded": True}, self.target, self.private_run)
        self.assertEqual(events[0], ("write", self.target / "failure-receipt.json"))
        self.assertEqual(events[1], ("durable", self.target))
        self.assertEqual(events[-1], ("clear", "operation"))
        self.assertEqual(len([e for e in events if e[0] == "durable"]), 3)

    def test_receipt_write_or_directory_sync_failure_keeps_the_pending_guard(self):
        for boundary in ("atomic", "sync_directory"):
            for step in range(3):
                with self.subTest(boundary=boundary, step=step), patch.object(self.r, "atomic"), \
                        patch.object(self.r, "sync_directory"), patch.object(self.r, "release_operation_clear") as clear:
                    with patch.object(self.r, boundary, side_effect=[None] * step + [OSError("injected persistence failure")]):
                        with self.assertRaises(OSError):
                            self.r.record_failed_operation("operation", {"configurationRollbackSucceeded": True}, self.target, self.private_run)
                    clear.assert_not_called()

    def test_unconfirmed_rollback_keeps_the_guard_even_after_the_failure_receipt_commits(self):
        with patch.object(self.r, "atomic"), patch.object(self.r, "sync_directory"), \
                patch.object(self.r, "release_operation_clear") as clear:
            self.r.record_failed_operation("operation", {"configurationRollbackSucceeded": False}, self.target, self.private_run)
        clear.assert_not_called()


if __name__ == "__main__":
    unittest.main()
