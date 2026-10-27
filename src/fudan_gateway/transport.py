"""Native byte-stream transport through the user's authorized WebVPN and campus relay."""

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientWSTimeout, WSMsgType, WSServerHandshakeError

from .auth import HEADER, sign_request
from .relay import read_key
from .session import GatewayError, NeedsLogin, WebVPNSession


async def open_tunnel(session: WebVPNSession, relay: dict, target: str):
    if target not in relay["targets"]:
        raise ValueError("Target is not configured.")
    path = f"/v1/connect/{target}"
    key = read_key(Path(relay["key_file"]))
    url = session.url_for(relay["url"].rstrip("/") + path, websocket=True)
    client = session.client()
    try:
        ws = await client.ws_connect(
            url,
            headers={HEADER: sign_request(key, "GET", path), "Origin": session.data["origin"]},
            timeout=ClientWSTimeout(ws_receive=None, ws_close=5),
            heartbeat=20,
            compress=0,
        )
    except WSServerHandshakeError as error:
        await client.close()
        destination = urlsplit(str(error.request_info.real_url))
        if (
            error.status in (301, 302, 303)
            or destination.path.startswith("/login")
            or destination.hostname == "id.fudan.edu.cn"
        ):
            raise NeedsLogin("WebVPN authorization must be renewed through UIS.") from None
        raise GatewayError(f"WebVPN WebSocket handshake returned HTTP {error.status}.") from None
    except BaseException:
        await client.close()
        raise
    return client, ws


async def probe_banner(session: WebVPNSession, relay: dict, target: str) -> str:
    client, ws = await open_tunnel(session, relay, target)
    try:
        msg = await asyncio.wait_for(ws.receive(), 12)
        if msg.type != WSMsgType.BINARY or not msg.data.startswith(b"SSH-"):
            raise RuntimeError("Relay did not return a real SSH banner.")
        return msg.data.split(b"\n", 1)[0].decode("ascii", "replace").strip()
    finally:
        await ws.close()
        await client.close()


async def connect_stdio(session: WebVPNSession, relay: dict, target: str):
    client, ws = await open_tunnel(session, relay, target)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    stdin_transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    async def upload():
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    async def download():
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                view = memoryview(message.data)
                while view:
                    view = view[os.write(sys.stdout.fileno(), view) :]
            else:
                break

    tasks = {asyncio.create_task(upload()), asyncio.create_task(download())}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stdin_transport.close()
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        await ws.close()
        await client.close()
