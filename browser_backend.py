from __future__ import annotations

import os
import sys
from urllib.parse import quote, urlparse
from typing import Any, Dict, Optional, Sequence


LOCAL_DEPS = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)

try:
    from camoufox.async_api import AsyncCamoufox
except Exception:
    AsyncCamoufox = None  # type: ignore

try:
    from camoufox.sync_api import Camoufox
except Exception:
    Camoufox = None  # type: ignore

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None  # type: ignore

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None  # type: ignore


DEFAULT_BACKEND = os.environ.get("DAEMON_BROWSER_BACKEND", "camoufox")


def normalize_browser_backend(value: Optional[str]) -> str:
    raw = str(value or DEFAULT_BACKEND).strip().lower()
    if raw in {"camoufox", "camou"}:
        return "camoufox"
    if raw in {"chromium", "playwright-chromium", "playwright_chromium", "legacy"}:
        return "chromium"
    if raw == "playwright":
        return "camoufox"
    return "camoufox"


def backend_display_name(value: Optional[str]) -> str:
    backend = normalize_browser_backend(value)
    return "camoufox" if backend == "camoufox" else "playwright_chromium"


def normalize_proxy_url(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if not parsed.hostname or not parsed.port:
        return ""
    host = parsed.hostname
    port = parsed.port
    username = parsed.username or ""
    password = parsed.password or ""
    if username and password:
        return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return f"http://{host}:{port}"


def to_playwright_proxy(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    normalized = normalize_proxy_url(proxy_url)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if not parsed.hostname or not parsed.port:
        return None
    proxy: Dict[str, str] = {"server": f"http://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


class AsyncBrowserRuntime:
    def __init__(
        self,
        *,
        backend: Optional[str] = None,
        headless: bool = True,
        chromium_args: Optional[Sequence[str]] = None,
        camoufox_options: Optional[Dict[str, Any]] = None,
        proxy_url: Optional[str] = None,
    ) -> None:
        self.backend = normalize_browser_backend(backend)
        self.headless = bool(headless)
        self.chromium_args = list(chromium_args or [])
        self.camoufox_options = dict(camoufox_options or {})
        self.proxy_url = normalize_proxy_url(proxy_url)
        self.browser = None
        self._camoufox_cm = None
        self._pw_cm = None
        self._pw = None

    async def launch(self):
        await self.close()
        if self.backend == "camoufox":
            if AsyncCamoufox is None:
                raise RuntimeError("Camoufox is not available. Run setup_daemon.sh to install .deps.")
            options = {"headless": self.headless}
            options.update(self.camoufox_options)
            proxy = to_playwright_proxy(self.proxy_url)
            if proxy:
                options["proxy"] = proxy
            self._camoufox_cm = AsyncCamoufox(**options)
            self.browser = await self._camoufox_cm.__aenter__()
            return self.browser

        if async_playwright is None:
            raise RuntimeError("Playwright is not available. Run setup_daemon.sh to install .deps.")
        self._pw_cm = async_playwright()
        self._pw = await self._pw_cm.__aenter__()
        launch_kwargs: Dict[str, Any] = {"headless": self.headless}
        if self.chromium_args:
            launch_kwargs["args"] = self.chromium_args
        proxy = to_playwright_proxy(self.proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        self.browser = await self._pw.chromium.launch(**launch_kwargs)
        return self.browser

    async def restart(self):
        return await self.launch()

    async def close(self) -> None:
        browser = self.browser
        self.browser = None
        if self._camoufox_cm is not None:
            try:
                await self._camoufox_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._camoufox_cm = None
            return

        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

        if self._pw_cm is not None:
            try:
                await self._pw_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._pw_cm = None
        self._pw = None


class SyncBrowserRuntime:
    def __init__(
        self,
        *,
        backend: Optional[str] = None,
        headless: bool = True,
        chromium_args: Optional[Sequence[str]] = None,
        camoufox_options: Optional[Dict[str, Any]] = None,
        proxy_url: Optional[str] = None,
    ) -> None:
        self.backend = normalize_browser_backend(backend)
        self.headless = bool(headless)
        self.chromium_args = list(chromium_args or [])
        self.camoufox_options = dict(camoufox_options or {})
        self.proxy_url = normalize_proxy_url(proxy_url)
        self.browser = None
        self._camoufox_cm = None
        self._pw = None

    def launch(self):
        self.close()
        if self.backend == "camoufox":
            if Camoufox is None:
                raise RuntimeError("Camoufox is not available. Run setup_daemon.sh to install .deps.")
            options = {"headless": self.headless}
            options.update(self.camoufox_options)
            proxy = to_playwright_proxy(self.proxy_url)
            if proxy:
                options["proxy"] = proxy
            self._camoufox_cm = Camoufox(**options)
            self.browser = self._camoufox_cm.__enter__()
            return self.browser

        if sync_playwright is None:
            raise RuntimeError("Playwright is not available. Run setup_daemon.sh to install .deps.")
        self._pw = sync_playwright().start()
        launch_kwargs: Dict[str, Any] = {"headless": self.headless}
        if self.chromium_args:
            launch_kwargs["args"] = self.chromium_args
        proxy = to_playwright_proxy(self.proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        self.browser = self._pw.chromium.launch(**launch_kwargs)
        return self.browser

    def close(self) -> None:
        if self._camoufox_cm is not None:
            try:
                self._camoufox_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._camoufox_cm = None
            self.browser = None
            return

        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
        self.browser = None

        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
