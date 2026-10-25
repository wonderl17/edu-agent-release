#!/usr/bin/env python3
"""Fixed Tencent Production release. Never authenticates to or invokes GCP."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from release_auth import github_request, installation_token, stop_process
from cnb_controller import build as build_cnb, verify_access as verify_cnb_access
from release_usage import not_used, report as report_usage

SOURCE = "shukezhinao/edu-agent"
PUBLIC = "wonderl17/edu-agent-release"
HOST = "49.233.166.202"
USER = "teaching-production-ci"
SITE = "https://shukezhinao.321weiqi.com"
PROTOCOL = "teaching-production-ci-v1"
TRANSFER_BUCKET = "teaching-production-releases-1331158445"
INITIAL_SOURCE_SHA = "c59e7360dfd52cedbfc625a9a942ba67d64c320b"
INITIAL_VERSION = "0.0.125"
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class Failure(Exception):
    def __init__(self, code):
        self.code = code


def status(message):
    # Callers supply constants; private subprocess output never reaches stdout.
    print(message, flush=True)


def cancel(signum, _frame):
    raise SystemExit(128 + signum)


def configuration(env):
    operation = env.get("TENCENT_OPERATION", "verify")
    if (env.get("PUBLIC_REPOSITORY") != PUBLIC or
            env.get("GITHUB_REF") != "refs/heads/main" or
            operation not in ("verify", "stage", "deploy", "resume", "automatic") or
            not SHA.fullmatch(env.get("GITHUB_SHA", ""))):
        raise Failure(40)
    requested = env.get("TENCENT_SOURCE_SHA", "")
    if requested and not SHA.fullmatch(requested):
        raise Failure(40)
    if operation in ("stage", "deploy", "resume") and env.get("TENCENT_CONFIRM_RELEASE") != "yes":
        raise Failure(40)
    if operation in ("stage", "deploy", "resume") and (env.get("TENCENT_PRODUCTION_ENABLED") != "true" or
                                                         env.get("RELEASES_ENABLED") != "true"):
        raise Failure(40)
    if operation == "stage" and not requested:
        raise Failure(40)
    previous_ticket = env.get("TENCENT_PREVIOUS_TRANSFER_TICKET", "")
    if operation == "resume":
        if not requested or not re.fullmatch(r"[0-9a-f]{32}", previous_ticket):
            raise Failure(40)
    elif previous_ticket:
        raise Failure(40)
    if operation == "automatic" and requested:
        raise Failure(40)
    return operation, requested


def safe_environment(env, private):
    child = {k: env[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in env}
    child.update({"CI": "true", "GIT_TERMINAL_PROMPT": "0",
                  "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    # Source build scripts cannot write public Actions file commands.
    for key in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"):
        path = private / key.lower()
        path.touch(mode=0o600)
        child[key] = str(path)
    return child


def run(command, child, log, *, cwd=None, capture=False, stdin=None, timeout=5400):
    process = subprocess.Popen(command, cwd=cwd, env=child, stdin=stdin or subprocess.DEVNULL,
                               stdout=subprocess.PIPE if capture else log,
                               stderr=log, start_new_session=True)
    try:
        result, _ = process.communicate(timeout=timeout)
    except BaseException:
        stop_process(process)
        raise
    if process.returncode:
        raise Failure(42)
    return result or b""


def clone_source(sha, token, directory, child, log):
    if not SHA.fullmatch(sha):
        raise Failure(41)
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    git_env = dict(child, GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="core.hooksPath",
                   GIT_CONFIG_VALUE_0="/dev/null", GIT_CONFIG_KEY_1=f"http.https://github.com/{SOURCE}.git.extraheader",
                   GIT_CONFIG_VALUE_1="AUTHORIZATION: basic " + auth)
    for command in (["git", "init", "--quiet"],
                    ["git", "remote", "add", "origin", f"https://github.com/{SOURCE}.git"],
                    ["git", "fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin", "main"],
                    ["git", "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main"],
                    ["git", "checkout", "--quiet", "--detach", sha]):
        run(command, git_env, log, cwd=directory)


def version_tuple(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise Failure(41)
    return tuple(map(int, value.split(".")))


def should_deploy(operation, version, target, baseline, source_sha=None):
    if source_sha is not None and source_sha == target.get("currentSourceSha") and version == target["version"]:
        return False
    current = version_tuple(version)
    threshold = max(version_tuple(baseline), version_tuple(target["version"]),
                    version_tuple(target.get("lastAttemptedVersion") or target["version"]))
    return operation in ("deploy", "resume") or (operation == "automatic" and current > threshold)


def initial_activation(operation, requested, target):
    """Only a root-issued one-shot gate permits manual initial original-package resume."""
    return (operation == "resume" and requested == INITIAL_SOURCE_SHA and
            target.get("status") == "verified" and target.get("configValid") is True and
            target.get("bootstrapReady") is True and target.get("healthy") is False and
            target.get("phase") == "bootstrap" and "currentSourceSha" in target and target["currentSourceSha"] is None and
            target.get("version") == INITIAL_VERSION and target.get("baselineSourceSha") == requested and
            target.get("initialActivationReady") is True and
            isinstance(target.get("initialGateId"), str) and
            re.fullmatch(r"[0-9a-f]{32}", target["initialGateId"]) is not None and
            type(target.get("initialBundleBytes")) is int and 0 < target["initialBundleBytes"] <= 5 * 1024**3 and
            isinstance(target.get("initialBundleSha256"), str) and
            re.fullmatch(r"[0-9a-f]{64}", target["initialBundleSha256"]) is not None)


def release_request(operation, sha, version, target):
    if version_tuple(version) < version_tuple(target["version"]):
        raise Failure(41)
    request = {"sourceSha": sha, "version": version}
    if (operation in ("stage", "deploy", "resume") and target.get("currentSourceSha") is not None and version == target["version"] and
            sha != target["currentSourceSha"]):
        request.update(expectedCurrentSourceSha=target["currentSourceSha"],
                       allowSameVersionSourceChange=True)
    return request


def private_file(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def ssh_configuration(env, private):
    key = env.get("TENCENT_PRODUCTION_DEPLOY_SSH_KEY", "")
    hosts = env.get("TENCENT_PRODUCTION_SSH_KNOWN_HOSTS", "")
    if "OPENSSH PRIVATE KEY" not in key or not hosts.strip():
        raise Failure(40)
    for line in hosts.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] != HOST or fields[1] not in ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"):
            raise Failure(40)
    keypath, known = private / "deploy.key", private / "known_hosts"
    private_file(keypath, (key.rstrip() + "\n").encode())
    private_file(known, (hosts.strip() + "\n").encode())
    return ["ssh", "-F", "/dev/null", "-T", "-i", str(keypath), "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o",
            "UserKnownHostsFile=" + str(known), "-o", "ConnectTimeout=20", f"{USER}@{HOST}"]


def read_with_open_input(process, timeout=5400):
    """Keep stdin alive; its EOF is the remote receiver's cancellation signal."""
    output = bytearray()
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Failure(44)
            for key, _ in selector.select(min(remaining, 5)):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > 128 * 1024:
                    raise Failure(44)
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    return bytes(output)


