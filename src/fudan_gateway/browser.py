"""One per-device browser context owns UIS login and WebVPN renewal."""

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .session import ORIGIN, NeedsLogin, WebVPNSession
from .storage import machine_id, read_private, write_private

AUTH_HOSTS = {"id.fudan.edu.cn", "webvpn.fudan.edu.cn"}


class BrowserManager:
    def __init__(self, root: Path, config: dict):
        self.root = root
        self.config = config
        self.context = None
        self.playwright = None
        self.lock = asyncio.Lock()
        self.checked_at = 0.0

    async def start(self):
        if self.context is not None:
            return
        if self.playwright is not None:
            await self.playwright.stop()
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Install the browser extra: uv sync --extra browser.") from None
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.root / "browser"),
            channel="chrome",
            headless=False,
            viewport={"width": 1100, "height": 800},
            args=["--no-proxy-server", "--no-first-run", "--window-size=1100,800"],
        )
        self.context.on("close", lambda: setattr(self, "context", None))
        state_file = self.root / "browser-state.json"
        if state_file.exists():
            state = read_private(state_file)
            if state.get("machine_id") == machine_id():
                stored = state["storage_state"]
                cookies = [
                    c for c in stored.get("cookies", []) if c["domain"].lstrip(".") in AUTH_HOSTS
                ]
                await self.context.add_cookies(cookies)
                origins = [
                    o
                    for o in stored.get("origins", [])
                    if urlsplit(o["origin"]).hostname in AUTH_HOSTS
                ]
                script = (
                    "for(const entry of "
                    + json.dumps(origins)
                    + """){if(location.origin===entry.origin){
                    for(const x of entry.localStorage)localStorage.setItem(x.name,x.value);
                }}"""
                )
                await self.context.add_init_script(script)

    async def save_browser(self):
        state = await self.context.storage_state()
        state["cookies"] = [c for c in state["cookies"] if c["domain"].lstrip(".") in AUTH_HOSTS]
        state["origins"] = [
            o for o in state["origins"] if urlsplit(o["origin"]).hostname in AUTH_HOSTS
        ]
        write_private(
            self.root / "browser-state.json",
            {
                "machine_id": machine_id(),
                "storage_state": state,
            },
        )

    async def ensure(self, *, force=False, interactive=False, credentials=None):
        async with self.lock:
            session_file = self.root / "session.json"
            if not force and session_file.exists():
                try:
                    current = WebVPNSession.load(session_file)
                    if time.monotonic() - self.checked_at < 90:
                        return current
                    code, _, body = await current.get(
                        self.config.get("health_resource", "https://library.fudan.edu.cn/")
                    )
                    if code == 200 and "复旦大学图书馆" in body.decode("utf-8", "replace"):
                        self.checked_at = time.monotonic()
                        return current
                except (NeedsLogin, OSError, ValueError):
                    pass
            await self.start()
            if force:
                await self.context.clear_cookies(domain=re.compile(r"^\.?webvpn\.fudan\.edu\.cn$"))
            page = await self.context.new_page()
            keep_page = interactive
            try:
                await page.goto(ORIGIN + "/", wait_until="domcontentloaded", timeout=25000)
                deadline = time.monotonic() + (180 if interactive else 25)
                submitted = False
                followed_cas = False
                last_stage = None
                while time.monotonic() < deadline:
                    u = urlsplit(page.url)
                    stage = (u.hostname, u.path)
                    if stage != last_stage:
                        write_private(
                            self.root / "auth-stage.json",
                            {
                                "host": u.hostname,
                                "path": u.path,
                                "password_submitted": submitted,
                                "updated_at": time.time(),
                            },
                        )
                        last_stage = stage
                    if u.hostname == "webvpn.fudan.edu.cn" and u.path == "/":
                        ready = await page.evaluate(
                            """()=>!!window.__NUXT__?.state?.user?.userInfo?.wrdvpnKey"""
                        )
                        if ready:
                            expected = self.config.get("expected_account")
                            if (
                                expected
                                and str(expected) not in await page.locator("body").inner_text()
                            ):
                                raise NeedsLogin("UIS logged into a different account.")
                            routing = await page.evaluate(
                                "()=>{const u=window.__NUXT__.state.user.userInfo;"
                                "return {key:u.wrdvpnKey,iv:u.wrdvpnIV}}"
                            )
                            cookies = await self.context.cookies(ORIGIN)
                            data = {
                                "origin": ORIGIN,
                                "machine_id": machine_id(),
                                "created_at": time.time(),
                                "auth_method": "password"
                                if submitted
                                else ("uis_sso" if followed_cas else "existing_webvpn_session"),
                                "url_rewrite": routing,
                                "user_agent": await page.evaluate("navigator.userAgent"),
                                "cookies": [
                                    c
                                    for c in cookies
                                    if c["domain"].lstrip(".") == "webvpn.fudan.edu.cn"
                                ],
                            }
                            write_private(session_file, data)
                            await self.save_browser()
                            self.checked_at = time.monotonic()
                            return WebVPNSession.load(session_file)
                    if u.hostname == "webvpn.fudan.edu.cn" and not followed_cas:
                        cas = page.locator("#cas-login")
                        if await cas.count():
                            href = await cas.get_attribute("href")
                            followed_cas = True
                            await page.goto(
                                urljoin(page.url, href),
                                wait_until="domcontentloaded",
                                timeout=25000,
                            )
                            continue
                    if u.hostname == "id.fudan.edu.cn":
                        password = page.locator('#login-password, input[name="password"]')
                        if await password.count():
                            if credentials and not submitted:
                                await page.evaluate(
                                    """data=>{
                                    const set=Object.getOwnPropertyDescriptor(
                                        HTMLInputElement.prototype,'value').set;
                                    for(const name of ['username','password']){
                                        const selector='input[name="'+name+'"]';
                                        const field=document.querySelector(selector);
                                        if(!field)throw new Error('UIS field unavailable');
                                        set.call(field,data[name]);
                                        field.dispatchEvent(new Event('input',{bubbles:true}));
                                        field.dispatchEvent(new Event('change',{bubbles:true}));
                                    }
                                }""",
                                    credentials,
                                )
                                submitted = True
                                write_private(
                                    self.root / "auth-stage.json",
                                    {
                                        "host": u.hostname,
                                        "path": u.path,
                                        "password_submitted": True,
                                        "updated_at": time.time(),
                                    },
                                )
                                await page.get_by_role(
                                    "button", name=re.compile(r"^\s*(登录|Sign in)\s*$", re.I)
                                ).evaluate("e=>e.click()")
                            elif not interactive:
                                raise NeedsLogin(
                                    "UIS requires a new login. Run fudangateway login."
                                )
                        if interactive:
                            await page.bring_to_front()
                    await asyncio.sleep(0.2)
                raise NeedsLogin("Complete the UIS login in the FudanGateway browser window.")
            finally:
                credentials = None
                if not keep_page and not page.is_closed():
                    await page.close()

    async def open(self, target=None):
        session = await self.ensure(interactive=True)
        await self.start()
        page = await self.context.new_page()
        url = session.url_for(target) if target else ORIGIN + "/"
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.bring_to_front()

    async def close(self):
        context = self.context
        if context is not None:
            try:
                with contextlib.suppress(Exception):
                    await self.save_browser()
            finally:
                await context.close()
        if self.playwright is not None:
            await self.playwright.stop()
