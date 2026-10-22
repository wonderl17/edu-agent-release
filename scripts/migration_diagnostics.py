"""Persist only validated migration metadata in the fixed private target bucket."""
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import unicodedata

FILE_ENV = "MIGRATION_BOOTSTRAP_DIAGNOSTICS"
BUILD_ERROR_ENV = "MIGRATION_BOOTSTRAP_BUILD_ERROR"
BUILD_ERROR_FILE = "migration-build-error.log"
BUILD_PHASES = frozenset(("build-api", "build-worker", "build-web"))
BUILD_ERROR_LIMIT = 8192
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


def migration_environment(environment):
    return not (environment.get("GITHUB_ACTIONS") != "true" or environment.get("RELEASE_OPERATION") != "migration-bootstrap" or
            environment.get("GCP_PRODUCTION_PROJECT_ID") != PROJECT or
            not re.fullmatch(r"[1-9][0-9]{0,19}", environment.get("GITHUB_RUN_ID", "")) or
            not re.fullmatch(r"[1-9][0-9]{0,4}", environment.get("GITHUB_RUN_ATTEMPT", "")))


def upload_private_file(output, destination, environment, content_type):
    process = subprocess.Popen(["gcloud", "--project=" + PROJECT, "--billing-project=" + PROJECT,
        "storage", "cp", str(output), destination, "--no-clobber", "--quiet", "--content-type=" + content_type],
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


def upload_failure_receipt(path, private, environment):
    """Caller limits this to migration failures; never upload private.log."""
    if not migration_environment(environment):
        return False
    receipt = normalized_receipt(path)
    output = Path(private) / "migration-diagnostics-upload.json"
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(receipt, stream, separators=(",", ":"))
    destination = (f"gs://{PROJECT}_cloudbuild/migration-diagnostics/"
                   f"{environment['GITHUB_RUN_ID']}/{environment['GITHUB_RUN_ATTEMPT']}.log")
    return upload_private_file(output, destination, environment, "application/json")


def sanitized_build_error(data, environment):
    """Second redaction pass over the dedicated bounded build-submit channel."""
    value = data.decode("utf-8", errors="ignore")
    sensitive_name = re.compile(r"TOKEN|PASSWORD|PASSWD|SECRET|API.?KEY|PRIVATE.?KEY|ACCESS.?KEY|CREDENTIAL|CREDS|CERTIFICATE|AUTHORIZATION", re.I)
    secrets = sorted({v for k, v in environment.items() if sensitive_name.search(k) and isinstance(v, str) and v}, key=len, reverse=True)
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")

    def controls(text):
        text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        return "".join(c for c in text if c == "\n" or not unicodedata.category(c).startswith("C"))

    value = controls(value)
    for secret in secrets:
        normalized = controls(secret)
        if normalized:
            value = value.replace(normalized, "[REDACTED]")
    value = re.sub(r"-----BEGIN [^-]+-----[\s\S]*?(?:-----END [^-]+-----|$)", "[REDACTED]", value)
    value = re.sub(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]+", "[REDACTED]", value, flags=re.I)
    value = re.sub(r"\b(?:ya29\.[A-Za-z0-9._~\-]+|eyJ[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+){1,2}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|AIza[A-Za-z0-9_\-]{16,}|(?:AKIA|ASIA)[A-Z0-9]{16}|sk-[A-Za-z0-9_\-]{8,})", "[REDACTED]", value)
    value = re.sub(r"([a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1[REDACTED]@", value, flags=re.I)
    assignment = r'''(["']?[A-Za-z0-9_.-]*(?:token|password|passwd|secret|api[_-]?key|credential|private[_-]?key|access[_-]?key|authorization|x-goog-signature)[A-Za-z0-9_.-]*["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)'''
    value = re.sub(assignment, r"\1[REDACTED]", value, flags=re.I)
    # Truncate after redaction; ignore an incomplete final UTF-8 code point.
    return value.encode("utf-8")[:BUILD_ERROR_LIMIT].decode("utf-8", errors="ignore").encode("utf-8")


def upload_failure_build_error(path, private, environment, receipt_path):
    """Only the exact controller-created channel can become a private object."""
    if not migration_environment(environment):
        return False
    private, path, receipt_path = Path(private), Path(path), Path(receipt_path)
    if (private.is_symlink() or not private.is_dir() or private.stat().st_mode & 0o077 or
            path != private / BUILD_ERROR_FILE or receipt_path != private / "migration-diagnostics.json"):
        return False
    receipt = normalized_receipt(receipt_path)
    if (receipt["phase"] not in BUILD_PHASES or receipt["state"] != "failure" or
            receipt.get("command", {}).get("tool") != "gcloud" or receipt["command"]["exitCode"] == 0):
        return False
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or
                not 0 < info.st_size <= BUILD_ERROR_LIMIT):
            return False
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(BUILD_ERROR_LIMIT + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not data or len(data) > BUILD_ERROR_LIMIT:
        return False
    redacted = sanitized_build_error(data, environment)
    if not redacted.strip():
        return False
    output = private / "migration-build-error-upload.log"
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(redacted)
    destination = (f"gs://{PROJECT}_cloudbuild/migration-diagnostics/"
                   f"{environment['GITHUB_RUN_ID']}/{environment['GITHUB_RUN_ATTEMPT']}.build-submit.stderr.log")
    return upload_private_file(output, destination, environment, "text/plain; charset=utf-8")
