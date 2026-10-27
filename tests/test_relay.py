import asyncio
import os

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from fudan_gateway.auth import HEADER, sign_request
from fudan_gateway.relay import create_app

KEY = b"local-test-key-material-0000000000"


@pytest.mark.asyncio
async def test_authorized_binary_tunnel_and_auth_controls():
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    tcp = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    app = create_app(KEY, {"echo": {"host": "127.0.0.1", "port": port}})
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as client:
            url = str(server.make_url("/v1/connect/echo"))
            async with client.get(url) as response:
                assert response.status == 401
            path = "/v1/connect/echo"
            header = sign_request(KEY, "GET", path)
            async with client.ws_connect(url, headers={HEADER: header}) as ws:
                payload = bytes(range(256)) * 257 + os.urandom(4096)
                await ws.send_bytes(payload)
                received = bytearray()
                while len(received) < len(payload):
                    received.extend((await asyncio.wait_for(ws.receive(), 3)).data)
                assert bytes(received) == payload
            async with client.get(url, headers={HEADER: header}) as response:
                assert response.status == 401
            path = "/v1/connect/forbidden"
            async with client.get(
                str(server.make_url(path)), headers={HEADER: sign_request(KEY, "GET", path)}
            ) as response:
                assert response.status == 403
    finally:
        await server.close()
        tcp.close()
        await tcp.wait_closed()


@pytest.mark.asyncio
async def test_shutdown_closes_active_tunnel_without_waiting_for_idle_tcp():
    async def hold(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    tcp = await asyncio.start_server(hold, "127.0.0.1", 0)
    app = create_app(KEY, {"hold": {"host": "127.0.0.1", "port": tcp.sockets[0].getsockname()[1]}})
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as client:
        path = "/v1/connect/hold"
        ws = await client.ws_connect(
            str(server.make_url(path)), headers={HEADER: sign_request(KEY, "GET", path)}
        )
        await asyncio.wait_for(server.close(), 3)
        message = await asyncio.wait_for(ws.receive(), 2)
        assert message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        await ws.close()
    tcp.close()
    await tcp.wait_closed()
