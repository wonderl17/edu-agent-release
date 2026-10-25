"""Shared, strict identity contract for CNB-built Tencent release manifests."""
import json
import re

REPO = "shukezhinao/edu-agent-cloud-build"
REGISTRY = "docker.cnb.cool/" + REPO
SOURCE = "shukezhinao/edu-agent"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_manifest(value, sha, version, snapshot=None, request_id=None):
    fields = {"schemaVersion", "deployment", "sourceRepository", "sourceSha", "version",
              "sourceTree", "snapshotSha", "requestId", "images", "timings"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("manifest fields")
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or
            value["deployment"] != "teaching-production" or value["sourceRepository"] != SOURCE or
            value["sourceSha"] != sha or not SHA.fullmatch(sha) or value["version"] != version or
            not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) or
            not SHA.fullmatch(value["snapshotSha"]) or not SHA.fullmatch(value["sourceTree"]) or
            not re.fullmatch(r"[0-9a-f]{32}", value["requestId"]) or
            (snapshot is not None and value["snapshotSha"] != snapshot) or
            (request_id is not None and value["requestId"] != request_id)):
        raise ValueError("manifest identity")
    if not isinstance(value["images"], dict) or set(value["images"]) != {"api", "worker", "web"}:
        raise ValueError("image set")
    for kind, item in value["images"].items():
        if (not isinstance(item, dict) or set(item) != {"ref", "id"} or
                not isinstance(item["ref"], str) or not isinstance(item["id"], str) or
                not DIGEST.fullmatch(item["id"]) or
                not re.fullmatch(re.escape(REGISTRY + "/" + kind + "@") + r"sha256:[0-9a-f]{64}", item["ref"])):
            raise ValueError("immutable image reference")
    if (not isinstance(value["timings"], dict) or set(value["timings"]) != {"api", "worker", "web", "total"} or
            any(type(v) not in (float, int) or not 0 <= v <= 10800 for v in value["timings"].values())):
        raise ValueError("build timings")
    return value
