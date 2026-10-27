"""Private Unix-socket login assistant; credentials never use a public listener."""

import asyncio
import fcntl
import json
import os
import signal
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from .browser import BrowserManager
from .session import NeedsLogin
from .storage import broker_socket, read_private, write_private


async def serve(root: Path):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (root / "broker.lock").open("a") as lock:
        os.chmod(root / "broker.lock", 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        socket = broker_socket(root)
        if socket.exists():
            info = socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Unexpected broker socket path.")
            socket.unlink()
        config = read_private(root / "config.json")
        browser = BrowserManager(root, config)
        stop = asyncio.Event()

        async def handle(reader, writer):
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(), 5))
                op = request.get("op")
                if op in ("ensure", "login"):
                    session = await browser.ensure(
                        force=request.get("force", False),
                        interactive=op == "login",
                        credentials=request.get("credentials"),
                    )
                    result = {
                        "state": "ready",
                        "mode": "webvpn",
                        "checked_at": time.time(),
                        "login_method": session.data.get("auth_method", "restored"),
                    }
                    write_private(root / "status.json", result)
                elif op == "open":
                    await browser.open(request.get("url"))
                    result = {"state": "ready", "mode": "webvpn"}
                elif op == "stop":
                    stop.set()
                    result = {"state": "stopping"}
                elif op == "diagnostics":
                    result = {"browser_running": browser.context is not None, "pages": []}
                    if browser.context is not None:
                        result["pages"] = [
                            {"host": urlsplit(p.url).hostname, "path": urlsplit(p.url).path}
                            for p in browser.context.pages
                        ]
                else:
                    raise ValueError("Unknown broker operation.")
                response = {"ok": True, "result": result}
            except NeedsLogin as error:
                result = {"state": "needs_login", "mode": "webvpn", "detail": str(error)}
                write_private(root / "status.json", result)
                response = {"ok": False, "result": result}
            except Exception as error:
                # Playwright exceptions may contain single-use CAS query tokens.
                result = {"state": "unavailable", "error_type": type(error).__name__}
                write_private(root / "status.json", result)
                response = {"ok": False, "result": result}
            writer.write(json.dumps(response).encode() + b"\n")
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, path=socket, limit=65536)
        os.chmod(socket, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            async with server:
                await stop.wait()
        finally:
            await browser.close()
            socket.unlink(missing_ok=True)
            write_private(root / "status.json", {"state": "stopped", "mode": "webvpn"})
