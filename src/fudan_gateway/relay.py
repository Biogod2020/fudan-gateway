"""User-level relay with signed requests and an explicit destination allowlist."""

import asyncio
import contextlib
import ipaddress
import json
import os
import signal
import stat
from pathlib import Path

from aiohttp import WSMsgType, web

from .auth import HEADER, RequestVerifier

KEY = web.AppKey("verifier", RequestVerifier)
TARGETS = web.AppKey("targets", dict)
SOCKETS = web.AppKey("websockets", set)


def read_key(path: Path) -> bytes:
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Relay key must belong to this user with mode 0600.")
    key = bytes.fromhex(path.read_text().strip())
    if len(key) < 32:
        raise ValueError("Relay key is too short.")
    return key


def create_app(key: bytes, targets: dict) -> web.Application:
    app = web.Application(client_max_size=65536)
    app[KEY] = RequestVerifier(key)
    app[TARGETS] = targets
    app[SOCKETS] = set()

    async def shutdown(application):
        for socket in list(application[SOCKETS]):
            await socket.close(code=1001, message=b"Gateway shutting down")

    app.on_shutdown.append(shutdown)

    async def health(request: web.Request):
        return web.json_response({"service": "fudan-gateway-relay", "version": 1})

    async def connect(request: web.Request):
        if not app[KEY].verify(request.headers.get(HEADER, ""), request.method, request.path):
            raise web.HTTPUnauthorized(text="Relay authorization required.")
        target = app[TARGETS].get(request.match_info["target"])
        if target is None:
            raise web.HTTPForbidden(text="Destination is not allowed.")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(target["host"], int(target["port"])), 10
            )
        except (TimeoutError, OSError):
            raise web.HTTPBadGateway(text="Destination is unavailable.") from None
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1048576, compress=False)
        try:
            await ws.prepare(request)
            app[SOCKETS].add(ws)

            async def downstream():
                while data := await reader.read(65536):
                    await ws.send_bytes(data)

            async def upstream():
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        writer.write(msg.data)
                        await writer.drain()
                    else:
                        break

            tasks = {asyncio.create_task(downstream()), asyncio.create_task(upstream())}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            app[SOCKETS].discard(ws)
            await ws.close()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return ws

    app.router.add_get("/health", health)
    app.router.add_get("/v1/connect/{target}", connect)
    return app


def run(config_path: Path):
    config = json.loads(config_path.read_text())
    host = config["listen"]
    address = ipaddress.ip_address(host)
    if address.is_unspecified or not (address.is_private or address.is_loopback):
        raise ValueError("Relay must bind an explicit private or loopback address.")
    app = create_app(read_key(Path(config["key_file"])), config["targets"])
    if config.get("watch_parent"):
        parent = os.getppid()
        if parent == 1:
            raise RuntimeError("Parent supervision requires a live parent process.")

        async def supervised():
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
            await runner.setup()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                loop.add_signal_handler(sig, stop.set)
            try:
                await web.TCPSite(runner, host, int(config["port"])).start()
                while os.getppid() == parent and not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), 1)
                    except TimeoutError:
                        pass
            finally:
                await runner.cleanup()

        asyncio.run(supervised())
    else:
        web.run_app(
            app,
            host=host,
            port=int(config["port"]),
            access_log=None,
            shutdown_timeout=5,
            print=None,
        )
