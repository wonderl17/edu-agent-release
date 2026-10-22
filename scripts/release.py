#!/usr/bin/env python3
"""Public controller. Private source and all subprocess output stay ephemeral."""

import base64
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

from release_auth import REPOSITORIES, github_request, installation_token, stop_process
from mirror_installers import mirror_installers
from desktop_checkpoints import CheckpointFile, FILE_ENV, report_checkpoints, without_checkpoint_environment

MODES = frozenset(("cloud", "desktop", "resume", "verify", "migration-bootstrap"))
SOURCE_PROJECT = "project-cd874991-605d-4143-a62"
MIGRATION_PROJECT = "project-f1f104ee-36ad-451a-9b5"


def production_project(environment):
    project = environment.get("GCP_PRODUCTION_PROJECT_ID") or MIGRATION_PROJECT
    if project != MIGRATION_PROJECT:
        raise ValueError("Unsupported GCP project")
    return project
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


class ReleaseCancelled(BaseException):
    def __init__(self, code):
        self.code = code


def cancelled(signum, _frame):
    raise ReleaseCancelled(128 + signum)


def run_process(command, environment, cwd=None):
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        return process.wait()
    except BaseException:
        stop_process(process)
        raise


def run_private_stage(operation, environment):
    command = [sys.executable, str(Path(__file__).with_name("private-run.py")), operation]
    child = without_checkpoint_environment(environment)
    checkpoint = None
    try:
        if operation in ("desktop", "resume") and child.get("RUNNER_TEMP"):
            try:
                checkpoint = CheckpointFile(child["RUNNER_TEMP"])
                child[FILE_ENV] = str(checkpoint.path)
            except OSError:
                pass
        return run_process(command, child)
    finally:
        if checkpoint is not None:
            try:
                report_checkpoints(checkpoint.path)
            finally:
                checkpoint.close()


def run_probe_process(command, environment, *, capture_stdout=False):
    """Drain diagnostic output privately, keeping at most 8 KiB per stream."""
    process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                               stderr=subprocess.PIPE, start_new_session=True)
    streams = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    for name in output:
        pipe = getattr(process, name)
        if pipe is not None:
            streams.register(pipe, selectors.EVENT_READ, name)
    deadline = time.monotonic() + 20
    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 20)
            for key, _events in streams.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                buffer = output[key.data]
                buffer.extend(chunk[:max(0, 8192 - len(buffer))])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 20)
        return process.wait(timeout=remaining), bytes(output["stdout"]), bytes(output["stderr"])
    except BaseException:
        stop_process(process)
        raise
    finally:
        streams.close()
        for name in output:
            pipe = getattr(process, name)
            if pipe is not None:
                pipe.close()


def probe_build_log_access(environment):
    """Read-only, non-blocking diagnostic with an allowlisted public result."""
    category = "unavailable"
    try:
        code, metadata, _error = run_probe_process([
            "gcloud", "builds", "list", "--project", production_project(environment),
            "--region", "global", "--limit", "1", "--sort-by=~createTime", "--format=json(id)",
        ], environment, capture_stdout=True)
        if code == 0:
            builds = json.loads(metadata)
            build_id = builds[0].get("id") if isinstance(builds, list) and len(builds) == 1 and isinstance(builds[0], dict) else None
            if isinstance(build_id, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", build_id):
                code, _output, error = run_probe_process([
                    "gcloud", "builds", "log", build_id, "--project", production_project(environment),
                    "--region", "global",
                ], environment)
                if code == 0:
                    category = "available"
                elif any(marker in error.lower() for marker in (
                    b"permission_denied", b"permission denied", b"permissiondenied", b"access denied",
                    b"accessdenied", b"forbidden", b"403", b"storage.objects.get",
                    b"do not have permission", b"does not have permission", b"do not have read access",
                )):
                    category = "permission-denied"
    except Exception:
        # Cancellation is a BaseException and must still stop the workflow.
        pass
    print(f"Cloud Build log access: {category}.", flush=True)


def clone_source(repository, sha, token, source):
    if repository not in REPOSITORIES or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid source")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GIT_CONFIG", "RELEASE_APP", "DESKTOP_", "R2_", "MACOS_", "GCP_", "GOOGLE_", "AZURE_", "PUBLIC_", "ACTIONS_"))
                   and key not in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")
                   and not re.search(r"DEBUG|TRACE", key.upper())}
    authorization = base64.b64encode(("x-access-token:" + token).encode()).decode()
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_KEY_0": "http.https://github.com/" + repository + ".git.extraheader",
                        "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + authorization,
                        "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": "/dev/null"})
    for command in (["git", "init", "--quiet"],
                    ["git", "remote", "add", "origin", "https://github.com/" + repository + ".git"],
                    ["git", "fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin", "main"],
                    ["git", "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main"],
                    ["git", "checkout", "--quiet", "-B", "main", sha]):
        if run_process(command, environment, cwd=source):
            raise RuntimeError("Source checkout failed")


