import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from fudan_gateway.session import GatewayError, NeedsLogin
from fudan_gateway.transport import open_tunnel


@pytest.mark.asyncio
async def test_login_redirect_requires_renewal_but_relay_denial_does_not(tmp_path):
    async def redirect(request):
        raise web.HTTPFound("/login?ticket=synthetic-not-a-real-ticket")

    async def login(request):
        return web.Response(text="Login", content_type="text/html")

    async def denied(request):
        raise web.HTTPUnauthorized()

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/login", login)
    app.router.add_get("/denied", denied)
    server = TestServer(app)
    await server.start_server()
    key = tmp_path / "relay.key"
    key.write_text("12" * 32)
    key.chmod(0o600)
    relay = {"url": "http://fixture.invalid", "key_file": str(key), "targets": ["test"]}

    class Session:
        data = {"origin": "http://fixture.invalid"}
        route = "/redirect"

        def url_for(self, target, websocket=False):
            return str(server.make_url(self.route))

        def client(self):
            return aiohttp.ClientSession()

    try:
        session = Session()
        with pytest.raises(NeedsLogin) as failure:
            await open_tunnel(session, relay, "test")
        assert "synthetic-not-a-real-ticket" not in str(failure.value)
        session.route = "/denied"
        with pytest.raises(GatewayError, match="HTTP 401") as failure:
            await open_tunnel(session, relay, "test")
        assert not isinstance(failure.value, NeedsLogin)
    finally:
        await server.close()
