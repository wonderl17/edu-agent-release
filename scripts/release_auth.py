"""Short-lived GitHub App authentication; never log HTTP bodies or credentials."""

import base64
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORIES = frozenset(("shukezhinao/edu-agent", "wonderl17/edu-agent"))
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/\-]+=*")


class ReleaseAuthError(Exception):
    """An intentionally fixed message safe for callers to report."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_arguments, **_keywords):
        # These JSON endpoints do not require redirects. Never forward a bearer
        # token to another host, including after a compromised API response.
        return None


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def github_request(token, method, path, body=None):
    try:
        if not isinstance(token, str) or BEARER_TOKEN.fullmatch(token) is None:
            raise ValueError()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError()
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or len(path) > 8192 or re.search(r"[\s\\#]", path):
            raise ValueError()
        request = Request("https://api.github.com" + path,
                          data=None if body is None else json.dumps(body).encode(),
                          method=method, headers={
                              "Authorization": "Bearer " + token,
                              "Accept": "application/vnd.github+json",
                              "X-GitHub-Api-Version": "2022-11-28",
                              "User-Agent": "edu-agent-release",
                              "Content-Type": "application/json",
                          })
        with build_opener(NoRedirect()).open(request, timeout=60) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError()
        return json.loads(data) if data else None
    except Exception:
        # HTTPError, JSON errors and TLS failures can contain private response
        # data or request details. Never expose those exceptions to a caller.
        raise ReleaseAuthError("GitHub API request failed") from None


def app_jwt():
    app_id = os.environ.get("RELEASE_APP_ID", "")
    private_key = os.environ.get("RELEASE_APP_PRIVATE_KEY", "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", app_id) or "PRIVATE KEY-----" not in private_key:
        raise ReleaseAuthError("App authentication unavailable")
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    now = int(time.time())
    data = b".".join((encode(b'{"alg":"RS256","typ":"JWT"}'),
                      encode(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())))
    try:
        with tempfile.TemporaryDirectory(prefix="release-auth-", dir=os.environ.get("RUNNER_TEMP")) as directory:
            Path(directory).chmod(0o700)
            key = Path(directory) / "key.pem"
            descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(private_key)
            environment = {key: value for key, value in os.environ.items()
                           if key != "RELEASE_APP_PRIVATE_KEY" and not re.search(r"DEBUG|TRACE", key.upper())}
            process = subprocess.Popen(["openssl", "dgst", "-sha256", "-sign", str(key)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, start_new_session=True)
            try:
                signature, _ = process.communicate(input=data, timeout=30)
            except BaseException:
                stop_process(process)
                raise
            if process.returncode or not signature:
                raise ReleaseAuthError("App authentication failed")
        return (data + b"." + encode(signature)).decode()
    except Exception:
        raise ReleaseAuthError("App authentication failed") from None


def installation_token(repository, permission="read"):
    if repository not in REPOSITORIES or permission not in ("read", "write"):
        raise ReleaseAuthError("Invalid repository or permission")
    try:
        jwt = app_jwt()
        installation = github_request(jwt, "GET", "/repos/" + repository + "/installation")
        installation_id = installation["id"]
        if type(installation_id) is not int or installation_id <= 0:
            raise ValueError()
        result = github_request(jwt, "POST", "/app/installations/" + str(installation_id) + "/access_tokens", {
            "repositories": [repository.split("/")[1]],
            "permissions": {"contents": permission},
        })
        token = result["token"]
        if not isinstance(token, str) or BEARER_TOKEN.fullmatch(token) is None:
            raise ValueError()
        return token
    except Exception:
        raise ReleaseAuthError("Installation authentication failed") from None


def main(arguments=None):
    # Only the private executor calls this mode, capturing stdout in memory.
    arguments = sys.argv[1:] if arguments is None else arguments
    try:
        if len(arguments) != 2 or arguments[0] != "--token-for":
            raise ReleaseAuthError("Invalid command")
        sys.stdout.write(installation_token(arguments[1], "write"))
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    def cancelled(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    raise SystemExit(main())
