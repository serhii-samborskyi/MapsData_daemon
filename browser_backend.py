from __future__ import annotations

import os
import sys
import warnings
from urllib.parse import quote, unquote, urlparse
from typing import Any, Dict, List, Optional, Sequence


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
    from camoufox.exceptions import NotInstalledGeoIPExtra
except Exception:
    NotInstalledGeoIPExtra = None  # type: ignore

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None  # type: ignore

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None  # type: ignore


DEFAULT_BACKEND = os.environ.get("DAEMON_BROWSER_BACKEND", "camoufox")
DEFAULT_BLOCKED_RESOURCE_EXTENSIONS = [
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".avif", ".apng",
    ".mp4", ".webm", ".mov", ".m4v", ".avi", ".m3u8", ".ts", ".mp3", ".wav", ".ogg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
]


def _warn(msg: str) -> None:
    try:
        print(f"[browser_backend] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


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


def normalize_blocked_resource_extensions(value: Any = None) -> List[str]:
    if value is None:
        if os.environ.get("DAEMON_BLOCK_RESOURCE_EXTENSIONS_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
            return []
        value = os.environ.get("DAEMON_BLOCKED_RESOURCE_EXTENSIONS", ",".join(DEFAULT_BLOCKED_RESOURCE_EXTENSIONS))
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []

    normalized: List[str] = []
    seen = set()
    for item in raw_items:
        ext = str(item or "").strip().lower()
        if not ext:
            continue
        if ext.startswith("*"):
            ext = ext.lstrip("*")
        if not ext.startswith("."):
            ext = f".{ext}"
        if "/" in ext or "\\" in ext or "?" in ext or "#" in ext:
            continue
        if ext not in seen:
            seen.add(ext)
            normalized.append(ext)
    return normalized


def should_block_resource_url(url: str, blocked_extensions: Any = None) -> bool:
    extensions = normalize_blocked_resource_extensions(blocked_extensions)
    if not extensions:
        return False
    try:
        path = unquote(urlparse(str(url or "")).path).lower()
    except Exception:
        path = str(url or "").split("?", 1)[0].split("#", 1)[0].lower()
    return any(path.endswith(ext) for ext in extensions)


async def install_async_blocked_resource_routes(target: Any, blocked_extensions: Any = None) -> None:
    extensions = normalize_blocked_resource_extensions(blocked_extensions)
    if not extensions or not hasattr(target, "route"):
        return

    async def route_handler(route):
        if should_block_resource_url(route.request.url, extensions):
            return await route.abort()
        return await route.continue_()

    await target.route("**/*", route_handler)


def install_sync_blocked_resource_routes(target: Any, blocked_extensions: Any = None) -> None:
    extensions = normalize_blocked_resource_extensions(blocked_extensions)
    if not extensions or not hasattr(target, "route"):
        return

    def route_handler(route):
        if should_block_resource_url(route.request.url, extensions):
            return route.abort()
        return route.continue_()

    target.route("**/*", route_handler)


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
                if async_playwright is None:
                    raise RuntimeError("Camoufox is not available. Run setup_daemon.sh to install .deps.")
                _warn("Camoufox module not available, falling back to Playwright Chromium.")
                self.backend = "chromium"
            else:
                options = {"headless": self.headless}
                options.update(self.camoufox_options)
                if self.proxy_url and "geoip" not in options:
                    options["geoip"] = True
                proxy = to_playwright_proxy(self.proxy_url)
                if proxy:
                    options["proxy"] = proxy
                try:
                    self._camoufox_cm = AsyncCamoufox(**options)
                    self.browser = await self._camoufox_cm.__aenter__()
                    return self.browser
                except Exception as exc:
                    needs_geoip_fallback = (
                        bool(options.get("geoip"))
                        and (
                            (NotInstalledGeoIPExtra is not None and isinstance(exc, NotInstalledGeoIPExtra))
                            or ("geoip extra" in str(exc).lower())
                            or ("notinstalledgeoipextra" in str(exc).lower())
                        )
                    )
                    if needs_geoip_fallback:
                        _warn("Camoufox geoip extra missing; retrying with geoip disabled.")
                        options["geoip"] = False
                        with warnings.catch_warnings():
                            warnings.filterwarnings(
                                "ignore",
                                message=r"When using a proxy, it is heavily recommended that you pass `geoip=True`\.",
                            )
                            self._camoufox_cm = AsyncCamoufox(**options)
                            self.browser = await self._camoufox_cm.__aenter__()
                        return self.browser
                    raise

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
                if sync_playwright is None:
                    raise RuntimeError("Camoufox is not available. Run setup_daemon.sh to install .deps.")
                _warn("Camoufox module not available, falling back to Playwright Chromium.")
                self.backend = "chromium"
            else:
                options = {"headless": self.headless}
                options.update(self.camoufox_options)
                if self.proxy_url and "geoip" not in options:
                    options["geoip"] = True
                proxy = to_playwright_proxy(self.proxy_url)
                if proxy:
                    options["proxy"] = proxy
                try:
                    self._camoufox_cm = Camoufox(**options)
                    self.browser = self._camoufox_cm.__enter__()
                    return self.browser
                except Exception as exc:
                    needs_geoip_fallback = (
                        bool(options.get("geoip"))
                        and (
                            (NotInstalledGeoIPExtra is not None and isinstance(exc, NotInstalledGeoIPExtra))
                            or ("geoip extra" in str(exc).lower())
                            or ("notinstalledgeoipextra" in str(exc).lower())
                        )
                    )
                    if needs_geoip_fallback:
                        _warn("Camoufox geoip extra missing; retrying with geoip disabled.")
                        options["geoip"] = False
                        with warnings.catch_warnings():
                            warnings.filterwarnings(
                                "ignore",
                                message=r"When using a proxy, it is heavily recommended that you pass `geoip=True`\.",
                            )
                            self._camoufox_cm = Camoufox(**options)
                            self.browser = self._camoufox_cm.__enter__()
                        return self.browser
                    raise

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
