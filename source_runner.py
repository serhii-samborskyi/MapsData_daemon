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


def _field_label(field: dict) -> str:
    return str(field.get("label") or field.get("target_field") or field.get("xpath") or "field")


def _contact_keys(contact: Dict[str, Any]) -> List[str]:
    keys = [key for key, value in contact.items() if key != "__missing_required" and _clean(value)]
    source_data = contact.get("source_data")
    if isinstance(source_data, dict):
        keys.extend([f"source_data.{key}" for key, value in source_data.items() if _clean(value)])
    return sorted(set(keys))


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


async def _xpath_content_values(page, xpath: str, root_handle=None) -> List[str]:
    if not xpath:
        return []
    script = r"""
    ({xpath, root}) => {
        const context = root || document;
        const result = document.evaluate(xpath, context, null, XPathResult.ANY_TYPE, null);
        const out = [];
        const pushNode = (node) => {
            if (!node) return;
            if (node.nodeType === Node.ATTRIBUTE_NODE) out.push(node.value || '');
            else if (node.nodeType === Node.TEXT_NODE) out.push(node.textContent || '');
            else out.push((node.innerText || node.textContent || '').trim());
        };
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
                    pushNode(node);
                    node = result.iterateNext();
                }
                break;
            }
            case XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE:
            case XPathResult.ORDERED_NODE_SNAPSHOT_TYPE:
                for (let i = 0; i < result.snapshotLength; i++) pushNode(result.snapshotItem(i));
                break;
            case XPathResult.ANY_UNORDERED_NODE_TYPE:
            case XPathResult.FIRST_ORDERED_NODE_TYPE:
                pushNode(result.singleNodeValue);
                break;
        }
        return out.map(v => String(v || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
    }
    """
    values = await page.evaluate(script, {"xpath": xpath, "root": root_handle})
    return [str(v).strip() for v in values if str(v).strip()] if isinstance(values, list) else []


