"""A bounded, allowlisted diagnostic channel; never a private-output reader."""

import os
from collections import deque
from pathlib import Path
import stat
import tempfile


FILE_ENV = "RELEASE_DESKTOP_CHECKPOINT_FILE"
MAX_BYTES = 8192
MAX_RECORDS = 128
STAGES = frozenset((
    "D_ENTRY", "D_TOOLCHAIN", "D_DEPENDENCIES", "D_CHANGELOG", "D_NODE_CHECK",
    "D_VPK_SETUP", "D_WINDOWS_SETUP", "D_MAC_SIGN", "D_PACKAGE", "D_PREFLIGHT",
    "D_WEB_BUILD", "D_VITE", "D_SIDECAR", "D_ELECTRON", "D_LATEX", "D_PDF",
    "D_VPK_DOWNLOAD", "D_VPK_PACK", "D_VPK_DELTA", "D_VERIFY", "D_R2_PUBLISH",
    "D_FINAL_PUBLISH", "D_RESUME", "D_TOKEN_REFRESH",
))
STATES = frozenset(("start", "success", "failure"))


def without_checkpoint_environment(environment):
    return {key: value for key, value in environment.items()
            if not key.upper().startswith("RELEASE_DESKTOP_CHECKPOINT")}


def validated_checkpoint_path(value, runner_temp):
    """Accept only the controller's private regular file, with no symlinks."""
    try:
        path = Path(value)
        parent = path.parent
        if (path.name != "desktop-checkpoints.log" or not parent.name.startswith("release-checkpoints-")
                or parent.parent.resolve(strict=True) != runner_temp.resolve(strict=True)
                or path.is_symlink() or parent.is_symlink()):
            return None
        file_stat, directory_stat = path.stat(), parent.stat()
        if (not stat.S_ISREG(file_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode)
                or stat.S_IMODE(file_stat.st_mode) != 0o600 or stat.S_IMODE(directory_stat.st_mode) != 0o700
                or file_stat.st_uid != os.getuid() or directory_stat.st_uid != os.getuid()):
            return None
        return path.resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return None


def report_checkpoints(path):
    """Public strings are reconstructed exclusively from fixed enumerations."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.getuid() or metadata.st_size > MAX_BYTES):
                return
            data = stream.read(MAX_BYTES)
        # Ignore an incomplete trailing record, even when it resembles a valid one.
        data = data[:data.rfind(b"\n") + 1]
        records = deque(maxlen=MAX_RECORDS)
        for line in data.splitlines():
            parts = line.split(b" ")
            if len(parts) != 2:
                continue
            try:
                stage, state = (part.decode("ascii") for part in parts)
            except UnicodeDecodeError:
                continue
            if stage in STAGES and state in STATES:
                records.append((stage, state))
        for stage, state in records:
            print(f"Desktop checkpoint: {stage} {state}.", flush=True)
    except OSError:
        # Diagnostics cannot replace the private command's real outcome.
        pass


class CheckpointFile:
    def __init__(self, runner_temp):
        self.directory = tempfile.TemporaryDirectory(prefix="release-checkpoints-", dir=runner_temp)
        try:
            directory = Path(self.directory.name)
            directory.chmod(0o700)
            self.path = directory / "desktop-checkpoints.log"
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except OSError:
            self.close()
            raise

    def close(self):
        try:
            self.directory.cleanup()
        except OSError:
            pass
