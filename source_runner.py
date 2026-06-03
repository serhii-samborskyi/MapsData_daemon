from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Set

from browser_backend import AsyncBrowserRuntime, backend_display_name, normalize_proxy_url
from maps_scraper import Campaign, HttpClient, LeadsApiClient, RequestItem

logger = logging.getLogger(__name__)

CORE_ALIAS = {
    "business_name": "business_name",
    "companyname": "business_name",
    "name": "business_name",
    "website": "domain",
    "www": "www",
    "reviews": "review_count",
    "review_count": "review_count",
}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _apply_regex(value: str, pattern: str) -> str:
    if not pattern:
        return value
    try:
        match = re.search(pattern, value, flags=re.I | re.S)
    except re.error:
        return value
    if not match:
        return ""
    if match.groups():
        return _clean(match.group(1))
    return _clean(match.group(0))


def _absolute_url(base_url: str, value: str) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    return urllib.parse.urljoin(base_url, raw)


async def _xpath_values(page, xpath: str, root_handle=None) -> List[str]:
    if not xpath:
        return []
    script = r"""
    ({xpath, root}) => {
        const context = root || document;
        const result = document.evaluate(xpath, context, null, XPathResult.ANY_TYPE, null);
        const out = [];
        switch (result.resultType) {
            case XPathResult.STRING_TYPE:
                out.push(result.stringValue || '');
                break;
            case XPathResult.NUMBER_TYPE:
                out.push(String(result.numberValue));
                break;
            case XPathResult.BOOLEAN_TYPE:
                out.push(String(result.booleanValue));
                break;
            case XPathResult.UNORDERED_NODE_ITERATOR_TYPE:
            case XPathResult.ORDERED_NODE_ITERATOR_TYPE: {
                let node = result.iterateNext();
                while (node) {
                    if (node.nodeType === Node.ATTRIBUTE_NODE) out.push(node.value || '');
                    else out.push((node.textContent || node.getAttribute?.('href') || '').trim());
                    node = result.iterateNext();
                }
                break;
            }
            case XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE:
            case XPathResult.ORDERED_NODE_SNAPSHOT_TYPE:
                for (let i = 0; i < result.snapshotLength; i++) {
                    const node = result.snapshotItem(i);
                    if (!node) continue;
                    if (node.nodeType === Node.ATTRIBUTE_NODE) out.push(node.value || '');
                    else out.push((node.textContent || node.getAttribute?.('href') || '').trim());
                }
                break;
            case XPathResult.ANY_UNORDERED_NODE_TYPE:
            case XPathResult.FIRST_ORDERED_NODE_TYPE: {
                const node = result.singleNodeValue;
                if (node) {
                    if (node.nodeType === Node.ATTRIBUTE_NODE) out.push(node.value || '');
                    else out.push((node.textContent || node.getAttribute?.('href') || '').trim());
                }
                break;
            }
        }
        return out.map(v => String(v || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
    }
    """
    values = await page.evaluate(script, {"xpath": xpath, "root": root_handle})
    return [str(v).strip() for v in values if str(v).strip()] if isinstance(values, list) else []


async def _query_nodes(page, xpath: str):
    return await page.query_selector_all(f"xpath={xpath}")


async def _navigate_results(page, config: dict, stop_signal: Callable[[], bool]) -> None:
    nav = config.get("navigation") if isinstance(config.get("navigation"), dict) else {}
    nav_type = str(nav.get("type") or "scroll").strip().lower()
    max_scrolls = max(1, int(nav.get("max_scrolls") or 60))
    max_pages = max(1, int(nav.get("max_pages") or 10))
    stable_cycles = max(1, int(nav.get("stable_cycles") or 3))
    pause_min_ms = max(1, int(nav.get("pause_min_ms") or 800))
    pause_max_ms = max(pause_min_ms, int(nav.get("pause_max_ms") or pause_min_ms))
    scroll_container_xpath = str(nav.get("scroll_container_xpath") or "").strip()
    fast = config.get("fast") if isinstance(config.get("fast"), dict) else {}
    block_xpath = str(fast.get("block_xpath") or "").strip()

    async def pause() -> None:
        await page.wait_for_timeout(random.randint(pause_min_ms, pause_max_ms))

    async def block_count() -> int:
        try:
            return len(await _query_nodes(page, block_xpath))
        except Exception:
            return 0

    async def safe_scroll(xpath: str = "") -> None:
        await page.evaluate(
            r"""(xpath) => {
                const scrollWindow = () => {
                    const root = document.scrollingElement || document.documentElement || document.body;
                    if (!root) return false;
                    window.scrollBy(0, root.scrollHeight || window.innerHeight || 1200);
                    return true;
                };
                if (xpath) {
                    const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                    if (node) {
                        node.scrollBy(0, node.scrollHeight || node.clientHeight || 1200);
                        return true;
                    }
                }
                return scrollWindow();
            }""",
            xpath,
        )

    if nav_type == "pagination":
        next_xpath = str(nav.get("next_button_xpath") or "").strip()
        for _ in range(max_pages):
            if stop_signal():
                return
            before = await block_count()
            buttons = await _query_nodes(page, next_xpath)
            if not buttons:
                return
            try:
                await buttons[0].click(timeout=5000)
            except Exception:
                return
            await pause()
            after = await block_count()
            if after <= before:
                await pause()
        return

    if nav_type == "load_more":
        button_xpath = str(nav.get("load_more_button_xpath") or "").strip()
        stable = 0
        last = await block_count()
        for _ in range(max_pages):
            if stop_signal():
                return
            buttons = await _query_nodes(page, button_xpath)
            if not buttons:
                return
            try:
                await buttons[0].click(timeout=5000)
            except Exception:
                return
            await pause()
            count = await block_count()
            stable = stable + 1 if count <= last else 0
            last = count
            if stable >= stable_cycles:
                return
        return

    stable = 0
    last = await block_count()
    for _ in range(max_scrolls):
        if stop_signal():
            return
        if scroll_container_xpath:
            await safe_scroll(scroll_container_xpath)
        else:
            await safe_scroll("")
        await pause()
        count = await block_count()
        stable = stable + 1 if count <= last else 0
        last = count
        if stable >= stable_cycles:
            return


