"""WebVPN resource mapping and authenticated HTTP requests, without global routing."""

from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from cryptography.hazmat.decrepit.ciphers.modes import CFB
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from yarl import URL

from .storage import machine_id, read_private

ORIGIN = "https://webvpn.fudan.edu.cn"


class GatewayError(RuntimeError):
    """A deliberately credential-free diagnostic safe to present to the user."""


class NeedsLogin(GatewayError):
    pass


@dataclass
class WebVPNSession:
    data: dict

    @classmethod
    def load(cls, path: Path):
        data = read_private(path)
        if data.get("machine_id") != machine_id():
            raise NeedsLogin("This session belongs to another device. Log in on this device.")
        if data.get("origin") != ORIGIN:
            raise ValueError("Unexpected WebVPN origin.")
        if not data.get("cookies"):
            raise NeedsLogin("Log in through UIS before connecting.")
        return cls(data)

    def url_for(self, target: str, websocket: bool = False) -> str:
        u = urlsplit(target)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("Only HTTP/HTTPS resource URLs are supported.")
        if u.username is not None or u.password is not None:
            raise ValueError("Do not embed credentials in resource URLs.")
        host = u.hostname.encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        key = self.data["url_rewrite"]["key"].encode()
        iv = self.data["url_rewrite"]["iv"].encode()
        cipher = Cipher(algorithms.AES(key), CFB(iv)).encryptor()
        encoded = iv.hex() + (cipher.update(host.encode()) + cipher.finalize()).hex()
        upstream_scheme = {"http": "ws", "https": "wss"}[u.scheme] if websocket else u.scheme
        protocol = upstream_scheme + (f"-{u.port}" if u.port is not None else "")
        path = f"/{protocol}/{encoded}{u.path or '/'}"
        return urlunsplit(
            ("wss" if websocket else "https", "webvpn.fudan.edu.cn", path, u.query, "")
        )

    def client(self) -> aiohttp.ClientSession:
        jar = aiohttp.CookieJar()
        for item in self.data["cookies"]:
            if item["domain"].lstrip(".") != "webvpn.fudan.edu.cn":
                raise ValueError("Cookie outside the WebVPN origin.")
            cookie = SimpleCookie()
            cookie[item["name"]] = item["value"]
            morsel = cookie[item["name"]]
            morsel["domain"] = item["domain"]
            morsel["path"] = item["path"]
            if item.get("secure"):
                morsel["secure"] = True
            jar.update_cookies(cookie, response_url=URL(ORIGIN))
        return aiohttp.ClientSession(
            cookie_jar=jar,
            headers={"User-Agent": self.data["user_agent"]},
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=25),
        )

    async def get(self, target: str) -> tuple[int, dict, bytes]:
        url = self.url_for(target)
        async with self.client() as client:
            for _ in range(10):
                async with client.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = str(URL(url).join(URL(response.headers["Location"])))
                        u = urlsplit(url)
                        if (u.scheme, u.netloc) != ("https", "webvpn.fudan.edu.cn"):
                            raise NeedsLogin("WebVPN authorization must be renewed through UIS.")
                        if u.path.startswith("/login"):
                            raise NeedsLogin("WebVPN authorization has expired.")
                        continue
                    if response.status in (401, 403):
                        raise GatewayError(
                            "Resource authorization was refused; check WebVPN access rights."
                        )
                    return response.status, dict(response.headers), await response.read()
        raise RuntimeError("Too many WebVPN redirects.")
