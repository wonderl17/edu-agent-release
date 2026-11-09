"""Shared, strict identity contract for CNB-built Tencent release manifests."""
import json
from pathlib import Path
import re
import shutil
import stat

REPO = "shukezhinao/edu-agent-cloud-build"
REGISTRY = "docker.cnb.cool/" + REPO
SOURCE = "shukezhinao/edu-agent"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
RUNTIME_KIND = "codex-runtime"
RUNTIME_ROOT = "tools/codex-agent-runtime/"
RUNTIME_FILES = tuple(RUNTIME_ROOT + name for name in ("release.json", "package.json", "package-lock.json", "Dockerfile"))
RUNTIME_PROTOCOL = "teaching-codex-plan-v1"
RUNTIME_LABELS = {"protocol": "io.shukezhinao.codex.protocol", "sdkVersion": "io.shukezhinao.codex.sdk-version"}
NATIVE_RUNTIME_LABELS = {"protocol": RUNTIME_LABELS["protocol"], "engine": "io.shukezhinao.codex.engine",
                         "runtimeVersion": "io.shukezhinao.codex.runtime-version"}
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_runtime(value):
    if (not isinstance(value, dict) or type(value.get("schemaVersion")) is not int or
            value.get("protocol") != RUNTIME_PROTOCOL):
        raise ValueError("runtime identity")
    if (value["schemaVersion"] == 1 and set(value) == {"schemaVersion", "protocol", "sdkVersion"} and
            isinstance(value["sdkVersion"], str) and SEMVER.fullmatch(value["sdkVersion"])):
        return value
    if (value["schemaVersion"] == 2 and set(value) == {"schemaVersion", "protocol", "engine", "runtimeVersion"} and
            value["engine"] == "responses" and isinstance(value["runtimeVersion"], str) and
            SEMVER.fullmatch(value["runtimeVersion"])):
        return value
    raise ValueError("runtime identity")


def runtime_labels(runtime):
    validate_runtime(runtime)
    keys = RUNTIME_LABELS if runtime["schemaVersion"] == 1 else NATIVE_RUNTIME_LABELS
    return {label: runtime[key] for key, label in keys.items()}


def validate_native_package(runtime, package):
    return (isinstance(package, dict) and package.get("name") == "@teaching-ai-agent/codex-runtime" and
            package.get("version") == runtime["runtimeVersion"] and
            all(package.get(key, {}) == {} for key in ("dependencies", "optionalDependencies", "peerDependencies", "devDependencies")))


def source_runtime(read_file):
    """Explicit opt-in in the exact source tree; old source keeps schema 2."""
    raw = read_file(RUNTIME_FILES[0])
    if raw is None:
        return None
    runtime = validate_runtime(json.loads(raw))
    try:
        package = json.loads(read_file(RUNTIME_FILES[1]))
        lock = json.loads(read_file(RUNTIME_FILES[2]))
        dockerfile = read_file(RUNTIME_FILES[3])
        if runtime["schemaVersion"] == 1:
            version = runtime["sdkVersion"]
            valid = (package["name"] == "@teaching-ai-agent/codex-runtime" and
                     package["dependencies"]["@openai/codex-sdk"] == version and
                     lock["packages"][""]["dependencies"]["@openai/codex-sdk"] == version and
                     lock["packages"]["node_modules/@openai/codex-sdk"]["version"] == version)
        else:
            valid = (validate_native_package(runtime, package) and set(lock["packages"]) == {""} and
                     validate_native_package(runtime, lock["packages"][""]))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid or not dockerfile:
        raise ValueError("runtime source dependency identity")
    return runtime


def filesystem_runtime(source):
    def read(name):
        path = Path(source) / name
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or path.resolve() != Path(source).resolve() / name:
            raise ValueError("runtime source file")
        return path.read_bytes()
    return source_runtime(read)


