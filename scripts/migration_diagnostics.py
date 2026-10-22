"""Persist only validated migration metadata in the fixed private target bucket."""
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess

FILE_ENV = "MIGRATION_BOOTSTRAP_DIAGNOSTICS"
PROJECT = "project-f1f104ee-36ad-451a-9b5"
PHASES = frozenset(("preflight", "source", "identity", "project", "package-manager", "dependencies", "changelog",
    "runtime-config", "source-archive", "source-upload", "build-api", "build-worker", "build-web", "deploy-api",
    "deploy-web", "deploy-worker", "deploy-ppt-worker", "verify-resources", "validation-deploy", "validation-execute", "complete"))
STATES = frozenset(("start", "success", "failure"))


def normalized_receipt(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError("Invalid private receipt")
        value = json.loads(stream.read(16385))
    if (not isinstance(value, dict) or value.get("schemaVersion") != 1 or
            value.get("phase") not in PHASES or value.get("state") not in STATES or
            not isinstance(value.get("records"), list) or len(value["records"]) > 64):
        raise ValueError("Invalid receipt schema")
    result = {"schemaVersion": 1, "phase": value["phase"], "state": value["state"], "records": []}
    for record in value["records"]:
        if not isinstance(record, dict) or record.get("phase") not in PHASES or record.get("state") not in STATES:
            raise ValueError("Invalid phase record")
        result["records"].append({"phase": record["phase"], "state": record["state"]})
    if "identity" in value:
        identity = value["identity"]
        if (not isinstance(identity, dict) or identity.get("accountState") not in ("exact", "unset", "other") or
                type(identity.get("projectMatches")) is not bool or type(identity.get("impersonationTargetMatches")) is not bool or
                identity.get("credentialFileType") not in ("unavailable", "external_account", "service_account", "authorized_user", "unsupported")):
            raise ValueError("Invalid identity metadata")
        result["identity"] = {key: identity[key] for key in ("accountState", "projectMatches", "credentialFileType", "impersonationTargetMatches")}
    if "command" in value:
        command = value["command"]
        if (not isinstance(command, dict) or command.get("tool") not in ("gcloud", "git", "npm", "pnpm", "node") or
                type(command.get("exitCode")) is not int or not -128 <= command["exitCode"] <= 255):
            raise ValueError("Invalid command metadata")
        result["command"] = {"tool": command["tool"], "exitCode": command["exitCode"]}
    return result


def upload_failure_receipt(path, private, environment):
    """Caller limits this to migration failures; never upload private.log."""
    if (environment.get("GITHUB_ACTIONS") != "true" or environment.get("RELEASE_OPERATION") != "migration-bootstrap" or
            environment.get("GCP_PRODUCTION_PROJECT_ID") != PROJECT or
            not re.fullmatch(r"[1-9][0-9]{0,19}", environment.get("GITHUB_RUN_ID", "")) or
            not re.fullmatch(r"[1-9][0-9]{0,4}", environment.get("GITHUB_RUN_ATTEMPT", ""))):
        return False
    receipt = normalized_receipt(path)
    output = Path(private) / "migration-diagnostics-upload.json"
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(receipt, stream, separators=(",", ":"))
    destination = (f"gs://{PROJECT}_cloudbuild/migration-diagnostics/"
                   f"{environment['GITHUB_RUN_ID']}/{environment['GITHUB_RUN_ATTEMPT']}.log")
    process = subprocess.Popen(["gcloud", "--project=" + PROJECT, "--billing-project=" + PROJECT,
        "storage", "cp", str(output), destination, "--no-clobber", "--quiet", "--content-type=application/json"],
        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait(timeout=20) == 0
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise
