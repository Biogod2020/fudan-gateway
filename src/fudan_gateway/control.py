import asyncio
import json
import os
import platform
import stat
import subprocess
import sys
from pathlib import Path

from .session import NeedsLogin, WebVPNSession
from .storage import broker_socket


async def request(root: Path, op: str, *, timeout=35, **options):
    socket = broker_socket(root)
    reader = writer = None
    for attempt in range(35):
        if socket.exists():
            info = socket.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("Unexpected broker socket owner or type.")
        try:
            reader, writer = await asyncio.open_unix_connection(socket)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if op == "stop":
                return {"state": "stopped"}
            if attempt == 0:
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(root / "broker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "ab") as log:
                    managed = False
                    if platform.system() == "Darwin":
                        result = subprocess.run(
                            [
                                "launchctl",
                                "kickstart",
                                f"gui/{os.getuid()}/com.biogod.fudan-gateway.broker",
                            ],
                            capture_output=True,
                            check=False,
                        )
                        managed = result.returncode == 0
                    if not managed:
                        subprocess.Popen(
                            [sys.executable, "-m", "fudan_gateway", "broker"],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            env={**os.environ, "FUDANGATEWAY_STATE_DIR": str(root)},
                            start_new_session=True,
                        )
            await asyncio.sleep(0.2)
    if writer is None:
        raise RuntimeError("Login assistant did not start.")
    try:
        writer.write(json.dumps({"op": op, **options}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout))
        if not response["ok"]:
            result = response["result"]
            if result["state"] == "needs_login":
                raise NeedsLogin(result.get("detail", "UIS login required."))
            raise RuntimeError(
                "Login assistant unavailable: " + result.get("error_type", "unknown")
            )
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


async def ensure_session(root: Path, force=False):
    await request(root, "ensure", force=force)
    return WebVPNSession.load(root / "session.json")
