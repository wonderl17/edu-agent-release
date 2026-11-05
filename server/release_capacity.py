"""Read-only capacity checks. No Docker or business content enters the result."""
import os
from pathlib import Path

GIB = 1024 ** 3
IMAGE_RESERVE = 32 * GIB
WEB_RESERVE = 512 * 1024 ** 2
HEADROOM = 2 * GIB


def filesystem_capacity(path):
    path = Path(path)
    value = os.statvfs(path)
    return {"filesystemId": str(path.stat().st_dev),
            "totalBytes": value.f_blocks * value.f_frsize,
            "availableBytes": value.f_bavail * value.f_frsize,
            "totalInodes": value.f_files, "availableInodes": value.f_favail}


def collect_capacity(operations_path, docker_path, bundle_bytes, *, filesystem=filesystem_capacity):
    if type(bundle_bytes) is not int or not 0 <= bundle_bytes <= 8 * GIB:
        raise ValueError("CAPACITY_PACKAGE_SIZE_REFUSED")
    required = IMAGE_RESERVE + WEB_RESERVE + HEADROOM + bundle_bytes
    filesystems = []
    for name, path in (("operations", operations_path), ("docker", docker_path)):
        row = filesystem(path)
        fields = {"filesystemId", "totalBytes", "availableBytes", "totalInodes", "availableInodes"}
        if (not isinstance(row, dict) or set(row) != fields or
                not isinstance(row["filesystemId"], str) or
                any(type(row[k]) is not int or row[k] < 0 for k in fields - {"filesystemId"}) or
                row["availableBytes"] > row["totalBytes"] or row["availableInodes"] > row["totalInodes"]):
            raise ValueError("CAPACITY_FILESYSTEM_INVALID")
        match = next((x for x in filesystems if x["filesystemId"] == row["filesystemId"]), None)
        if match is not None:
            match["roles"].append(name)
            # A busy filesystem can change between probes; retain the conservative observation.
            match["availableBytes"] = min(match["availableBytes"], row["availableBytes"])
            match["availableInodes"] = min(match["availableInodes"], row["availableInodes"])
        else:
            filesystems.append(dict(row, roles=[name]))
    ready = all(x["availableBytes"] >= required and
                (not x["totalInodes"] or x["availableInodes"] >= 10000) for x in filesystems)
    low_inodes = any(x["totalInodes"] and x["availableInodes"] < max(10000, x["totalInodes"] // 20)
                     for x in filesystems)
    minimum = min(x["availableBytes"] for x in filesystems)
    severity = "critical" if not ready or minimum < 45 * GIB else "warning" if minimum < 60 * GIB or low_inodes else "ok"
    return {"schemaVersion": 1, "filesystems": filesystems,
            "availableBytes": minimum, "requiredBytes": required,
            "releaseReady": ready, "severity": severity}
