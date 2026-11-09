#!/usr/bin/env python3
"""Run only in the private CNB project; no Production or GitHub credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from cnb_release import (REPO, REGISTRY, SOURCE, SHA, DIGEST, canonical, validate_manifest,
                         RUNTIME_KIND, RUNTIME_ROOT, runtime_labels, filesystem_runtime, inspect_runtime_image)

CODEX_CACHE_KIND = "codex-runtime"
IMAGE_MANIFEST_TYPES = frozenset(("application/vnd.oci.image.manifest.v1+json",
                                 "application/vnd.docker.distribution.manifest.v2+json"))
IMAGE_CONFIG_TYPES = frozenset(("application/vnd.oci.image.config.v1+json",
                               "application/vnd.docker.container.image.v1+json"))


def run(args, **kwargs):
    kwargs.setdefault('timeout', 300)
    return subprocess.check_output(args, **kwargs)


def cache_references(kind, tag="main"):
    # Isolate explicitly requested build experiments from Production cache writes.
    if kind not in ("api", "worker", "web", CODEX_CACHE_KIND) or (tag != "main" and not re.fullmatch(r"benchmark-[0-9a-f]{12}", tag)):
        raise ValueError("cache scope")
    return REGISTRY + "/cache-" + kind + ":" + tag


def stop_builds(active):
    """Cancel every process group, including descendants, before removing logs/config."""
    for process, _start, _log, _metadata in active.values():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while any(item[0].poll() is None for item in active.values()) and time.monotonic() < deadline:
        time.sleep(0.05)
    for process, _start, _log, _metadata in active.values():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


class NoRegistryRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def registry_config_bytes(kind, digest, docker_env):
    """Read one small config blob; credentials only reach the fixed CNB registry."""
    if kind not in ("api", "worker", "web", CODEX_CACHE_KIND) or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError("registry config scope")
    config = json.loads((Path(docker_env["DOCKER_CONFIG"]) / "config.json").read_text())
    auth = config.get("auths", {}).get("docker.cnb.cool", {}).get("auth")
    if not isinstance(auth, str) or not 0 < len(auth) <= 32768:
        raise ValueError("registry config authentication")
    decoded = base64.b64decode(auth, validate=True)
    if b":" not in decoded or any(x in decoded for x in (b"\n", b"\r", b"\0")):
        raise ValueError("registry config authentication")
    opener = build_opener(NoRegistryRedirect())
    def read(request, limit):
        with opener.open(request, timeout=30) as response:
            raw = response.read(limit + 1)
        if not 0 < len(raw) <= limit:
            raise ValueError("registry config response limit")
        return raw
    query = urlencode({"service": "cnb-registry", "scope": "repository:" + REPO + "/" + kind + ":pull"})
    token = json.loads(read(Request("https://docker.cnb.cool/service/token?" + query,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"}), 65536))
    token = token.get("token", token.get("access_token")) if isinstance(token, dict) else None
    if not isinstance(token, str) or not 0 < len(token) <= 65536 or any(x in token for x in ("\n", "\r", "\0")):
        raise ValueError("registry config token")
    return read(Request("https://docker.cnb.cool/v2/" + REPO + "/" + kind + "/blobs/" + digest,
                        headers={"Authorization": "Bearer " + token}), 1024 ** 2)


def image_identity_from_metadata(value, kind, docker_env, identity=None):
    """Resolve missing exporter metadata from the exact pushed image, never a tag/cache."""
    if not isinstance(value, dict) or kind not in ("api", "worker", "web", CODEX_CACHE_KIND):
        raise ValueError("build metadata")
    digest = value.get("containerimage.digest")
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError("build digest")
    descriptor = value.get("containerimage.descriptor")
    if descriptor is not None and (not isinstance(descriptor, dict) or descriptor.get("digest") != digest or
                                  descriptor.get("mediaType") not in IMAGE_MANIFEST_TYPES):
        raise ValueError("build descriptor")
    if descriptor is not None and "platform" in descriptor:
        platform = descriptor["platform"]
        if not isinstance(platform, dict) or platform.get("os") != "linux" or platform.get("architecture") != "amd64":
            raise ValueError("build descriptor platform")
    annotations = descriptor.get("annotations", {}) if descriptor is not None else {}
    if not isinstance(annotations, dict):
        raise ValueError("build descriptor annotations")
    annotated = annotations.get("config.digest")
    if "config.digest" in annotations and (not isinstance(annotated, str) or not DIGEST.fullmatch(annotated)):
        raise ValueError("build descriptor config")
    if "containerimage.config.digest" in value:
        config = value["containerimage.config.digest"]
        if not isinstance(config, str) or not DIGEST.fullmatch(config):
            raise ValueError("build config digest")
        if annotated is not None and annotated != config:
            raise ValueError("build config conflict")
        return digest, config
    # Some completed image+registry-cache exports omit this convenience field.
    # The immutable manifest's SHA-256 and config descriptor remain authoritative.
    reference = REGISTRY + "/" + kind + "@" + digest
    raw = run(["docker", "buildx", "imagetools", "inspect", "--raw", reference], env=docker_env, timeout=60)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 1024 ** 2 or "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("build manifest digest")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int or
            manifest["schemaVersion"] != 2 or manifest.get("mediaType") not in IMAGE_MANIFEST_TYPES):
        raise ValueError("build image manifest")
    if descriptor is not None and (descriptor["mediaType"] != manifest["mediaType"] or
            ("size" in descriptor and (type(descriptor["size"]) is not int or descriptor["size"] != len(raw)))):
        raise ValueError("build descriptor content")
    config = manifest.get("config")
    if (not isinstance(config, dict) or config.get("mediaType") not in IMAGE_CONFIG_TYPES or
            not isinstance(config.get("digest"), str) or not DIGEST.fullmatch(config["digest"]) or
            type(config.get("size")) is not int or not 0 < config["size"] <= 1024 ** 2):
        raise ValueError("build image config")
    if annotated is not None and annotated != config["digest"]:
        raise ValueError("build config conflict")
    raw_config = registry_config_bytes(kind, config["digest"], docker_env)
    if (not isinstance(raw_config, bytes) or len(raw_config) != config["size"] or
            "sha256:" + hashlib.sha256(raw_config).hexdigest() != config["digest"]):
        raise ValueError("build config content")
    content = json.loads(raw_config)
    if not isinstance(content, dict) or content.get("os") != "linux" or content.get("architecture") != "amd64":
        raise ValueError("build config platform")
    if identity is not None:
        image_config = content.get("config")
        labels = image_config.get("Labels") if isinstance(image_config, dict) else None
        if (not isinstance(labels, dict) or labels.get("org.opencontainers.image.revision") != identity["sourceSha"] or
                labels.get("org.opencontainers.image.version") != identity["version"]):
            raise ValueError("build config source")
    print("CNB build metadata: " + kind + " config verified from immutable image manifest.", flush=True)
    return digest, config["digest"]


def build_images(jobs, source, docker_env, directory, concurrency=2, timeout=5400, identity=None):
    """Bounded parallel builds; no receipt or next job after any failed validation."""
    if type(concurrency) is not int or concurrency not in (1, 2):
        raise ValueError("build concurrency")
    if identity is not None and (not isinstance(identity, dict) or set(identity) != {"sourceSha", "version"} or
            not isinstance(identity["sourceSha"], str) or not SHA.fullmatch(identity["sourceSha"]) or
            not isinstance(identity["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", identity["version"])):
        raise ValueError("build identity")
    pending, active, images, timings = list(jobs), {}, {}, {}
    try:
        while pending or active:
            # Inspect all running builds before admitting any queued build.
            for kind, (process, start, log, metadata) in list(active.items()):
                result = process.poll()
                if result is None:
                    if time.monotonic() - start > timeout:
                        raise TimeoutError("image build deadline: " + kind)
                    continue
                log.flush()
                log.seek(0)
                shutil.copyfileobj(log, sys.stdout.buffer)
                sys.stdout.buffer.flush()
                if result:
                    raise subprocess.CalledProcessError(result, process.args)
                value = json.loads(metadata.read_text())
                digest, config = image_identity_from_metadata(value, kind, docker_env, identity)
                images[kind] = {"ref": REGISTRY + "/" + kind + "@" + digest, "id": config}
                timings[kind] = round(time.monotonic() - start, 3)
                log.close()
                del active[kind]
            while pending and len(active) < concurrency:
                kind, command, metadata = pending.pop(0)
                log = (Path(directory) / (kind + ".log")).open("w+b")
                try:
                    process = subprocess.Popen(command, cwd=source, env=docker_env, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                except BaseException:
                    log.close()
                    raise
                active[kind] = (process, time.monotonic(), log, metadata)
            if active:
                time.sleep(0.1)
        return images, timings
    finally:
        try:
            stop_builds(active)
        finally:
            for _process, _start, log, _metadata in active.values():
                log.close()


def warm_codex_runtime_cache(source, docker_env, directory, cache_tag="main", timeout=300):
    """Optional cache-only build; never add a service or change the release manifest."""
    cache = cache_references(CODEX_CACHE_KIND, cache_tag)
    context = Path("tools/codex-agent-runtime")
    # Earlier canonical commits do not contain the isolated runner yet.
    if not (source / context / "Dockerfile").is_file():
        return "absent"
    command = ["docker", "buildx", "build", "--platform", "linux/amd64",
               "--provenance=false", "--sbom=false", "--output=type=cacheonly", "--progress=plain",
               "--cache-from", "type=registry,ref=" + cache,
               "--cache-to", "type=registry,ref=" + cache + ",mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true",
               "--file", str(context / "Dockerfile")]
    if cache_tag != "main":
        command += ["--cache-from", "type=registry,ref=" + cache_references(CODEX_CACHE_KIND)]
    active = {}
    try:
        with (Path(directory) / "codex-runtime-cache.log").open("w+b") as log:
            try:
                process = subprocess.Popen(command + [str(context)], cwd=source, env=docker_env,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                active[CODEX_CACHE_KIND] = (process, time.monotonic(), log, None)
                return "completed" if process.wait(timeout=timeout) == 0 else "failed"
            except subprocess.TimeoutExpired:
                return "timed_out"
            finally:
                # SystemExit/KeyboardInterrupt propagate after terminating the whole build group.
                stop_builds(active)
                log.flush()
                log.seek(0)
                shutil.copyfileobj(log, sys.stdout.buffer)
                sys.stdout.buffer.flush()
    except OSError:
        return "unavailable"


def image_jobs(sha, version, request_id, directory, cache_tag, runtime):
    """The runtime is a required release image only for explicitly opted-in source."""
    jobs = []
    kinds = ("api", "worker", "web") + ((RUNTIME_KIND,) if runtime is not None else ())
    for kind in kinds:
        tag = REGISTRY + "/" + kind + ":" + request_id
        cache = cache_references(kind, cache_tag)
        metadata = Path(directory) / (kind + ".json")
        context = RUNTIME_ROOT.rstrip("/") if kind == RUNTIME_KIND else "."
        dockerfile = RUNTIME_ROOT + "Dockerfile" if kind == RUNTIME_KIND else "Dockerfile." + kind
        command = ["docker", "buildx", "build", "--platform", "linux/amd64",
                   "--provenance=false", "--sbom=false", "--push", "--progress=plain",
                   "--label", "org.opencontainers.image.revision=" + sha,
                   "--label", "org.opencontainers.image.version=" + version,
                   "--cache-from", "type=registry,ref=" + cache,
                   "--cache-to", "type=registry,ref=" + cache + ",mode=max,image-manifest=true,oci-mediatypes=true",
                   "--metadata-file", str(metadata), "--file", dockerfile, "--tag", tag]
        if cache_tag != "main":
            command += ["--cache-from", "type=registry,ref=" + cache_references(kind)]
        if kind == "web":
            command += ["--build-arg", "VITE_API_BASE_URL=/api", "--build-arg",
                        "VITE_DEPLOY_ENV=shukezhinao-prod", "--build-arg", "VITE_BUILD_SHA=" + sha]
        if kind == RUNTIME_KIND:
            for label, value in runtime_labels(runtime).items():
                command += ["--label", label + "=" + value]
        jobs.append((kind, command + [context], metadata))
    return jobs


def main():
    started = time.monotonic()
    source = Path("source").resolve()
    provenance = json.loads(Path(".release-builder/source.json").read_text())
    sha, version = provenance["sourceSha"], provenance["version"]
    snapshot = os.environ["CNB_COMMIT"]
    request_id = os.environ["RELEASE_REQUEST_ID"]
    if (os.environ.get("CNB_REPO_SLUG_LOWERCASE") != REPO or
            os.environ.get("CNB_EVENT") != "api_trigger_tencent" or
            not SHA.fullmatch(sha) or not SHA.fullmatch(snapshot) or
            not re.fullmatch(r"[0-9a-f]{32}", request_id) or
            provenance["sourceRepository"] != SOURCE or
            run(["git", "rev-parse", "HEAD:source"]).decode().strip() != provenance["sourceTree"] or
            json.loads((source / "package.json").read_text())["version"] != version):
        raise ValueError("source identity")
    token = os.environ["CNB_TOKEN"]
    concurrency = os.environ.get("CNB_BUILD_CONCURRENCY", "2")
    if concurrency not in ("1", "2"):
        raise ValueError("build concurrency")
    cache_tag = os.environ.get("CNB_CACHE_TAG", "main")
    cache_references("api", cache_tag)
    runtime = filesystem_runtime(source)
    changelog = (source / "changelog" / (version + ".md")).read_text()
    front = changelog.split("---", 2)
    if (len(front) != 3 or front[0].strip() or not front[2].strip() or
            not re.search(r'^status:\s*[\"]?published[\"]?\s*$', front[1], re.M) or
            not re.search(r'^version:\s*[\"]?' + re.escape(version) + r'[\"]?\s*$', front[1], re.M)):
        raise ValueError("published changelog required")
    # The CNB job's ephemeral registry token is never passed to a Docker build.
    with tempfile.TemporaryDirectory(prefix="cnb-docker-") as directory:
        docker_env = dict(os.environ, DOCKER_CONFIG=directory)
        run(["docker", "login", "docker.cnb.cool", "--username",
             os.environ.get("CNB_TOKEN_USER_NAME", "cnb"), "--password-stdin"],
            input=token.encode(), env=docker_env)
        # CNB provides a rootless Docker/BuildKit service with registry cache.
        # A nested docker-container driver cannot mount sysfs in that service.
        jobs = image_jobs(sha, version, request_id, directory, cache_tag, runtime)
        images, timings = build_images(jobs, source, docker_env, directory, int(concurrency),
                                       identity={"sourceSha": sha, "version": version})
        # Verify the actual Web bytes before making a completion receipt.
        run(["docker", "pull", images["web"]["ref"]], env=docker_env)
        container = run(["docker", "create", images["web"]["ref"]], env=docker_env).decode().strip()
        try:
            target = Path(directory) / "version.json"
            run(["docker", "cp", container + ":/usr/share/nginx/html/version.json", str(target)], env=docker_env)
            actual = json.loads(target.read_text())
            if actual.get("version") != version or actual.get("buildSha") != sha:
                raise ValueError("web version")
        finally:
            run(["docker", "rm", container], env=docker_env)
        if runtime is not None:
            run(["docker", "pull", images[RUNTIME_KIND]["ref"]], env=docker_env)
            inspect_runtime_image(runtime, images[RUNTIME_KIND]["ref"], directory,
                                  lambda args: run(["docker", *args], env=docker_env),
                                  lambda args: subprocess.run(["docker", *args], env=docker_env,
                                                              capture_output=True, timeout=30, check=False))
        else:
            cache_started = time.monotonic()
            cache_status = warm_codex_runtime_cache(source, docker_env, directory, cache_tag)
            # Preserve the optional cache-only path for older source trees.
            print("Codex runtime cache build: " + cache_status + "; seconds=" +
                  str(round(time.monotonic() - cache_started, 3)), flush=True)
        timings["total"] = round(time.monotonic() - started, 3)
        manifest = dict(schemaVersion=3 if runtime is not None else 2, deployment="teaching-production", sourceRepository=SOURCE,
                        sourceSha=sha, version=version, sourceTree=provenance["sourceTree"],
                        snapshotSha=snapshot, requestId=request_id, images=images, timings=timings)
        if runtime is not None:
            manifest["runtime"] = runtime
        validate_manifest(manifest, sha, version, snapshot, request_id)
        payload = {"annotations": [{"key": "tencent-" + request_id, "value": canonical(manifest).decode()}]}
        request = Request("https://api.cnb.cool/" + REPO + "/-/git/commit-annotations/" + snapshot,
                          data=canonical(payload), method="PUT", headers={"Authorization": "Bearer " + token,
                          "Accept": "application/vnd.cnb.api+json", "Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError("receipt publication")
        print("CNB image build and Web version verification completed.", flush=True)


if __name__ == "__main__":
    # CNB owns the process tree; cancellation must terminate the job, not retry.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    main()
