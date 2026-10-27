"""GitHub-side CNB source handoff, build cancellation and receipt validation."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from urllib.request import Request, urlopen

from cnb_release import REPO, SOURCE, SHA, canonical, validate_manifest
from release_usage import from_status, pending


def api(token, method, path, value=None, *, timeout=45):
    request = Request("https://api.cnb.cool/" + REPO + path, method=method,
                      data=canonical(value) if value is not None else None,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.cnb.api+json", "Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("CNB response limit")
        return json.loads(raw) if raw else None


def mirror(source, sha, version, private, child, log, token, run):
    """Copy the canonical tree, not its 5+ GiB history or local working files."""
    controller = Path(__file__).resolve().parents[1]
    target = private / "cnb-source"
    target.mkdir(mode=0o700)
    source_tree = run(["git", "rev-parse", sha + "^{tree}"], child, log, cwd=source, capture=True).decode().strip()
    objects = run(["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"], child, log, cwd=source, capture=True).decode().strip()
    auth = base64.b64encode(("cnb:" + token).encode()).decode()
    git_env = dict(child, GIT_ALTERNATE_OBJECT_DIRECTORIES=objects, GIT_CONFIG_COUNT="3",
                   GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0="/dev/null",
                   GIT_CONFIG_KEY_1="http.https://cnb.cool/" + REPO + ".git.extraheader",
                   GIT_CONFIG_VALUE_1="AUTHORIZATION: basic " + auth,
                   GIT_CONFIG_KEY_2="credential.helper", GIT_CONFIG_VALUE_2="")
    def git(*args, capture=False):
        return run(["git", *args], git_env, log, cwd=target, capture=capture)
    git("init", "--quiet")
    git("remote", "add", "origin", "https://cnb.cool/" + REPO + ".git")
    head = git("ls-remote", "--heads", "origin", "main", capture=True).decode().strip()
    if head:
        previous = head.split()[0]
        if not SHA.fullmatch(previous):
            raise ValueError("CNB branch identity")
        git("fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none", "origin", "main")
        git("update-ref", "refs/heads/main", previous)
    git("symbolic-ref", "HEAD", "refs/heads/main")
    git("read-tree", "--empty")
    git("read-tree", "--prefix=source/", source_tree)
    builder = target / ".release-builder"
    builder.mkdir()
    shutil.copyfile(controller / "cnb/pipeline.yml", target / ".cnb.yml")
    shutil.copyfile(controller / "cnb/build.py", builder / "build.py")
    shutil.copyfile(controller / "scripts/cnb_release.py", builder / "cnb_release.py")
    (builder / "source.json").write_bytes(canonical(dict(sourceRepository=SOURCE, sourceSha=sha,
                                                        sourceTree=source_tree, version=version)))
    git("add", "--", ".cnb.yml", ".release-builder")
    git("-c", "user.name=Tencent release controller", "-c", "user.email=release@users.noreply.github.com",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Build canonical source " + sha)
    snapshot = git("rev-parse", "HEAD", capture=True).decode().strip()
    if git("rev-parse", "HEAD:source", capture=True).decode().strip() != source_tree:
        raise ValueError("source tree altered")
    git("push", "--quiet", "origin", "HEAD:refs/heads/main")
    return snapshot, source_tree


def verify_access(token):
    """Read-only credential/destination check; never sync source or start a job."""
    if not token:
        raise ValueError("CNB configuration")
    project = api(token, "GET", "")
    if project.get("visibility_level") != "Private" or project.get("path") != REPO:
        raise ValueError("CNB repository must be private")
    api(token, "GET", "/-/build/logs?page=1&page_size=1")


def build(source, sha, version, private, child, log, token, run, status, usage=None, run_id=None, run_attempt=None,
          concurrency="2"):
    if not SHA.fullmatch(sha) or concurrency not in ("1", "2"):
        raise ValueError("CNB configuration")
    # Never send source to a public/mistaken destination.
    verify_access(token)
    started = time.monotonic()
    status("Tencent Production: synchronizing validated source to private CNB.")
    snapshot, source_tree = mirror(source, sha, version, private, child, log, token, run)
    request_id = uuid.uuid4().hex
    if usage is not None:
        # A lost trigger response may still have started a billable build.
        usage.update(pending())
    title = "Tencent Production " + version
    if (isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,19}", run_id) and
            isinstance(run_attempt, str) and re.fullmatch(r"[1-9][0-9]{0,5}", run_attempt)):
        # Allows later accounting reconciliation without exposing private CNB IDs.
        title += " [GitHub " + run_id + "/" + run_attempt + "]"
    result = api(token, "POST", "/-/build/start", {
        "branch": "main", "sha": snapshot, "event": "api_trigger_tencent",
        "env": {"RELEASE_REQUEST_ID": request_id, "CNB_BUILD_CONCURRENCY": concurrency}, "title": title})
    sn = result.get("sn", "")
    if result.get("success") is not True or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sn):
        raise ValueError("CNB trigger failed")
    (private / "cnb-build.json").write_bytes(canonical(dict(snapshotSha=snapshot, requestId=request_id, sn=sn)))
    complete = False
    terminal = False
    state = None
    try:
        status("Tencent Production: CNB building and pushing cached image layers.")
        deadline = time.monotonic() + 6000
        while time.monotonic() < deadline:
            state = api(token, "GET", "/-/build/status/" + sn)
            if state.get("status") == "success":
                complete = terminal = True
                (private / "cnb-status.json").write_bytes(canonical(state))
                break
            if state.get("status") in ("fail", "failed", "error", "cancel", "cancelled", "canceled", "timeout"):
                terminal = True
                raise ValueError("CNB build failed")
            time.sleep(15)
        if not complete:
            raise TimeoutError("CNB build deadline")
        annotations = api(token, "GET", "/-/git/commit-annotations/" + snapshot)
        values = [x["value"] for x in annotations if x.get("key") == "tencent-" + request_id]
        if len(values) != 1:
            raise ValueError("CNB receipt missing")
        manifest = validate_manifest(json.loads(values[0]), sha, version, snapshot, request_id)
        if manifest["sourceTree"] != source_tree:
            raise ValueError("CNB source tree mismatch")
        path = private / "registry-manifest.json"
        path.write_bytes(canonical(manifest))
        status("Tencent Production: CNB images verified; build and source sync took " +
               str(round(time.monotonic() - started)) + " seconds.")
        return path
    finally:
        if not terminal:
            try:
                api(token, "POST", "/-/build/stop/" + sn)
            except Exception:
                status("Tencent Production: CNB cancellation could not be confirmed.")
        if usage is not None:
            try:
                measured = from_status(state)
                if measured["basis"] in ("pending", "estimated"):
                    # One short, read-only reconciliation after failure/stop or
                    # delayed accounting. Never delay cancellation with polling.
                    refreshed = from_status(api(token, "GET", "/-/build/status/" + sn, timeout=10))
                    if refreshed["basis"] != "pending":
                        measured = refreshed
                usage.update(measured)
            except Exception:
                usage.update(from_status(state))
