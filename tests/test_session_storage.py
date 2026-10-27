import stat

import pytest

from fudan_gateway.session import NeedsLogin, WebVPNSession
from fudan_gateway.storage import machine_id, read_private, write_private


def session_data():
    return {
        "machine_id": machine_id(),
        "origin": "https://webvpn.fudan.edu.cn",
        "url_rewrite": {"key": "0123456789abcdef", "iv": "fedcba9876543210"},
        "user_agent": "FixtureAgent",
        "cookies": [
            {
                "domain": "webvpn.fudan.edu.cn",
                "name": "fixture",
                "value": "not-a-live-credential",
                "path": "/",
                "secure": True,
            }
        ],
    }


def test_private_atomic_state_and_cross_device_rejection(tmp_path):
    p = tmp_path / "session.json"
    data = session_data()
    write_private(p, data)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert read_private(p) == data
    data["machine_id"] = "a-different-device"
    write_private(p, data)
    with pytest.raises(NeedsLogin, match="another device"):
        WebVPNSession.load(p)
    p.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_private(p)


def test_websocket_uses_ws_resource_prefix_not_http():
    data = session_data()
    data["url_rewrite"]["iv"] = "0123456789abcdef"
    s = WebVPNSession(data)
    plain = s.url_for("http://relay.example:1234/v1/connect/server")
    ws = s.url_for("http://relay.example:1234/v1/connect/server", websocket=True)
    assert plain.startswith("https://webvpn.fudan.edu.cn/http-1234/")
    assert ws.startswith("wss://webvpn.fudan.edu.cn/ws-1234/")
    assert plain.split("/", 4)[4] == ws.split("/", 4)[4]
    secure = s.url_for("https://relay.example:443/x", websocket=True)
    assert secure.startswith("wss://webvpn.fudan.edu.cn/wss-443/")
    with pytest.raises(ValueError, match="credentials"):
        s.url_for("https://user:password@example.invalid/")