async def _extract_fields(page, fields: List[dict], root_handle, page_url: str) -> Dict[str, Any]:
    contact: Dict[str, Any] = {}
    source_data: Dict[str, Any] = {}
    missing_required: List[str] = []
    for field in fields:
        xpath = str(field.get("xpath") or "").strip()
        values = await _xpath_values(page, xpath, root_handle=root_handle)
        value = _clean(" ".join(values))
        value = _apply_regex(value, str(field.get("regex") or "").strip())
        if not value:
            if bool(field.get("required")):
                missing_required.append(str(field.get("label") or field.get("target_field") or xpath))
            continue
        target_type = str(field.get("target_type") or "core").lower()
        target_field = str(field.get("target_field") or "").strip().lower()
        if target_type == "dynamic":
            source_data[target_field] = value
        else:
            contact[CORE_ALIAS.get(target_field, target_field)] = value

    if source_data:
        contact["source_data"] = source_data
    if missing_required:
        contact["__missing_required"] = missing_required
    if "domain" in contact:
        contact["domain"] = _absolute_url(page_url, contact["domain"])
    if "www" in contact:
        contact["www"] = _absolute_url(page_url, contact["www"])
    return contact


async def _page_diagnostics(page) -> str:
    try:
        data = await page.evaluate(
            r"""() => ({
                url: location.href,
                title: document.title || '',
                readyState: document.readyState || '',
                bodyText: (document.body && document.body.innerText || '').slice(0, 250),
                htmlLength: document.documentElement ? document.documentElement.outerHTML.length : 0
            })"""
        )
    except Exception as exc:
        return f"diagnostics_unavailable={exc}"
    if not isinstance(data, dict):
        return "diagnostics_unavailable=bad_result"
    return (
        f"url={data.get('url')!r} title={data.get('title')!r} "
        f"readyState={data.get('readyState')!r} htmlLength={data.get('htmlLength')} "
        f"bodyText={data.get('bodyText')!r}"
    )


def _normalize_contact(contact: Dict[str, Any], campaign_id: str, request_id: str) -> Dict[str, Any]:
    out = dict(contact)
    out["campaign_id"] = campaign_id
    out["request_id"] = request_id
    if not _clean(out.get("business_name")):
        out["business_name"] = _clean(out.get("company")) or "Unknown Business"
    out["review_count"] = out.get("review_count") or 0
    return out