def remote(ssh, child, log, request, payload=None):
    request = dict(request)
    keep_open = request.get("action") in ("deploy", "stage")
    if keep_open:
        request["clientKeepsInputOpen"] = True
    header = json.dumps(dict(request, protocol=PROTOCOL, sourceRepository=SOURCE), separators=(",", ":")).encode() + b"\n"
    # Prefix and file are streamed through a pipe, never assembled in RAM or a
    # command line. A broken receiver stops the sender and the whole request.
    log_start = log.tell()
    process = subprocess.Popen(ssh, env=child, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=log, start_new_session=True)
    try:
        process.stdin.write(header)
        if payload is not None:
            with payload.open("rb") as handle:
                shutil.copyfileobj(handle, process.stdin, length=1024 * 1024)
        process.stdin.flush()
        if keep_open:
            output = read_with_open_input(process)
            process.stdin.close()
        else:
            process.stdin.close()
            process.stdin = None
            output, _ = process.communicate(timeout=5400)
        if process.returncode or len(output) > 128 * 1024:
            if process.returncode:
                log.flush()
                end = log.seek(0, os.SEEK_END)
                log.seek(max(log_start, end - 128 * 1024))
                diagnostic = log.read().lower()
                log.seek(0, os.SEEK_END)
                for pattern, category in ((b"error in libcrypto", "key-format"),
                                          (b"host key verification failed", "host-pin"),
                                          (b"permission denied", "authentication"),
                                          (b"connection timed out", "connection-timeout"),
                                          (b"connection refused", "connection-refused"),
                                          (b"network is unreachable", "network-unreachable")):
                    if pattern in diagnostic:
                        status("Tencent Production: SSH failed (" + category + ").")
                        break
            raise Failure(44)
        result = json.loads(output)
        if not isinstance(result, dict):
            raise Failure(44)
        return result
    except BaseException:
        stop_process(process)
        raise


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(part)
    return {"bytes": path.stat().st_size, "sha256": result.hexdigest()}


