#!/usr/bin/env python3
"""Run only in the private CNB project; no Production or GitHub credentials."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

from cnb_release import REPO, REGISTRY, SOURCE, SHA, DIGEST, canonical, validate_manifest


def run(args, **kwargs):
    kwargs.setdefault('timeout', 300)
    return subprocess.check_output(args, **kwargs)


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
        images, timings = {}, {}
        for kind in ("api", "worker", "web"):
            start = time.monotonic()
            tag = REGISTRY + "/" + kind + ":" + request_id
            cache = REGISTRY + "/cache-" + kind + ":main"
            metadata = Path(directory) / (kind + ".json")
            command = ["docker", "buildx", "build", "--platform", "linux/amd64",
                       "--provenance=false", "--sbom=false", "--push", "--progress=plain",
                       "--label", "org.opencontainers.image.revision=" + sha,
                       "--label", "org.opencontainers.image.version=" + version,
                       "--cache-from", "type=registry,ref=" + cache,
                       "--cache-to", "type=registry,ref=" + cache + ",mode=max,image-manifest=true,oci-mediatypes=true",
                       "--metadata-file", str(metadata), "--file", "Dockerfile." + kind, "--tag", tag]
            if kind == "web":
                command += ["--build-arg", "VITE_API_BASE_URL=/api", "--build-arg",
                            "VITE_DEPLOY_ENV=shukezhinao-prod", "--build-arg", "VITE_BUILD_SHA=" + sha]
            subprocess.run(command + ["."], cwd=source, env=docker_env, check=True, timeout=5400)
            result = json.loads(metadata.read_text())
            digest, config = result["containerimage.digest"], result["containerimage.config.digest"]
            if not DIGEST.fullmatch(digest) or not DIGEST.fullmatch(config):
                raise ValueError("build digest")
            images[kind] = {"ref": REGISTRY + "/" + kind + "@" + digest, "id": config}
            timings[kind] = round(time.monotonic() - start, 3)
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
        timings["total"] = round(time.monotonic() - started, 3)
        manifest = dict(schemaVersion=2, deployment="teaching-production", sourceRepository=SOURCE,
                        sourceSha=sha, version=version, sourceTree=provenance["sourceTree"],
                        snapshotSha=snapshot, requestId=request_id, images=images, timings=timings)
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
