import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("release_capacity", Path(__file__).resolve().parents[1] / "server/release_capacity.py")
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class CapacityTests(unittest.TestCase):
    def row(self, free=80, device="one", inodes=50000):
        return dict(filesystemId=device, totalBytes=180*capacity.GIB, availableBytes=free*capacity.GIB,
                    totalInodes=100000, availableInodes=inodes)

    def test_separate_docker_disk_is_checked_and_same_disk_is_not_double_counted(self):
        rows = iter([self.row(), self.row(free=20, device="two")])
        value = capacity.collect_capacity("ops", "docker", 12000, filesystem=lambda _: next(rows))
        self.assertFalse(value["releaseReady"])
        self.assertEqual(value["severity"], "critical")
        self.assertEqual(len(value["filesystems"]), 2)
        rows = iter([self.row(), self.row(free=70)])
        value = capacity.collect_capacity("ops", "docker", 12000, filesystem=lambda _: next(rows))
        self.assertEqual(len(value["filesystems"]), 1)
        self.assertEqual(value["filesystems"][0]["roles"], ["operations", "docker"])
        self.assertEqual(value["availableBytes"], 70*capacity.GIB)

    def test_absolute_capacity_and_inodes_both_control_readiness(self):
        for row in (self.row(free=34), self.row(inodes=9999)):
            result = capacity.collect_capacity("ops", "docker", 12000, filesystem=lambda _: row)
            self.assertFalse(result["releaseReady"])
        self.assertTrue(capacity.collect_capacity("ops", "docker", 12000, filesystem=lambda _: self.row())["releaseReady"])

    def test_diagnostics_are_bounded_and_contain_no_paths(self):
        value = capacity.collect_capacity("PRIVATE_OPS", "PRIVATE_DOCKER", 12000, filesystem=lambda _: self.row(free=50))
        self.assertEqual(value["severity"], "warning")
        self.assertNotIn("PRIVATE", str(value))
        for size in (-1, True, 9*capacity.GIB):
            with self.assertRaises(ValueError): capacity.collect_capacity("ops", "docker", size)


if __name__ == "__main__": unittest.main()
