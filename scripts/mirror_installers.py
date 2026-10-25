"""Copy verified desktop installers without exposing private release content."""

import hashlib
import json
import shutil
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from release_auth import BEARER_TOKEN, REPOSITORIES, github_request, stop_process

PUBLIC_REPOSITORY = "wonderl17/edu-agent-release"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 1800


class MirrorInstallersError(Exception):
    """A fixed diagnostic that never contains private metadata or credentials."""


def _release(value, tag, *, published):
    if (not isinstance(value, dict) or value.get("tag_name") != tag
            or type(value.get("id")) is not int or value["id"] <= 0
            or type(value.get("draft")) is not bool
            or value.get("prerelease") is not False
            or (published and value["draft"])
            or not isinstance(value.get("assets"), list)):
        raise ValueError()
    return value


def _assets(release, names, *, complete, public=False):
    result = {}
    for asset in release["assets"]:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError()
        name = asset["name"]
        if name not in names:
            if public:
                raise ValueError()
            continue
        if (name in result or type(asset.get("size")) is not int
                or not 0 < asset["size"] <= MAX_ASSET_BYTES
                or not isinstance(asset.get("digest"), str)
                or DIGEST.fullmatch(asset["digest"]) is None
                or asset.get("state") != "uploaded"):
            raise ValueError()
        result[name] = (asset["size"], asset["digest"])
    if complete and set(result) != set(names):
        raise ValueError()
    return result


def _run_gh(arguments, token, directory):
    # Give gh only its own token and a fresh config directory. Signing/storage,
    # App credentials and the other repository token never reach the process.
    environment = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
                              "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")}
    environment.update({"GH_TOKEN": token, "GH_HOST": "github.com",
                        "GH_CONFIG_DIR": str(directory / "gh-config"),
                        "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    process = subprocess.Popen(["gh", *arguments], cwd=directory, env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        if process.wait(timeout=COMMAND_TIMEOUT_SECONDS):
            raise ValueError()
    except BaseException:
        stop_process(process)
        raise


def _verify_file(file, expected):
    metadata = file.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected[0]:
        raise ValueError()
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    if "sha256:" + digest.hexdigest() != expected[1]:
        raise ValueError()


def _local_installer(directory, temporary, name, output, expected):
    """Reuse only bytes authenticated against the published private asset digest."""
    if directory is None:
        return False
    try:
        directory = Path(directory)
        if not directory.is_relative_to(temporary) or directory.resolve(strict=True) != directory:
            return False
        channel = next(value for value in ("mac-arm64", "mac-x64", "win-x64") if name.endswith(value + (".exe" if value == "win-x64" else ".pkg")))
        metadata = directory / ("assets." + channel + ".json")
        info = metadata.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            return False
        entries = json.loads(metadata.read_text())
        matches = [x["RelativeFileName"] for x in entries if x.get("Type") == "Installer"]
        if len(matches) != 1 or re.fullmatch(r"[A-Za-z0-9._-]+", matches[0]) is None or channel not in matches[0]:
            return False
        candidate = directory / matches[0]
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected[0]:
                return False
            with output.open("xb") as target:
                shutil.copyfileobj(source, target, 8 * 1024 * 1024)
        _verify_file(output, expected)
        return True
    except Exception:
        output.unlink(missing_ok=True)
        return False


def _destination_release(token, path, tag):
    release = github_request(token, "GET", path + "/tags/" + tag, allow_not_found=True)
    if release is not None:
        return release
    # The tag endpoint returns published releases. An interrupted mirror can
    # leave a draft; list with the write token so it can be resumed safely.
    matches = []
    for page in range(1, 11):
        releases = github_request(token, "GET", path + f"?per_page=100&page={page}")
        if not isinstance(releases, list) or len(releases) > 100:
            raise ValueError()
        matches.extend(item for item in releases if isinstance(item, dict) and item.get("tag_name") == tag)
        if len(matches) > 1:
            raise ValueError()
        if len(releases) < 100:
            return matches[0] if matches else None
    # Do not create a potentially duplicate draft after a truncated search.
    raise ValueError()


def mirror_installers(*, source_repository, version, source_token, public_repository,
                      public_sha, public_token, temporary, local_release_dir=None):
    """Mirror only the three original installers; never rebuild or overwrite."""
    try:
        if (source_repository not in REPOSITORIES or public_repository != PUBLIC_REPOSITORY
                or not isinstance(version, str) or len(version) > 100
                or VERSION.fullmatch(version) is None
                or not isinstance(public_sha, str) or re.fullmatch(r"[0-9a-f]{40}", public_sha) is None
                or any(not isinstance(token, str) or BEARER_TOKEN.fullmatch(token) is None
                       for token in (source_token, public_token))):
            raise ValueError()
        temporary = Path(temporary).resolve(strict=True)
        if not temporary.is_dir():
            raise ValueError()
        tag = "desktop-v" + version
        names = tuple("ShukeZhinao-" + version + "-" + platform
                      for platform in ("mac-arm64.pkg", "mac-x64.pkg", "win-x64.exe"))
        source = _release(github_request(source_token, "GET",
                          "/repos/" + source_repository + "/releases/tags/" + tag), tag, published=True)
        expected = _assets(source, names, complete=True)
        destination_path = "/repos/" + public_repository + "/releases"
        destination = _destination_release(public_token, destination_path, tag)
        existing = {}
        if destination is not None:
            _release(destination, tag, published=False)
            existing = _assets(destination, names, complete=False, public=True)
            if any(expected[name] != value for name, value in existing.items()):
                raise ValueError()
        with tempfile.TemporaryDirectory(prefix="installer-mirror-", dir=temporary) as directory:
            directory = Path(directory)
            directory.chmod(0o700)
            downloads = directory / "installers"
            downloads.mkdir(mode=0o700)
            arguments = ["release", "download", tag, "--repo", source_repository,
                         "--dir", str(downloads)]
            missing_local = [name for name in names if not _local_installer(
                local_release_dir, temporary, name, downloads / name, expected[name])]
            if missing_local:
                for name in missing_local:
                    arguments.extend(("--pattern", name))
                _run_gh(arguments, source_token, directory)
            if {file.name for file in downloads.iterdir()} != set(names):
                raise ValueError()
            for name in names:
                _verify_file(downloads / name, expected[name])
            if destination is None:
                destination = _release(github_request(public_token, "POST", destination_path, {
                    "tag_name": tag, "target_commitish": public_sha,
                    "name": "数科智脑 v" + version,
                    "body": "Desktop installers for macOS arm64, macOS x64, and Windows x64.",
                    "draft": True, "prerelease": False,
                }), tag, published=False)
                if not destination["draft"] or _assets(destination, names, complete=False, public=True):
                    raise ValueError()
            missing = [str(downloads / name) for name in names if name not in existing]
            if missing:
                _run_gh(["release", "upload", tag, *missing, "--repo", public_repository],
                        public_token, directory)
            verified = _release(github_request(public_token, "GET", destination_path + "/" + str(destination["id"])),
                                tag, published=False)
            if verified["id"] != destination["id"] or _assets(verified, names, complete=True, public=True) != expected:
                raise ValueError()
            if verified["draft"]:
                _release(github_request(public_token, "PATCH", destination_path + "/" + str(verified["id"]),
                                        {"draft": False, "make_latest": "legacy"}), tag, published=True)
    except Exception:
        raise MirrorInstallersError("Installer mirror failed") from None