def upload_bundle(ticket, bundle, sha, metadata):
    """One regional private COS PUT; no acceleration or cloud key in this job."""
    identity = ticket.get("transferTicket")
    if (ticket.get("status") != "transfer_prepared" or not isinstance(identity, str) or
            not re.fullmatch(r"[0-9a-f]{32}", identity) or metadata["bytes"] > 5 * 1024**3):
        raise Failure(47)
    url = ticket.get("uploadUrl", "")
    parsed = urlsplit(url)
    host = TRANSFER_BUCKET + ".cos.ap-beijing.myqcloud.com"
    if (parsed.scheme != "https" or parsed.hostname != host or parsed.port is not None or
            parsed.username or parsed.password or parsed.fragment or not parsed.query or
            parsed.path != f"/teaching-production/releases/{sha}/{identity}.tar"):
        raise Failure(47)
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(metadata["bytes"])}
    if ticket.get("uploadHeaders") != headers:
        raise Failure(47)
    deadline = time.monotonic() + 3600
    with bundle.open("rb") as handle:
        class Body:
            def read(self, size=-1):
                if time.monotonic() > deadline:
                    raise Failure(47)
                return handle.read(min(size, 1024**2) if size >= 0 else 1024**2)
        try:
            # Never log a presigned URL, response body, headers or exception text.
            request = Request(url, data=Body(), headers=headers, method="PUT")
            with urlopen(request, timeout=120) as response:
                if response.status != 200:
                    raise Failure(47)
                response.read(4096)
        except Exception:
            raise Failure(47)
    return identity