def deployment(token, repository, operation, sha, source_repository, source_sha, version):
    return github_request(token, "POST", "/repos/" + repository + "/deployments", {
        "ref": sha, "task": "release-" + operation, "auto_merge": False, "required_contexts": [],
        "environment": "production-" + operation, "production_environment": True,
        "payload": {"source_repository": source_repository, "source_sha": source_sha,
                    "version": version, "operation": operation},
    })["id"]


def main():
    source = None
    source_owned = False
    was_cancelled = False
    token = None
    mirror_token = None
    deployment_id = None
    public_token = os.environ.get("PUBLIC_GH_TOKEN", "")
    public_repository = os.environ.get("PUBLIC_REPOSITORY", "wonderl17/edu-agent-release")
    result = 1
    try:
        operation = os.environ.get("RELEASE_OPERATION", "verify")
        repository = os.environ.get("SOURCE_REPOSITORY", "")
        requested_sha = os.environ.get("SOURCE_SHA", "")
        if operation not in MODES or repository not in REPOSITORIES:
            raise ValueError("Invalid operation")
        if os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise ValueError("Main branch required")
        if operation != "verify" and os.environ.get("CONFIRM_RELEASE") != "yes":
            raise ValueError("Confirmation required")
        if requested_sha and not re.fullmatch(r"[0-9a-f]{40}", requested_sha):
            raise ValueError("Full source SHA required")
        if operation in ("cloud", "verify", "migration-bootstrap") and production_project(os.environ) != MIGRATION_PROJECT:
            raise ValueError("Exact Production project required")
        if operation == "migration-bootstrap" and (
                not requested_sha or repository != "shukezhinao/edu-agent" or
                production_project(os.environ) != MIGRATION_PROJECT or
                os.environ.get("RUN_MIGRATION") != "false" or
                os.environ.get("RELEASE_TARGET", "all") != "all" or
                os.environ.get("PRE_MIGRATION_REPAIR", "none") != "none"):
            raise ValueError("Explicit private migration target and source required")
        if os.environ.get("RELEASE_TARGET", "all") not in ("all", "api", "worker", "web"):
            raise ValueError("Invalid target")
        if not re.fullmatch(r"wonderl17/[A-Za-z0-9_.-]+", public_repository) or public_repository in REPOSITORIES:
            raise ValueError("Invalid public repository")
        if operation in ("cloud", "desktop", "resume", "migration-bootstrap") and (not public_token or not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("GITHUB_SHA", ""))):
            raise ValueError("Public release metadata unavailable")
        # Checkout never receives write permission. Desktop entry points refresh
        # a write token through the helper immediately before publishing.
        print("Release controller stage: auth.", flush=True)
        token = installation_token(repository, "read")
        source_sha = requested_sha or github_request(token, "GET", "/repos/" + repository + "/commits/main")["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ValueError("Invalid source SHA")
        print("Release controller stage: source.", flush=True)
        temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        source = temporary / "edu-agent-private-source"
        if source.exists() or source.is_symlink():
            raise ValueError("Source workspace already exists")
        source.mkdir(mode=0o700)
        source_owned = True
        clone_source(repository, source_sha, token, source)
        child = dict(os.environ)
        child.pop("PUBLIC_GH_TOKEN", None)
        child.pop("GITHUB_TOKEN", None)
        child.update({"SOURCE_SHA": source_sha, "SOURCE_REPOSITORY": repository,
                      "RELEASE_SOURCE_DIR": str(source), "GH_TOKEN": token,
                      "RELEASE_TOKEN_REFRESH_HELPER": str(Path(__file__).with_name("release_auth.py").resolve())})
        if operation == "verify":
            # Check required configuration without building or modifying Production.
            print("Release controller stage: verify.", flush=True)
            for name in ("DESKTOP_UPDATE_S3_ACCESS_KEY_ID", "DESKTOP_UPDATE_S3_SECRET_ACCESS_KEY",
                         "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "MACOS_CERTIFICATE_P12_BASE64",
                         "MACOS_CERTIFICATE_PASSWORD"):
                if not child.get(name):
                    raise ValueError("Required release configuration unavailable")
            for entry in ("scripts/ci-release-cloud.mjs", "scripts/ci-release-desktop.sh"):
                if not (source / entry).is_file():
                    raise ValueError("Private release entry unavailable")
            if run_process([
                "gcloud", "run", "services", "list",
                "--project", production_project(child), "--region", "asia-east1",
                "--format=value(metadata.name)",
            ], child, cwd=source):
                raise RuntimeError("Cloud access verification failed")
            probe_build_log_access(child)
        if operation in ("cloud", "desktop", "resume", "migration-bootstrap"):
            package = "package.json" if operation in ("cloud", "migration-bootstrap") else "apps/desktop/package.json"
            version = json.loads((source / package).read_text())["version"]
            if not isinstance(version, str) or len(version) > 200 or SEMVER.fullmatch(version) is None:
                raise ValueError("Invalid version")
            if operation in ("cloud", "desktop", "migration-bootstrap"):
                deployment_id = deployment(public_token, public_repository, operation, os.environ["GITHUB_SHA"],
                                           repository, source_sha, version)
        print("Release controller stage: private-execution.", flush=True)
        result = run_private_stage(operation, child)
        print(f"Private stage exit code: {result:d}.", flush=True)
        if result in (130, 143, -signal.SIGINT, -signal.SIGTERM):
            raise ReleaseCancelled(result if result > 0 else 128 - result)
        if operation in ("desktop", "resume"):
            # The private publisher can save installers even if COS fails. Copy
            # only those verified release assets, without changing that result.
            print("Release controller stage: public-installers.", flush=True)
            mirror_token = installation_token(repository, "read")
            mirror_installers(source_repository=repository, version=version, source_token=mirror_token,
                              public_repository=public_repository, public_sha=os.environ["GITHUB_SHA"],
                              public_token=public_token, temporary=temporary)
        if result:
            raise RuntimeError("Private stage failed")
        print("Release controller complete.", flush=True)
        return 0
    except ReleaseCancelled as error:
        was_cancelled = True
        result = error.code
        print("Release controller cancelled.", flush=True)
        return result
    except BaseException:
        result = 1
        print("Release controller failed; private diagnostics were withheld.", flush=True)
        return 1
    finally:
        if source_owned:
            shutil.rmtree(source, ignore_errors=True)
        # Cancellation stops publication immediately; do not wait on network
        # cleanup. The installation token expires automatically.
        if deployment_id and not was_cancelled:
            try:
                github_request(public_token, "POST", "/repos/" + public_repository + "/deployments/" + str(deployment_id) + "/statuses",
                               {"state": "success" if result == 0 else "failure", "auto_inactive": False})
            except BaseException:
                print("Release status recording failed.", flush=True)
        if not was_cancelled:
            for credential in (mirror_token, token):
                if credential:
                    try:
                        github_request(credential, "DELETE", "/installation/token")
                    except BaseException:
                        pass


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    raise SystemExit(main())
