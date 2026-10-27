import asyncio
import json
import stat

import pytest

from fudan_gateway import broker
from fudan_gateway.storage import broker_socket, write_private


@pytest.mark.asyncio
async def test_private_broker_redacts_browser_errors_and_shuts_down(tmp_path, monkeypatch):
    class FakeBrowser:
        def __init__(self, root, config):
            pass

        async def ensure(self, **options):
            raise ValueError("callback?ticket=pretend-sensitive-value")

        async def close(self):
            pass

    monkeypatch.setattr(broker, "BrowserManager", FakeBrowser)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    write_private(tmp_path / "config.json", {"mode": "webvpn"})
    task = asyncio.create_task(broker.serve(tmp_path))
    socket = broker_socket(tmp_path)
    for _ in range(100):
        if socket.exists():
            break
        await asyncio.sleep(0.01)
    assert stat.S_IMODE(socket.stat().st_mode) == 0o600

    async def call(op):
        reader, writer = await asyncio.open_unix_connection(socket)
        writer.write(json.dumps({"op": op}).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), 1)
        writer.close()
        await writer.wait_closed()
        return data

    response = await call("ensure")
    assert b"pretend-sensitive-value" not in response
    assert json.loads(response)["result"]["error_type"] == "ValueError"
    assert b"pretend-sensitive-value" not in (tmp_path / "status.json").read_bytes()
    await call("stop")
    await asyncio.wait_for(task, 2)
    assert not socket.exists()
    if socket.parent != tmp_path:
        socket.parent.rmdir()


@pytest.mark.asyncio
async def test_broker_never_replaces_an_unrelated_socket_path(tmp_path):
    write_private(tmp_path / "config.json", {"mode": "webvpn"})
    path = broker_socket(tmp_path)
    path.write_text("User-owned file")
    with pytest.raises(ValueError, match="Unexpected"):
        await broker.serve(tmp_path)
    assert path.read_text() == "User-owned file"
    path.unlink()
    if path.parent != tmp_path:
        path.parent.rmdir()