def inspect_runtime_image(runtime, image_ref, directory, run, remove):
    """Inspect candidate bytes without starting it. Callbacks take Docker subcommands."""
    validate_runtime(runtime)
    container = run(["create", image_ref]).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise ValueError("runtime container identity")
    target = Path(directory).resolve() / "codex-runtime-metadata"
    created = False
    try:
        target.mkdir(mode=0o700)
        created = True
        values = []
        paths = [("release", "/app/release.json"), ("package", "/app/package.json")]
        if runtime["schemaVersion"] == 1:
            paths.append(("sdk", "/app/node_modules/@openai/codex-sdk/package.json"))
        for name, source in paths:
            path = target / (name + ".json")
            run(["cp", container + ":" + source, str(path)])
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.resolve() != path or info.st_size > 16384:
                raise ValueError("runtime image metadata file")
            values.append(json.loads(path.read_bytes()))
        release, package, *installed = values
        if release != runtime:
            raise ValueError("runtime image dependency identity")
        if runtime["schemaVersion"] == 2:
            if not validate_native_package(runtime, package):
                raise ValueError("runtime image dependency identity")
            return dict(protocol=runtime["protocol"], engine=runtime["engine"],
                        runtimeVersion=runtime["runtimeVersion"], runtimeMetadataVerified=True)
        sdk = installed[0]
        if (not isinstance(package, dict) or not isinstance(sdk, dict) or
                package.get("name") != "@teaching-ai-agent/codex-runtime" or
                package.get("dependencies", {}).get("@openai/codex-sdk") != runtime["sdkVersion"] or
                sdk.get("name") != "@openai/codex-sdk" or sdk.get("version") != runtime["sdkVersion"]):
            raise ValueError("runtime image dependency identity")
        return dict(protocol=runtime["protocol"], sdkVersion=runtime["sdkVersion"], installedPackageVerified=True)
    finally:
        try:
            remove(["rm", container])
        finally:
            if created:
                shutil.rmtree(target, ignore_errors=True)


def validate_manifest(value, sha, version, snapshot=None, request_id=None):
    fields = {"schemaVersion", "deployment", "sourceRepository", "sourceSha", "version",
              "sourceTree", "snapshotSha", "requestId", "images", "timings"}
    if not isinstance(value, dict):
        raise ValueError("manifest fields")
    schema = value.get("schemaVersion")
    if schema == 3:
        fields.add("runtime")
    if set(value) != fields:
        raise ValueError("manifest fields")
    if (type(schema) is not int or schema not in (2, 3) or
            value["deployment"] != "teaching-production" or value["sourceRepository"] != SOURCE or
            value["sourceSha"] != sha or not SHA.fullmatch(sha) or value["version"] != version or
            not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) or
            not SHA.fullmatch(value["snapshotSha"]) or not SHA.fullmatch(value["sourceTree"]) or
            not re.fullmatch(r"[0-9a-f]{32}", value["requestId"]) or
            (snapshot is not None and value["snapshotSha"] != snapshot) or
            (request_id is not None and value["requestId"] != request_id)):
        raise ValueError("manifest identity")
    components = {"api", "worker", "web"}
    if schema == 3:
        validate_runtime(value["runtime"])
        components.add(RUNTIME_KIND)
    if not isinstance(value["images"], dict) or set(value["images"]) != components:
        raise ValueError("image set")
    for kind, item in value["images"].items():
        if (not isinstance(item, dict) or set(item) != {"ref", "id"} or
                not isinstance(item["ref"], str) or not isinstance(item["id"], str) or
                not DIGEST.fullmatch(item["id"]) or
                not re.fullmatch(re.escape(REGISTRY + "/" + kind + "@") + r"sha256:[0-9a-f]{64}", item["ref"])):
            raise ValueError("immutable image reference")
    if (not isinstance(value["timings"], dict) or set(value["timings"]) != components | {"total"} or
            any(type(v) not in (float, int) or not 0 <= v <= 10800 for v in value["timings"].values())):
        raise ValueError("build timings")
    return value
