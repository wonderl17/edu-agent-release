#!/usr/bin/env python3
"""Run trusted private release code without forwarding its output to Actions."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile


ALLOWED_REPOSITORIES = frozenset(("wonderl17/edu-agent", "shukezhinao/edu-agent"))
ALLOWED_MODES = frozenset(("cloud", "desktop", "resume", "verify"))
FILE_COMMANDS = ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE")
REMOVED_ENVIRONMENT = frozenset((
    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "PROMPT_COMMAND",
    "NODE_OPTIONS", "PYTHONINSPECT", "PYTHONSTARTUP", "PYTHONVERBOSE",
    "GIT_CURL_VERBOSE", "CLOUDSDK_CORE_LOG_HTTP", "CLOUDSDK_CORE_VERBOSITY",
    "AZURE_CORE_LOG_LEVEL", "NPM_CONFIG_LOGLEVEL", "PNPM_LOG_LEVEL",
    "DOTNET_CLI_CONTEXT_VERBOSE",
))


class ReleaseFailure(Exception):
    def __init__(self, code: int):
        self.code = code


class ReleaseCancelled(Exception):
    def __init__(self, signum: int):
        self.code = 128 + signum


def public_status(message: str) -> None:
    # Only constant strings and numeric error codes may reach the public log.
    os.write(1, (message + "\n").encode("ascii"))


def cancel(signum: int, _frame: object) -> None:
    raise ReleaseCancelled(signum)


def configuration(environment: dict[str, str]) -> tuple[Path, Path, str, str]:
    repository = environment.get("SOURCE_REPOSITORY", "")
    sha = environment.get("SOURCE_SHA", "")
    if repository not in ALLOWED_REPOSITORIES or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise ReleaseFailure(20)
    if not environment.get("RUNNER_TEMP") or not environment.get("RELEASE_SOURCE_DIR"):
        raise ReleaseFailure(20)
    try:
        runner_temp = Path(environment["RUNNER_TEMP"]).resolve(strict=True)
        source = Path(environment["RELEASE_SOURCE_DIR"]).resolve(strict=True)
        if not runner_temp.is_dir() or not source.is_dir() or not source.is_relative_to(runner_temp) or source == runner_temp:
            raise ReleaseFailure(20)
        source.chmod(0o700)
    except (OSError, ValueError):
        raise ReleaseFailure(20) from None
    return runner_temp, source, repository, sha.lower()


def child_environment(environment: dict[str, str], private: Path, source: Path, repository: str, sha: str) -> dict[str, str]:
    child = {
        key: value for key, value in environment.items()
        if key.upper() not in REMOVED_ENVIRONMENT
        and not re.search(r"DEBUG|TRACE", key.upper())
    }
    for name in FILE_COMMANDS:
        path = private / name.lower()
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        child[name] = str(path)
    child.update({
        "RELEASE_SOURCE_DIR": str(source),
        "GITHUB_WORKSPACE": str(source),
        "GITHUB_REPOSITORY": repository,
        "GITHUB_SHA": sha,
        "GITHUB_REF": "refs/heads/main",
        "CI": "true",
    })
    return child


def run_private(command: list[str], *, source: Path, environment: dict[str, str], log: object, capture_metadata: bool = False) -> tuple[int, bytes]:
    process = subprocess.Popen(
        command,
        cwd=source,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_metadata else log,
        stderr=log,
        start_new_session=True,
    )
    try:
        metadata, _ = process.communicate()
        return process.returncode, metadata or b""
    except BaseException:
        # Cancellation must also stop descendants that may be publishing.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise


def verify_source(source: Path, repository: str, sha: str, environment: dict[str, str], log: object) -> None:
    def git(*arguments: str, metadata: bool = False) -> bytes:
        code, output = run_private(
            ["git", "-c", "core.hooksPath=/dev/null", *arguments],
            source=source, environment=environment, log=log, capture_metadata=metadata,
        )
        if code:
            raise ReleaseFailure(21)
        return output.strip()

    origin = git("remote", "get-url", "origin", metadata=True)
    allowed_origins = {
        f"https://github.com/{repository}".encode(),
        f"https://github.com/{repository}.git".encode(),
        f"git@github.com:{repository}".encode(),
        f"git@github.com:{repository}.git".encode(),
    }
    if origin not in allowed_origins or git("rev-parse", "--verify", "HEAD", metadata=True) != sha.encode():
        raise ReleaseFailure(21)
    git("merge-base", "--is-ancestor", sha, "refs/remotes/origin/main")
    git("checkout", "-B", "main", sha)


def execute(mode: str, environment: dict[str, str]) -> int:
    private: Path | None = None
    previous_umask = os.umask(0o077)
    try:
        runner_temp, source, repository, sha = configuration(environment)
        private = Path(tempfile.mkdtemp(prefix="release-private-", dir=runner_temp))
        private.chmod(0o700)
        child = child_environment(environment, private, source, repository, sha)
        descriptor = os.open(private / "private.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb", buffering=0) as log:
            verify_source(source, repository, sha, child, log)
            if mode == "verify":
                return 0
            entry = "scripts/ci-release-cloud.mjs" if mode == "cloud" else "scripts/ci-release-desktop.sh"
            try:
                script = (source / entry).resolve(strict=True)
            except OSError:
                raise ReleaseFailure(22) from None
            if not script.is_file() or not script.is_relative_to(source):
                raise ReleaseFailure(22)
            command = ["node", str(script)] if mode == "cloud" else ["bash", str(script), mode]
            code, _ = run_private(command, source=source, environment=child, log=log)
            if code:
                raise ReleaseFailure(22)
        return 0
    finally:
        if private is not None:
            # Nothing here is an Actions artifact or cache. Keep source cleanup
            # with the workflow's always() step so all stages can use it.
            shutil.rmtree(private, ignore_errors=True)
        os.umask(previous_umask)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ALLOWED_MODES:
        public_status("Release stage rejected (code=20).")
        return 20
    mode = sys.argv[1]
    public_status(f"Release stage: {mode}.")
    try:
        result = execute(mode, dict(os.environ))
    except ReleaseCancelled as error:
        public_status(f"Release stage cancelled (code={error.code}).")
        return error.code
    except ReleaseFailure as error:
        public_status(f"Release stage failed (code={error.code}).")
        return error.code
    except BaseException:
        # Exception text and tracebacks can contain private source or secrets.
        public_status("Release stage failed (code=23).")
        return 23
    public_status("Release stage complete.")
    return result


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    raise SystemExit(main())