async def _extract_field_value(page, field: dict, root_handle=None) -> str:
    xpath = str(field.get("xpath") or "").strip()
    regex = str(field.get("regex") or "").strip()
    run_within_content = bool(field.get("run_regex_within_xpath_content", False))
    if run_within_content and regex:
        content_values = await _xpath_content_values(page, xpath, root_handle=root_handle)
        for raw_value in content_values:
            value = _apply_regex(_clean(raw_value), regex)
            if value:
                return value
        return _apply_regex(_clean(" ".join(content_values)), regex)
    values = await _xpath_values(page, xpath, root_handle=root_handle)
    value = _clean(" ".join(values))
    return _apply_regex(value, regex)


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
    all_the_way_down_scrolls = bool(nav.get("all_the_way_down_scrolls", False))
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
        if all_the_way_down_scrolls:
            max_micro_steps = random.randint(8, 18)
            for _ in range(max_micro_steps):
                if stop_signal():
                    return
                state = await page.evaluate(
                    r"""({xpath, multiplier}) => {
                        const pageRoot = document.scrollingElement || document.documentElement || document.body;
                        let target = pageRoot;
                        let isWindow = true;
                        if (xpath) {
                            const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (node) {
                                target = node;
                                isWindow = false;
                            }
                        }
                        if (!target) return {done: true, moved: false};
                        const clientHeight = isWindow ? (window.innerHeight || target.clientHeight || 800) : (target.clientHeight || 800);
                        const scrollHeight = target.scrollHeight || clientHeight;
                        const before = isWindow ? (window.scrollY || target.scrollTop || 0) : (target.scrollTop || 0);
                        const step = Math.max(220, Math.floor(clientHeight * multiplier));
                        if (isWindow) {
                            window.scrollBy(0, step);
                        } else {
                            target.scrollBy(0, step);
                        }
                        const after = isWindow ? (window.scrollY || target.scrollTop || 0) : (target.scrollTop || 0);
                        const done = (after + clientHeight + 24) >= scrollHeight || after === before;
                        return {done, moved: after !== before, top: after, scrollHeight, clientHeight};
                    }""",
                    {
                        "xpath": xpath,
                        "multiplier": random.uniform(0.45, 0.95),
                    },
                )
                await page.wait_for_timeout(random.randint(180, 520))
                if isinstance(state, dict) and state.get("done"):
                    await page.wait_for_timeout(random.randint(650, 1600))
                    return
            await page.wait_for_timeout(random.randint(900, 1800))
            return

        await page.evaluate(
            r"""({xpath, allTheWayDown}) => {
                const scrollWindow = () => {
                    const root = document.scrollingElement || document.documentElement || document.body;
                    if (!root) return false;
                    const distance = root.scrollHeight || window.innerHeight || 1200;
                    if (allTheWayDown) {
                        window.scrollTo(0, distance);
                        root.scrollTop = distance;
                    } else {
                        window.scrollBy(0, distance);
                    }
                    return true;
                };
                if (xpath) {
                    const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                    if (node) {
                        const distance = node.scrollHeight || node.clientHeight || 1200;
                        if (allTheWayDown) {
                            node.scrollTop = distance;
                        } else {
                            node.scrollBy(0, distance);
                        }
                        return true;
                    }
                }
                return scrollWindow();
            }""",
            {"xpath": xpath, "allTheWayDown": all_the_way_down_scrolls},
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
        value = await _extract_field_value(page, field, root_handle=root_handle)
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


async def _field_debug_summary(page, fields: List[dict], root_handle=None) -> str:
    parts: List[str] = []
    for field in fields:
        label = _field_label(field)
        xpath = str(field.get("xpath") or "").strip()
        if not xpath:
            parts.append(f"{label}=empty_xpath")
            continue
        try:
            use_content = bool(field.get("run_regex_within_xpath_content", False)) and bool(str(field.get("regex") or "").strip())
            values = await (_xpath_content_values(page, xpath, root_handle=root_handle) if use_content else _xpath_values(page, xpath, root_handle=root_handle))
            raw = _clean(" ".join(values))
            value = await _extract_field_value(page, field, root_handle=root_handle)
            if value:
                parts.append(f"{label}=ok{'(content_regex)' if use_content else ''}")
            elif raw:
                parts.append(f"{label}=regex_empty(raw_len={len(raw)})")
            else:
                parts.append(f"{label}=xpath_empty")
        except Exception as exc:
            parts.append(f"{label}=error({type(exc).__name__}: {str(exc)[:120]})")
    return "; ".join(parts) if parts else "no_fields_configured"


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
    logger.info(
        "[source] Browser launch: show_browser=%s headless=%s proxy=%s",
        bool(show_browser),
        not bool(show_browser),
        "on" if normalize_proxy_url(proxy_url) else "off",
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
        skipped_duplicates = 0
        skipped_missing_required = 0
        slow_detail_attempted = 0
        slow_detail_opened = 0
        slow_detail_url_missing = 0
        slow_detail_wait_timeout = 0
        slow_detail_field_empty = 0
        slow_detail_errors = 0
        detail_url_xpath = str(slow.get("detail_url_xpath") or "").strip()
        wait_xpath = str(slow.get("wait_xpath") or "").strip()
        slow_fields = slow.get("fields") if isinstance(slow.get("fields"), list) else []
        if slow_enabled:
            logger.info(
                "[source][slow] Enabled for request %s: detail_url_xpath=%r wait_xpath=%r fields=%d",
                request.id,
                detail_url_xpath,
                wait_xpath,
                len(slow_fields),
            )
            if not detail_url_xpath:
                logger.warning("[source][slow] Request %s has slow mode enabled but detail_url_xpath is empty.", request.id)
            if not slow_fields:
                logger.warning("[source][slow] Request %s has slow mode enabled but no detail fields configured.", request.id)

        for block_index, block in enumerate(blocks, start=1):
            if stop_signal():
                break
            contact = await _extract_fields(page, fast_fields, block, page.url)
            missing_required = contact.pop("__missing_required", [])
            if missing_required:
                skipped_missing_required += 1
                logger.debug(
                    "[source] Skipping block for request %s; missing required fields: %s",
                    request.id,
                    ", ".join(missing_required),
                )
                continue
            if slow_enabled:
                slow_detail_attempted += 1
                try:
                    detail_values = await _xpath_values(page, detail_url_xpath, root_handle=block)
                    detail_url = _absolute_url(page.url, detail_values[0] if detail_values else "")
                    if not detail_url:
                        slow_detail_url_missing += 1
                        logger.warning(
                            "[source][slow] Request %s block=%d no detail URL. detail_url_xpath=%r extracted_values=%d fast_fields=%s",
                            request.id,
                            block_index,
                            detail_url_xpath,
                            len(detail_values),
                            await _field_debug_summary(page, fast_fields, root_handle=block),
                        )
                    else:
                        detail_page = await context.new_page()
                        try:
                            logger.info("[source][slow] Request %s block=%d opening detail URL: %s", request.id, block_index, detail_url)
                            await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                            slow_detail_opened += 1
                            try:
                                await detail_page.wait_for_load_state("domcontentloaded", timeout=15000)
                            except Exception as exc:
                                logger.warning(
                                    "[source][slow] Request %s block=%d load-state wait warning: %s",
                                    request.id,
                                    block_index,
                                    str(exc)[:180],
                                )
                            try:
                                await detail_page.wait_for_function(
                                    "() => document.body || document.documentElement",
                                    timeout=15000,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "[source][slow] Request %s block=%d DOM wait timeout. %s error=%s",
                                    request.id,
                                    block_index,
                                    await _page_diagnostics(detail_page),
                                    str(exc)[:180],
                                )
                            if wait_xpath:
                                try:
                                    await detail_page.wait_for_selector(f"xpath={wait_xpath}", timeout=15000)
                                    logger.info("[source][slow] Request %s block=%d wait XPath matched.", request.id, block_index)
                                except Exception as exc:
                                    slow_detail_wait_timeout += 1
                                    logger.warning(
                                        "[source][slow] Request %s block=%d wait XPath did not match within timeout. wait_xpath=%r %s error=%s",
                                        request.id,
                                        block_index,
                                        wait_xpath,
                                        await _page_diagnostics(detail_page),
                                        str(exc)[:180],
                                    )
                            detail_contact = await _extract_fields(detail_page, slow_fields, None, detail_page.url)
                            detail_contact.pop("__missing_required", None)
                            detail_keys = _contact_keys(detail_contact)
                            if detail_keys:
                                logger.info(
                                    "[source][slow] Request %s block=%d extracted detail fields: %s",
                                    request.id,
                                    block_index,
                                    ", ".join(detail_keys),
                                )
                            else:
                                slow_detail_field_empty += 1
                                logger.warning(
                                    "[source][slow] Request %s block=%d detail fields empty. %s fields=%s",
                                    request.id,
                                    block_index,
                                    await _page_diagnostics(detail_page),
                                    await _field_debug_summary(detail_page, slow_fields),
                                )
                            if detail_contact.get("source_data") and contact.get("source_data"):
                                merged = dict(contact.get("source_data") or {})
                                merged.update(detail_contact.get("source_data") or {})
                                detail_contact["source_data"] = merged
                            contact.update(detail_contact)
                        except Exception as exc:
                            slow_detail_errors += 1
                            logger.exception(
                                "[source][slow] Request %s block=%d detail scrape error for url=%s: %s",
                                request.id,
                                block_index,
                                detail_url,
                                exc,
                            )
                        finally:
                            await detail_page.close()
                except Exception as exc:
                    slow_detail_errors += 1
                    logger.exception(
                        "[source][slow] Request %s block=%d detail URL/extraction error: %s",
                        request.id,
                        block_index,
                        exc,
                    )
            normalized = _normalize_contact(contact, campaign_id, request.id)
            dedupe_key = "|".join(str(normalized.get(k, "")).lower() for k in ("business_name", "domain", "phone", "address"))
            if dedupe_key in seen:
                skipped_duplicates += 1
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
        logger.info(
            "[source] Request %s summary: blocks=%d sent=%d skipped_duplicates=%d skipped_missing_required=%d slow_attempted=%d slow_opened=%d slow_missing_url=%d slow_wait_timeout=%d slow_empty_fields=%d slow_errors=%d",
            request.id,
            len(blocks),
            total,
            skipped_duplicates,
            skipped_missing_required,
            slow_detail_attempted,
            slow_detail_opened,
            slow_detail_url_missing,
            slow_detail_wait_timeout,
            slow_detail_field_empty,
            slow_detail_errors,
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
