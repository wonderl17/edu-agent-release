#!/usr/bin/env python3
"""Poll private version metadata and queue releases in the public runner repo."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable


ALLOWED_REPOSITORIES = frozenset(("shukezhinao/edu-agent", "wonderl17/edu-agent"))
OPERATIONS = {"cloud": "package.json", "desktop": "apps/desktop/package.json"}
ENVIRONMENT = "automatic-release"
MAX_DEPLOYMENT_PAGES = 100
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


class PollFailure(Exception):
    def __init__(self, code: int):
        self.code = code


def parse_version(value: Any) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("Invalid version")
    match = SEMVER.fullmatch(value)
    if match is None:
        raise ValueError("Invalid version")
    return tuple(int(match[index]) for index in (1, 2, 3)), tuple(match[4].split(".")) if match[4] else None


def compare_versions(left: str, right: str) -> int:
    left_core, left_pre = parse_version(left)
    right_core, right_pre = parse_version(right)
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    if left_pre == right_pre:
        return 0
    if left_pre is None or right_pre is None:
        return 1 if left_pre is None else -1
    for left_part, right_part in zip(left_pre, right_pre):
        if left_part == right_part:
            continue
        left_numeric, right_numeric = left_part.isdigit(), right_part.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_part) > int(right_part) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_part > right_part else -1
    return 1 if len(left_pre) > len(right_pre) else -1


def validate_configuration(config: Any, environment: dict[str, str]) -> list[dict[str, Any]]:
    try:
        sources = config["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= len(ALLOWED_REPOSITORIES):
            raise ValueError()
        repositories = set()
        for source in sources:
            repository = source["repository"]
            if repository not in ALLOWED_REPOSITORIES or repository in repositories or not isinstance(source["automatic"], bool):
                raise ValueError()
            repositories.add(repository)
            for operation in OPERATIONS:
                parse_version(source["baseline"][operation])
        public_repository = environment.get("PUBLIC_REPOSITORY", "")
        if not re.fullmatch(r"wonderl17/[A-Za-z0-9_.-]+", public_repository) or public_repository in ALLOWED_REPOSITORIES:
            raise ValueError()
        if not environment.get("PUBLIC_GH_TOKEN") or not re.fullmatch(r"[0-9a-fA-F]{40}", environment.get("GITHUB_SHA", "")):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise PollFailure(30) from None
    return sources


def source_versions(repository: str, api: Callable, token_for_repository: Callable) -> tuple[str, dict[str, str]]:
    try:
        token = token_for_repository(repository, permission="read")
        commit = api(token, "GET", f"/repos/{repository}/commits/main")
        sha = commit["sha"]
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ValueError()
        versions = {}
        for operation, path in OPERATIONS.items():
            result = api(token, "GET", f"/repos/{repository}/contents/{path}?ref={sha}")
            if result["encoding"] != "base64" or not isinstance(result["content"], str) or len(result["content"]) > 1_400_000:
                raise ValueError()
            content = base64.b64decode("".join(result["content"].split()), validate=True)
            version = json.loads(content)["version"]
            parse_version(version)
            versions[operation] = version
        return sha, versions
    except Exception:
        raise PollFailure(31) from None


def attempted_version(repository: str, operation: str, baseline: str, public_repository: str, token: str, api: Callable) -> str:
    try:
        for page in range(1, MAX_DEPLOYMENT_PAGES + 1):
            deployments = api(
                token, "GET",
                f"/repos/{public_repository}/deployments?task=release-{operation}&per_page=100&page={page}",
            )
            if not isinstance(deployments, list):
                raise ValueError()
            for deployment in deployments:
                payload = deployment.get("payload", {})
                if deployment.get("task") != f"release-{operation}" or not isinstance(payload, dict):
                    continue
                if payload.get("source_repository") != repository or payload.get("operation") != operation:
                    continue
                version = payload["version"]
                parse_version(version)
                return version if compare_versions(version, baseline) > 0 else baseline
            if len(deployments) < 100:
                return baseline
        # Do not risk duplicate publication if pagination was truncated.
        raise ValueError()
    except Exception:
        raise PollFailure(32) from None


def queue_release(repository: str, sha: str, operation: str, version: str, environment: dict[str, str], api: Callable) -> None:
    public_repository = environment["PUBLIC_REPOSITORY"]
    token = environment["PUBLIC_GH_TOKEN"]
    deployment_id = None
    try:
        deployment = api(token, "POST", f"/repos/{public_repository}/deployments", {
            "ref": environment["GITHUB_SHA"],
            "task": f"release-{operation}",
            "auto_merge": False,
            "required_contexts": [],
            "environment": ENVIRONMENT,
            "description": "Automatic release attempt",
            "payload": {"source_repository": repository, "source_sha": sha, "version": version, "operation": operation},
        })
        deployment_id = deployment["id"]
        if not isinstance(deployment_id, int) or deployment_id <= 0:
            raise ValueError()
        api(token, "POST", f"/repos/{public_repository}/actions/workflows/release.yml/dispatches", {
            "ref": "main",
            "inputs": {
                "source_repository": repository, "source_sha": sha,
                "operation": operation, "target": "all", "confirm_release": "yes",
            },
        })
    except Exception:
        if deployment_id is not None:
            try:
                api(token, "POST", f"/repos/{public_repository}/deployments/{deployment_id}/statuses", {
                    "state": "error", "description": "Release dispatch failed; manual recovery required.",
                })
            except Exception:
                pass
        raise PollFailure(33) from None


def poll(config: Any, environment: dict[str, str], api: Callable, token_for_repository: Callable) -> int:
    sources = validate_configuration(config, environment)
    count = 0
    for source in sources:
        if not source["automatic"]:
            continue
        repository = source["repository"]
        sha, versions = source_versions(repository, api, token_for_repository)
        for operation in OPERATIONS:
            cursor = attempted_version(
                repository, operation, source["baseline"][operation],
                environment["PUBLIC_REPOSITORY"], environment["PUBLIC_GH_TOKEN"], api,
            )
            if compare_versions(versions[operation], cursor) <= 0:
                continue
            queue_release(repository, sha, operation, versions[operation], environment, api)
            count += 1
    return count


def main() -> int:
    print("Release polling started.", flush=True)
    try:
        from release_auth import github_request, installation_token

        config = json.loads((Path(__file__).resolve().parents[1] / "config/repositories.json").read_text())
        poll(config, dict(os.environ), github_request, installation_token)
    except PollFailure as error:
        print(f"Release polling failed (code={error.code}).", flush=True)
        return error.code
    except BaseException:
        print("Release polling failed (code=34).", flush=True)
        return 34
    print("Release polling complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
