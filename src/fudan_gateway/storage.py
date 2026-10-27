"""Private, device-bound local state. No credentials belong in the repository."""

import hashlib
import json
import os
import platform
import stat
import tempfile
from pathlib import Path


def runtime_dir() -> Path:
    if override := os.environ.get("FUDANGATEWAY_STATE_DIR"):
        return Path(override).expanduser().resolve()
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support/FudanGateway/webvpn"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "fudan-gateway"


def machine_id() -> str:
    identity = f"{platform.system()}:{platform.node()}:{os.getuid()}"
    return hashlib.sha256(identity.encode()).hexdigest()


def broker_socket(root: Path) -> Path:
    socket = root / "broker.sock"
    if len(os.fsencode(socket)) < 96:
        return socket
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    directory = Path("/tmp") / f"fudan-gateway-{os.getuid()}-{digest}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Unexpected private socket directory.")
    return directory / "broker.sock"


def read_private(path: Path) -> dict:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("State must be a regular file owned by the current user.")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("State file permissions must be 0600.")
    return json.loads(path.read_text())


def write_private(path: Path, data: dict):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False)
            output.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