async def _scrape_request(
    source: dict,
    request: RequestItem,
    campaign_id: str,
    batch_size: int,
    api: LeadsApiClient,
    scrape_mode: str,
    show_browser: bool,
    proxy_url: str,
    stop_signal: Callable[[], bool],
) -> int:
    config = source.get("config") if isinstance(source.get("config"), dict) else {}
    start_template = str(config.get("start_url_template") or "").strip()
    url = start_template.replace("{query}", urllib.parse.quote_plus(str(request.req_text or "").strip()))
    fast = config.get("fast") if isinstance(config.get("fast"), dict) else {}
    slow = config.get("slow") if isinstance(config.get("slow"), dict) else {}
    block_xpath = str(fast.get("block_xpath") or "").strip()
    fast_fields = fast.get("fields") if isinstance(fast.get("fields"), list) else []
    slow_enabled = bool(slow.get("enabled")) and str(scrape_mode or "fast").strip().lower() == "slow"

    runtime = AsyncBrowserRuntime(
        headless=(not show_browser),
        chromium_args=["--disable-blink-features=AutomationControlled", "--disable-extensions"],
        camoufox_options={"block_images": False},
        proxy_url=normalize_proxy_url(proxy_url),
    )
    browser = await runtime.launch()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        logger.info("[source] Opening %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        try:
            await page.wait_for_function(
                "() => document.body || document.documentElement",
                timeout=20000,
            )
        except Exception as exc:
            raise RuntimeError(f"Page DOM not ready for scraping after navigation: {page.url}") from exc
        await page.wait_for_timeout(1200)
        if block_xpath:
            try:
                await page.wait_for_selector(f"xpath={block_xpath}", timeout=30000)
            except Exception:
                logger.warning(
                    "[source] Initial block XPath did not appear within 30s for request %s. xpath=%r %s",
                    request.id,
                    block_xpath,
                    await _page_diagnostics(page),
                )
        await _navigate_results(page, config, stop_signal)
        blocks = await _query_nodes(page, block_xpath)
        logger.info("[source] Request %s collected %d blocks", request.id, len(blocks))
        if not blocks:
            raise RuntimeError(
                f"Generic source found 0 result blocks for request {request.id}; "
                f"check block XPath or page access. block_xpath={block_xpath!r} {await _page_diagnostics(page)}"
            )

        seen: Set[str] = set()
        batch: List[Dict[str, Any]] = []
        total = 0
        detail_url_xpath = str(slow.get("detail_url_xpath") or "").strip()
        wait_xpath = str(slow.get("wait_xpath") or "").strip()
        slow_fields = slow.get("fields") if isinstance(slow.get("fields"), list) else []

        for block in blocks:
            if stop_signal():
                break
            contact = await _extract_fields(page, fast_fields, block, page.url)
            missing_required = contact.pop("__missing_required", [])
            if missing_required:
                logger.debug(
                    "[source] Skipping block for request %s; missing required fields: %s",
                    request.id,
                    ", ".join(missing_required),
                )
                continue
            if slow_enabled:
                detail_values = await _xpath_values(page, detail_url_xpath, root_handle=block)
                detail_url = _absolute_url(page.url, detail_values[0] if detail_values else "")
                if detail_url:
                    detail_page = await context.new_page()
                    try:
                        await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                        if wait_xpath:
                            try:
                                await detail_page.wait_for_selector(f"xpath={wait_xpath}", timeout=15000)
                            except Exception:
                                pass
                        detail_contact = await _extract_fields(detail_page, slow_fields, None, detail_page.url)
                        detail_contact.pop("__missing_required", None)
                        if detail_contact.get("source_data") and contact.get("source_data"):
                            merged = dict(contact.get("source_data") or {})
                            merged.update(detail_contact.get("source_data") or {})
                            detail_contact["source_data"] = merged
                        contact.update(detail_contact)
                    finally:
                        await detail_page.close()
            normalized = _normalize_contact(contact, campaign_id, request.id)
            dedupe_key = "|".join(str(normalized.get(k, "")).lower() for k in ("business_name", "domain", "phone", "address"))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            batch.append(normalized)
            total += 1
            if len(batch) >= batch_size:
                if not api.send_contacts(batch):
                    raise RuntimeError(f"Failed sending generic source batch for request {request.id}")
                batch = []
        if batch:
            if not api.send_contacts(batch):
                raise RuntimeError(f"Failed sending generic source final batch for request {request.id}")
        if total <= 0:
            raise RuntimeError(
                f"Generic source produced 0 contacts for request {request.id}; "
                "check required fields and field XPath mappings."
            )
        return total
    finally:
        await runtime.close()


def run_generic_source_campaign(
    *,
    source: dict,
    campaign_id: str,
    campaign_name: str,
    base_url: str,
    batch_size: int,
    request_workers: int,
    show_browser: bool,
    proxy_url: str,
    scrape_mode: str,
    should_stop: Callable[[], bool],
) -> None:
    api = LeadsApiClient(base_url, HttpClient())
    worker_count = max(1, int(request_workers or 1))
    logger.info(
        "Generic source campaign start: campaign=%s source=%s workers=%s browser=%s proxy=%s",
        campaign_id,
        (source or {}).get("name") or (source or {}).get("id") or "generic",
        worker_count,
        backend_display_name(None),
        "on" if normalize_proxy_url(proxy_url) else "off",
    )

    while not should_stop():
        requests = api.get_requests_for_campaign_name(campaign_name, include_inuse=True)
        pending = [item for item in requests if _clean(item.req_text)]
        if not pending:
            logger.info("[source] No more requests for campaign %s", campaign_id)
            return
        selected = pending[:min(worker_count, len(pending))]
        for item in selected:
            api.set_request_status(item.id, "inuse")

        errors: List[Exception] = []
        threads: List[threading.Thread] = []

        def run_one(item: RequestItem) -> None:
            try:
                total = asyncio.run(_scrape_request(source, item, campaign_id, batch_size, api, scrape_mode, show_browser, proxy_url, should_stop))
                if total <= 0:
                    raise RuntimeError(f"Generic source produced 0 contacts for request {item.id}")
                if not should_stop():
                    api.set_request_status(item.id, "completed")
                logger.info("[source] Completed request %s with %d contacts", item.id, total)
            except Exception as exc:
                logger.exception("[source] Request %s failed", item.id)
                errors.append(exc)

        for item in selected:
            thread = threading.Thread(target=run_one, args=(item,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

        if errors:
            raise RuntimeError(f"Generic source campaign failed: {errors[0]}")