def archive_metadata(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = int(info.mtime)
    info.pax_headers = {}
    return info


def prepare_build_disk(private, child, log):
    required = 32 * 1024**3
    if shutil.disk_usage(private).free < required:
        status("Tencent Production: preparing runner disk space.")
        # This dedicated hosted job does not use either SDK. Never prune Docker
        # objects or traverse source/credential directories to reclaim space.
        run(["sudo", "rm", "-rf", "/usr/local/lib/android", "/usr/share/dotnet"],
            child, log, timeout=300)
    if shutil.disk_usage(private).free < required:
        raise Failure(45)


def saved_config_ids(path, images, sha):
    """Use Docker-save config hashes, independent of the daemon's ID type."""
    expected = {images[kind]["tag"]: kind for kind in ("api", "worker")}
    result = {}
    with tarfile.open(path, "r:") as archive:
        def metadata(name, limit):
            if not isinstance(name, str) or name.startswith("/") or ".." in name.split("/"):
                raise Failure(43)
            member = archive.getmember(name)
            if not member.isfile() or not 0 < member.size <= limit:
                raise Failure(43)
            return archive.extractfile(member).read(limit + 1)
        entries = json.loads(metadata("manifest.json", 128 * 1024))
        if not isinstance(entries, list) or len(entries) != 2:
            raise Failure(43)
        for entry in entries:
            tags = entry.get("RepoTags")
            if not isinstance(tags, list) or len(tags) != 1 or tags[0] not in expected or tags[0] in result:
                raise Failure(43)
            raw = metadata(entry["Config"], 1024**2)
            config = json.loads(raw)
            if (config.get("os") != "linux" or config.get("architecture") != "amd64" or
                    config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != sha):
                raise Failure(43)
            result[tags[0]] = "sha256:" + hashlib.sha256(raw).hexdigest()
    if set(result) != set(expected):
        raise Failure(43)
    return {kind: dict(images[kind], id=result[images[kind]["tag"]]) for kind in ("api", "worker")}


def build_bundle(source, sha, version, private, child, log):
    prepare_build_disk(private, child, log)
    status("Tencent Production: checking changelog.")
    # Dependency hooks and private scripts execute behind a mount/PID boundary.
    # An env whitelist alone cannot protect sibling key files from the same UID.
    run(["docker", "run", "--rm", "--pids-limit", "512", "--memory", "4g", "--cpus", "2",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         "--user", f"{os.getuid()}:{os.getgid()}",
         "--mount", f"type=bind,source={source},destination=/source,readonly",
         "--env", "HOME=/tmp/tencent-home", "--env", "npm_config_prefix=/tmp/tencent-pnpm",
         "--workdir", "/tmp", "node:22-bookworm-slim", "sh", "-ec",
         "mkdir /tmp/tencent-home /tmp/app\n"
         "cp -R /source/. /tmp/app/\n"
         "cd /tmp/app\n"
         "export PATH=/tmp/tencent-pnpm/bin:$PATH\n"
         "npm install --global pnpm@12.8.1\n"
         "pnpm install --frozen-lockfile --filter teaching-ai-agent --filter @teaching-ai-agent/web...\n"
         "pnpm changelog:check"], child, log)
    images = {}
    for kind in ("api", "worker", "web"):
        status("Tencent Production: building " + kind + ".")
        tag = f"teaching-ai-production/{kind}:{sha}"
        command = ["docker", "build", "--platform", "linux/amd64", "--provenance=false", "--sbom=false",
                   "--label", "org.opencontainers.image.revision=" + sha,
                   "--file", "Dockerfile." + kind, "--tag", tag]
        if kind == "web":
            command += ["--build-arg", "VITE_API_BASE_URL=/api", "--build-arg", "VITE_DEPLOY_ENV=shukezhinao-prod",
                        "--build-arg", "VITE_BUILD_SHA=" + sha]
        run(command + ["."], child, log, cwd=source)
        identity = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], child, log, capture=True).decode().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise Failure(42)
        images[kind] = {"tag": tag, "id": identity}
    directory = private / "bundle"
    directory.mkdir(mode=0o700)
    raw_images = private / "images.tar"
    run(["docker", "save", "--output", str(raw_images), images["api"]["tag"], images["worker"]["tag"]], child, log)
    canonical_images = saved_config_ids(raw_images, images, sha)
    run(["zstd", "-T2", "-3", "--rm", str(raw_images), "-o", str(directory / "images.tar.zst")], child, log)
    container = run(["docker", "create", images["web"]["tag"]], child, log, capture=True).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise Failure(42)
    web = private / "web-dist"
    try:
        run(["docker", "cp", container + ":/usr/share/nginx/html", str(web)], child, log)
    finally:
        run(["docker", "rm", container], child, log)
    build = json.loads((web / "version.json").read_text())
    if build.get("version") != version or build.get("buildSha") != sha:
        raise Failure(43)
    for path in web.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise Failure(43)
    with tarfile.open(directory / "web.tar.gz", "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(web.rglob("*")):
            archive.add(path, arcname=str(path.relative_to(web)), recursive=False, filter=archive_metadata)
    manifest = {"schemaVersion": 1, "deployment": "teaching-production", "sourceRepository": SOURCE,
                "sourceSha": sha, "version": version,
                "images": canonical_images,
                "files": {name: digest(directory / name) for name in ("images.tar.zst", "web.tar.gz")}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    bundle = private / "release-bundle.tar"
    with tarfile.open(bundle, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name in ("manifest.json", "images.tar.zst", "web.tar.gz"):
            archive.add(directory / name, arcname=name, filter=archive_metadata)
    return bundle


def execute(env):
    operation, requested = configuration(env)
    if operation == "automatic" and (env.get("TENCENT_PRODUCTION_AUTOMATIC_ENABLED") != "true" or
                                      env.get("TENCENT_PRODUCTION_ENABLED") != "true"):
        status("Tencent Production: automatic deployment disabled.")
        return
    token = None
    deployment_id = None
    deployed = False
    usage, report_version, outcome = not_used(), None, "failed"
    previous = os.umask(0o077)
    try:
        runner = Path(env["RUNNER_TEMP"]).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="tencent-production-", dir=runner) as directory:
            private = Path(directory)
            private.chmod(0o700)
            child = safe_environment(env, private)
            ssh = ssh_configuration(env, private)
            source = private / "source"
            source.mkdir(mode=0o700)
            with (private / "private.log").open("w+b", buffering=0) as log:
                sha, version = requested or env["GITHUB_SHA"], "0.0.125"
                try:
                    status("Tencent Production: checking target.")
                    target = remote(ssh, child, log, {"action": "verify", "sourceSha": sha, "version": version})
                    initial = initial_activation(operation, requested, target)
                    bootstrap = (target.get("bootstrapReady") is True and target.get("healthy") is False and
                                 target.get("phase") == "bootstrap" and target.get("currentSourceSha") is None)
                    if (target.get("status") != "verified" or target.get("configValid") is not True or
                            (target.get("healthy") is not True and not (operation == "stage" and bootstrap) and not initial)):
                        raise Failure(44)
                    status("Tencent Production: checking trusted private source.")
                    token = installation_token(SOURCE, permission="read")
                    sha = requested or github_request(token, "GET", f"/repos/{SOURCE}/commits/main")["sha"]
                    if not SHA.fullmatch(sha):
                        raise Failure(41)
                    clone_source(sha, token, source, child, log)
                    version = json.loads((source / "package.json").read_text())["version"]
                    version_tuple(version)
                    report_version = version
                    if operation == "verify":
                        if (env.get("TENCENT_BUILD_BACKEND") or "cnb") == "cnb":
                            status("Tencent Production: checking private CNB access.")
                            verify_cnb_access(env.get("TENCENT_CNB_TOKEN", ""))
                        status("Tencent Production: verification complete; no deployment.")
                        outcome = "success"
                        return
                    if initial and (sha != INITIAL_SOURCE_SHA or version != INITIAL_VERSION):
                        raise Failure(41)
                    if operation != "stage" and not initial and not should_deploy(operation, version, target, env.get("TENCENT_PRODUCTION_BASELINE_VERSION", "0.0.125"), sha):
                        status("Tencent Production: no release required.")
                        outcome = "success"
                        return
                    base = {"sourceSha": sha, "version": version, "initialGateId": target["initialGateId"]} if initial else release_request(operation, sha, version, target)
                    # The initial target has no current Web to record against.
                    # GitHub still records release-cloud, while the root-issued
                    # gate owns its irreversible attempt_started/consumed state.
                    if operation != "stage" and not initial:
                        result = remote(ssh, child, log, dict(base, action="record-attempt"))
                        if result.get("status") != "attempt_recorded":
                            raise Failure(44)
                    deployment_id = github_request(env["PUBLIC_GH_TOKEN"], "POST", f"/repos/{PUBLIC}/deployments", {
                        "ref": env["GITHUB_SHA"], "task": "stage-tencent-production" if operation == "stage" else "release-cloud", "auto_merge": False,
                        "required_contexts": [], "environment": "production-staging" if operation == "stage" else "production-cloud",
                        "production_environment": operation != "stage",
                        "payload": {"source_repository": SOURCE, "source_sha": sha, "version": version,
                                    "operation": "tencent-production-stage" if operation == "stage" else "cloud"},
                    })["id"]
                    if operation == "resume":
                        status("Tencent Production: recovering original private release package.")
                        ticket = remote(ssh, child, log, dict(base, action="resume-transfer",
                            previousTransferTicket=env["TENCENT_PREVIOUS_TRANSFER_TICKET"]))
                        if (ticket.get("status") != "transfer_resumed" or
                                not re.fullmatch(r"[0-9a-f]{32}", ticket.get("transferTicket", "")) or
                                type(ticket.get("bundleBytes")) is not int or
                                not 0 < ticket["bundleBytes"] <= 5 * 1024**3 or
                                not re.fullmatch(r"[0-9a-f]{64}", ticket.get("bundleSha256", ""))):
                            raise Failure(47)
                        metadata = {"bytes": ticket["bundleBytes"], "sha256": ticket["bundleSha256"]}
                        if initial and (metadata["bytes"] != target["initialBundleBytes"] or
                                        metadata["sha256"] != target["initialBundleSha256"]):
                            raise Failure(47)
                        identity = ticket["transferTicket"]
                    else:
                        backend = env.get("TENCENT_BUILD_BACKEND") or "cnb"
                        if backend == "cnb":
                            bundle = build_cnb(source, sha, version, private, child, log,
                                               env.get("TENCENT_CNB_TOKEN", ""), run, status, usage=usage,
                                               run_id=env.get("GITHUB_RUN_ID"), run_attempt=env.get("GITHUB_RUN_ATTEMPT"))
                            metadata = digest(bundle)
                            ticket = remote(ssh, child, log, dict(base, action="prepare-registry",
                                bundleBytes=metadata["bytes"], bundleSha256=metadata["sha256"],
                                registryManifest=json.loads(bundle.read_text())))
                            if (ticket.get("status") != "transfer_prepared" or ticket.get("transport") != "registry" or
                                    not re.fullmatch(r"[0-9a-f]{32}", ticket.get("transferTicket", "")) or
                                    ticket.get("bundleBytes") != metadata["bytes"] or
                                    ticket.get("bundleSha256") != metadata["sha256"]):
                                raise Failure(47)
                            identity = ticket["transferTicket"]
                        elif backend == "cos":
                            bundle = build_bundle(source, sha, version, private, child, log)
                            metadata = digest(bundle)
                            status("Tencent Production: preparing private transfer.")
                            ticket = remote(ssh, child, log, dict(base, action="prepare-transfer",
                                                                bundleBytes=metadata["bytes"], bundleSha256=metadata["sha256"]))
                            status("Tencent Production: uploading private release package.")
                            identity = upload_bundle(ticket, bundle, sha, metadata)
                        else:
                            raise Failure(40)
                    status("Tencent Production: staging private package." if operation == "stage" else
                           "Tencent Production: applying and verifying release.")
                    result = remote(ssh, child, log, dict(base, action="stage" if operation == "stage" else "deploy",
                                                        bundleBytes=metadata["bytes"], bundleSha256=metadata["sha256"],
                                                        transferTicket=identity))
                    if (result.get("status") != ("staged" if operation == "stage" else "deployed") or
                            result.get("version") != version or result.get("sourceSha") != sha):
                        raise Failure(44)
                    if initial and (result.get("initialActivation") is not True or
                                    result.get("gateId") != target["initialGateId"] or
                                    result.get("verification", {}).get("healthy") is not True):
                        raise Failure(44)
                    if operation == "stage" and (result.get("transferTicket") != identity or
                            result.get("bundleBytes") != metadata["bytes"] or
                            result.get("bundleSha256") != metadata["sha256"]):
                        raise Failure(44)
                    if "timings" in result:
                        timings = result["timings"]
                        for category in ("buildSeconds", "pullSeconds"):
                            values = timings.get(category, {})
                            if all(type(values.get(kind)) in (int, float) and 0 <= values[kind] <= 10800
                                   for kind in ("api", "worker", "web")):
                                status("Tencent Production: " + category + " " + ", ".join(
                                    kind + "=" + str(round(values[kind], 1)) for kind in ("api", "worker", "web")))
                    deployed = True
                    try:
                        github_request(env["PUBLIC_GH_TOKEN"], "POST", f"/repos/{PUBLIC}/deployments/{deployment_id}/statuses", {
                            "state": "success", "environment_url": SITE, "auto_inactive": False,
                            "description": "Production package staged; services unchanged." if operation == "stage" else
                                           "Tencent Production target verified.",
                        })
                    except Exception:
                        status("Tencent Production: target verified; deployment metadata update failed.")
                        raise Failure(46)
                    outcome = "success"
                    status("Tencent Production: staging complete; no services changed." if operation == "stage" else
                           "Tencent Production: deployment and verification complete.")
                except BaseException as error:
                    if isinstance(error, (SystemExit, KeyboardInterrupt)):
                        outcome = "cancelled"
                    if operation != "verify" and not isinstance(error, (SystemExit, KeyboardInterrupt)):
                        try:
                            log.flush()
                            log.seek(max(0, log.seek(0, os.SEEK_END) - 128 * 1024))
                            diagnostic = private / "diagnostic.log"
                            private_file(diagnostic, log.read(128 * 1024))
                            data = digest(diagnostic)
                            remote(ssh, child, log, {"action": "diagnostics", "sourceSha": sha, "version": version,
                                                    "bundleBytes": data["bytes"], "bundleSha256": data["sha256"],
                                                    "runId": env.get("GITHUB_RUN_ID", "")}, diagnostic)
                        except Exception:
                            pass
                    raise
    finally:
        report_usage(usage, env, operation, report_version, outcome, status)
        if deployment_id and not deployed:
            try:
                github_request(env["PUBLIC_GH_TOKEN"], "POST", f"/repos/{PUBLIC}/deployments/{deployment_id}/statuses", {
                    "state": "error", "auto_inactive": False,
                    "description": "Tencent Production attempt failed; manual retry required.",
                })
            except Exception:
                pass
        if token:
            try:
                github_request(token, "DELETE", "/installation/token")
            except Exception:
                pass
        os.umask(previous)


def main():
    try:
        execute(dict(os.environ))
        return 0
    except Failure as error:
        status(f"Tencent Production failed (code={error.code}).")
        return error.code
    except SystemExit:
        status("Tencent Production cancelled.")
        raise
    except BaseException:
        status("Tencent Production failed (code=49).")
        return 49


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    raise SystemExit(main())
