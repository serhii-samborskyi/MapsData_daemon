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
from email_quality import extract_candidate_emails_from_text
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


def _is_email_field(field: dict) -> bool:
    label = _field_label(field).lower()
    target = str(field.get("target_field") or "").strip().lower()
    return "email" in label or target == "email" or target.endswith("_email")


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
    return _strip_detail_url_trailing_slash(urllib.parse.urljoin(base_url, raw))


def _normalize_website_url(base_url: str, value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    if raw.startswith("//"):
        return _strip_detail_url_trailing_slash(f"https:{raw}")
    if raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(raw)
        if parsed.netloc.endswith("l.facebook.com") and parsed.path.startswith("/l.php"):
            query = urllib.parse.parse_qs(parsed.query)
            target = (query.get("u") or [""])[0]
            if target:
                return _normalize_website_url(base_url, target)
        return _strip_detail_url_trailing_slash(raw)
    if re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(/.*)?$", raw, flags=re.I):
        return _strip_detail_url_trailing_slash(f"https://{raw}")
    return _absolute_url(base_url, raw)


def _strip_detail_url_trailing_slash(value: str) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except Exception:
        return raw.rstrip("/")
    if parts.scheme and parts.netloc:
        path = parts.path.rstrip("/")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    return raw.rstrip("/")


def _detail_url_dedupe_key(value: Any) -> str:
    raw = _strip_detail_url_trailing_slash(str(value or ""))
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except Exception:
        return raw.lower()
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = parts.query
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _looks_like_url(value: Any) -> bool:
    raw = _clean(value)
    if not raw:
        return False
    if raw.startswith(("http://", "https://", "//", "/")):
        return True
    return bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/|$)", raw, flags=re.I))


def _fallback_detail_url_from_contact(contact: Dict[str, Any], page_url: str) -> tuple[str, str]:
    candidates = [
        ("facebook", contact.get("facebook")),
        ("fb_link", contact.get("fb_link")),
        ("profile_url", contact.get("profile_url")),
        ("url", contact.get("url")),
        ("website", contact.get("website")),
        ("domain", contact.get("domain")),
        ("www", contact.get("www")),
    ]
    source_data = contact.get("source_data")
    if isinstance(source_data, dict):
        for key in ("facebook", "fb_link", "profile_url", "url", "website", "domain", "www"):
            candidates.append((f"source_data.{key}", source_data.get(key)))
    for label, value in candidates:
        if _looks_like_url(value):
            return _absolute_url(page_url, str(value)), label
    return "", ""


def _block_scoped_xpath(xpath: str) -> str:
    value = str(xpath or "").strip()
    if value.startswith(".//") or value.startswith("./"):
        return value
    if value.startswith("//"):
        return f".{value}"
    return value


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


async def _xpath_content_values(page, xpath: str, root_handle=None, strip_html: bool = False) -> List[str]:
    if not xpath:
        return []
    script = r"""
    ({xpath, root, stripHtml}) => {
        const context = root || document;
        const result = document.evaluate(xpath, context, null, XPathResult.ANY_TYPE, null);
        const out = [];
        const pushNode = (node) => {
            if (!node) return;
            if (node.nodeType === Node.ATTRIBUTE_NODE) out.push(node.value || '');
            else if (node.nodeType === Node.TEXT_NODE) out.push(node.textContent || '');
            else if (stripHtml) {
                const clone = node.cloneNode(true);
                if (clone.querySelectorAll) {
                    clone.querySelectorAll('script,style,noscript,template,svg').forEach(child => child.remove());
                }
                out.push((node.innerText || clone.textContent || '').trim());
            } else {
                out.push((node.innerText || node.textContent || '').trim());
            }
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
    values = await page.evaluate(script, {"xpath": xpath, "root": root_handle, "stripHtml": bool(strip_html)})
    return [str(v).strip() for v in values if str(v).strip()] if isinstance(values, list) else []


async def _field_xpath_values(page, field: dict, root_handle=None) -> tuple[List[str], bool, bool]:
    xpath = str(field.get("xpath") or "").strip()
    regex = str(field.get("regex") or "").strip()
    use_content = bool(field.get("run_regex_within_xpath_content", False)) and bool(regex)
    strip_html = bool(field.get("strip_html_before_regex", False))
    if use_content:
        values = await _xpath_content_values(page, xpath, root_handle=root_handle, strip_html=strip_html)
    else:
        values = await _xpath_values(page, xpath, root_handle=root_handle)
    return values, use_content, strip_html


def _regex_attempts(values: List[str], pattern: str, limit: int = 12) -> List[Dict[str, Any]]:
    attempts: List[Dict[str, Any]] = []
    regex = str(pattern or "").strip()
    if not regex:
        return attempts
    for index, raw_value in enumerate(values[:limit], start=1):
        cleaned = _clean(raw_value)
        matched_value = _apply_regex(cleaned, regex)
        attempts.append({
            "index": index,
            "matched": bool(matched_value),
            "value": matched_value,
            "preview": cleaned[:300],
        })
    return attempts


def _matched_attempt_index(attempts: List[Dict[str, Any]]) -> int:
    for attempt in attempts:
        if attempt.get("matched"):
            return int(attempt.get("index") or 0)
    return 0


async def _extract_field_value(page, field: dict, root_handle=None) -> str:
    regex = str(field.get("regex") or "").strip()
    values, _use_content, _strip_html = await _field_xpath_values(page, field, root_handle=root_handle)
    if regex:
        for raw_value in values:
            value = _apply_regex(_clean(raw_value), regex)
            if value:
                return value
        return _apply_regex(_clean(" ".join(values)), regex)
    value = _clean(" ".join(values))
    return value


async def _email_candidates_from_page(page) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"html": [], "body_text": [], "mailto": []}
    try:
        html = await page.content()
        out["html"] = sorted(extract_candidate_emails_from_text(html or ""))
    except Exception:
        pass
    try:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        out["body_text"] = sorted(extract_candidate_emails_from_text(str(text or "")))
    except Exception:
        pass
    try:
        mailtos = await page.eval_on_selector_all(
            "a[href^='mailto:']",
            "els => els.map(el => el.getAttribute('href')).filter(Boolean)",
        )
        candidates = set()
        for item in mailtos or []:
            candidates.update(extract_candidate_emails_from_text(str(item or "")))
        out["mailto"] = sorted(candidates)
    except Exception:
        pass
    return out


async def _fallback_email_from_page(page) -> tuple[str, str, Dict[str, List[str]]]:
    candidates = await _email_candidates_from_page(page)
    for source in ("mailto", "body_text", "html"):
        if candidates.get(source):
            return candidates[source][0], source, candidates
    return "", "", candidates


async def _extract_field_value_with_meta(page, field: dict, root_handle=None) -> tuple[str, Dict[str, Any]]:
    value = await _extract_field_value(page, field, root_handle=root_handle)
    meta: Dict[str, Any] = {"fallback_source": "", "email_candidates": {}}
    if value or root_handle is not None or not _is_email_field(field):
        return value, meta
    fallback_value, source, candidates = await _fallback_email_from_page(page)
    if fallback_value:
        meta["fallback_source"] = source
        meta["email_candidates"] = candidates
        return fallback_value, meta
    meta["email_candidates"] = candidates
    return value, meta


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
        value, _meta = await _extract_field_value_with_meta(page, field, root_handle=root_handle)
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
        contact["domain"] = _normalize_website_url(page_url, contact["domain"])
    if "www" in contact:
        contact["www"] = _normalize_website_url(page_url, contact["www"])
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
            values, use_content, strip_html = await _field_xpath_values(page, field, root_handle=root_handle)
            raw = _clean(" ".join(values))
            value = await _extract_field_value(page, field, root_handle=root_handle)
            if value:
                suffix = "(content_regex_strip_html)" if use_content and strip_html else "(content_regex)" if use_content else ""
                parts.append(f"{label}=ok{suffix}")
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


async def _wait_for_detail_page_settle(page, max_wait_ms: int = 9000) -> Dict[str, Any]:
    deadline = time.monotonic() + max(0.5, max_wait_ms / 1000.0)
    last_signature = None
    stable_cycles = 0
    latest: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            latest = await page.evaluate(
                r"""() => {
                    const bodyText = document.body && document.body.innerText || '';
                    const html = document.documentElement && document.documentElement.outerHTML || '';
                    return {
                        readyState: document.readyState || '',
                        listitems: document.querySelectorAll('[role="listitem"]').length,
                        links: document.querySelectorAll('a[href]').length,
                        bodyTextLength: bodyText.length,
                        htmlLength: html.length,
                    };
                }"""
            )
        except Exception as exc:
            latest = {"error": str(exc)}
        signature = (
            latest.get("readyState"),
            latest.get("listitems"),
            latest.get("links"),
            latest.get("bodyTextLength"),
        )
        if signature == last_signature and int(latest.get("listitems") or 0) > 0:
            stable_cycles += 1
            if stable_cycles >= 2:
                break
        else:
            stable_cycles = 0
            last_signature = signature
        await page.wait_for_timeout(700)
    return latest


def _settle_summary(metrics: Dict[str, Any]) -> str:
    if not metrics:
        return "settle_metrics=empty"
    if metrics.get("error"):
        return f"settle_error={metrics.get('error')}"
    return (
        f"readyState={metrics.get('readyState')!r} "
        f"listitems={metrics.get('listitems')} links={metrics.get('links')} "
        f"bodyTextLength={metrics.get('bodyTextLength')} htmlLength={metrics.get('htmlLength')}"
    )


async def _scroll_detail_page(page, scrolls: int, pause_min_ms: int = 500, pause_max_ms: int = 1200) -> None:
    count = max(0, min(int(scrolls or 0), 50))
    if count <= 0:
        return
    min_pause = max(50, int(pause_min_ms or 500))
    max_pause = max(min_pause, int(pause_max_ms or min_pause))
    for _ in range(count):
        await page.evaluate(
            r"""() => {
                const root = document.scrollingElement || document.documentElement || document.body;
                if (!root) return false;
                const distance = Math.max(500, Math.floor((window.innerHeight || root.clientHeight || 900) * 0.85));
                window.scrollBy(0, distance);
                root.scrollTop = Math.max(root.scrollTop || 0, (window.scrollY || 0));
                return true;
            }"""
        )
        await page.wait_for_timeout(random.randint(min_pause, max_pause))


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
    detail_url_seen: Optional[Set[str]] = None,
    detail_url_seen_lock: Optional[threading.Lock] = None,
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
        slow_detail_duplicates = 0
        slow_detail_wait_timeout = 0
        slow_detail_field_empty = 0
        slow_detail_errors = 0
        detail_url_xpath = str(slow.get("detail_url_xpath") or "").strip()
        detail_url_within_block = bool(slow.get("detail_url_within_block", True))
        wait_xpath = str(slow.get("wait_xpath") or "").strip()
        detail_scrolls = max(0, min(int(slow.get("detail_scrolls") or 0), 50))
        slow_fields = slow.get("fields") if isinstance(slow.get("fields"), list) else []
        if slow_enabled:
            logger.info(
                "[source][slow] Enabled for request %s: detail_url_xpath=%r detail_url_scope=%s wait_xpath=%r detail_scrolls=%d fields=%d",
                request.id,
                detail_url_xpath,
                "block" if detail_url_within_block else "page",
                wait_xpath,
                detail_scrolls,
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
                    detail_lookup_xpath = _block_scoped_xpath(detail_url_xpath) if detail_url_within_block else detail_url_xpath
                    detail_values = await _xpath_values(
                        page,
                        detail_lookup_xpath,
                        root_handle=block if detail_url_within_block else None,
                    )
                    detail_url = _absolute_url(page.url, detail_values[0] if detail_values else "")
                    fallback_url_source = ""
                    if not detail_url:
                        detail_url, fallback_url_source = _fallback_detail_url_from_contact(contact, page.url)
                        if detail_url:
                            logger.info(
                                "[source][slow] Request %s block=%d detail URL XPath returned none; using fast field fallback %s=%s",
                                request.id,
                                block_index,
                                fallback_url_source,
                                detail_url,
                            )
                    if not detail_url:
                        slow_detail_url_missing += 1
                        logger.warning(
                            "[source][slow] Request %s block=%d no detail URL. detail_url_xpath=%r effective_xpath=%r scope=%s extracted_values=%d fast_keys=%s fast_fields=%s",
                            request.id,
                            block_index,
                            detail_url_xpath,
                            detail_lookup_xpath,
                            "block" if detail_url_within_block else "page",
                            len(detail_values),
                            ", ".join(_contact_keys(contact)) or "none",
                            await _field_debug_summary(page, fast_fields, root_handle=block),
                        )
                    else:
                        detail_dedupe_key = _detail_url_dedupe_key(detail_url)
                        if detail_dedupe_key:
                            should_skip_detail = False
                            if detail_url_seen is not None and detail_url_seen_lock is not None:
                                with detail_url_seen_lock:
                                    if detail_dedupe_key in detail_url_seen:
                                        should_skip_detail = True
                                    else:
                                        detail_url_seen.add(detail_dedupe_key)
                            elif detail_dedupe_key in seen:
                                should_skip_detail = True
                            else:
                                seen.add(detail_dedupe_key)
                            if should_skip_detail:
                                slow_detail_duplicates += 1
                                skipped_duplicates += 1
                                logger.info(
                                    "[source][slow] Request %s block=%d skipping duplicate detail URL: %s",
                                    request.id,
                                    block_index,
                                    detail_url,
                                )
                                continue
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
                            if detail_scrolls:
                                logger.info("[source][slow] Request %s block=%d scrolling detail page %d time(s).", request.id, block_index, detail_scrolls)
                                await _scroll_detail_page(detail_page, detail_scrolls)
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
                            settle_metrics = await _wait_for_detail_page_settle(detail_page)
                            logger.info(
                                "[source][slow] Request %s block=%d detail page settled before extraction. %s",
                                request.id,
                                block_index,
                                _settle_summary(settle_metrics),
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
            "[source] Request %s summary: blocks=%d sent=%d skipped_duplicates=%d skipped_missing_required=%d slow_attempted=%d slow_opened=%d slow_missing_url=%d slow_detail_duplicates=%d slow_wait_timeout=%d slow_empty_fields=%d slow_errors=%d",
            request.id,
            len(blocks),
            total,
            skipped_duplicates,
            skipped_missing_required,
            slow_detail_attempted,
            slow_detail_opened,
            slow_detail_url_missing,
            slow_detail_duplicates,
            slow_detail_wait_timeout,
            slow_detail_field_empty,
            slow_detail_errors,
        )
        return total
    finally:
        await runtime.close()


async def debug_source_template(
    *,
    source: dict,
    query: str,
    detail_url: str = "",
    scrape_mode: str = "fast",
    show_browser: bool = False,
    proxy_url: str = "",
    max_scrolls_override: Optional[int] = None,
    max_blocks: int = 5,
    max_detail_pages: int = 3,
    detail_hold_seconds: float = 0.0,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    stop_signal: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    def emit(message: str) -> None:
        if log:
            log(message)
        logger.info("[source-debug] %s", message)

    def emit_progress(event: Dict[str, Any]) -> None:
        if progress:
            try:
                progress(dict(event))
            except Exception:
                pass

    def stopped() -> bool:
        return bool(stop_signal and stop_signal())

    def compact_values(values: List[str], limit: int = 3) -> List[str]:
        out: List[str] = []
        for value in values[:limit]:
            text = _clean(value)
            out.append(text[:300])
        return out

    config = source.get("config") if isinstance(source.get("config"), dict) else {}
    start_template = str(config.get("start_url_template") or "").strip()
    manual_detail_url = _strip_detail_url_trailing_slash(str(detail_url or ""))
    url = manual_detail_url or start_template.replace("{query}", urllib.parse.quote_plus(str(query or "").strip()))
    fast = config.get("fast") if isinstance(config.get("fast"), dict) else {}
    slow = config.get("slow") if isinstance(config.get("slow"), dict) else {}
    nav = dict(config.get("navigation") if isinstance(config.get("navigation"), dict) else {})
    if max_scrolls_override is not None:
        nav["max_scrolls"] = max(1, int(max_scrolls_override))
    debug_config = dict(config)
    debug_config["navigation"] = nav

    block_xpath = str(fast.get("block_xpath") or "").strip()
    fast_fields = fast.get("fields") if isinstance(fast.get("fields"), list) else []
    slow_enabled = bool(slow.get("enabled")) and str(scrape_mode or "fast").strip().lower() == "slow"
    slow_fields = slow.get("fields") if isinstance(slow.get("fields"), list) else []
    detail_url_xpath = str(slow.get("detail_url_xpath") or "").strip()
    detail_url_within_block = bool(slow.get("detail_url_within_block", True))
    wait_xpath = str(slow.get("wait_xpath") or "").strip()
    detail_scrolls = max(0, min(int(slow.get("detail_scrolls") or 0), 50))

    result: Dict[str, Any] = {
        "ok": False,
        "source_name": source.get("name") or source.get("id") or "debug-source",
        "query": query,
        "detail_url_only": bool(manual_detail_url),
        "scrape_mode": scrape_mode,
        "opened_url": url,
        "title": "",
        "final_url": "",
        "block_xpath": block_xpath,
        "block_count": 0,
        "fast_samples": [],
        "detail_samples": [],
        "errors": [],
        "screenshot_base64": "",
    }

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

        if manual_detail_url:
            emit(f"Opening detail URL {manual_detail_url}")
            detail_sample: Dict[str, Any] = {
                "block_index": 1,
                "detail_url_xpath": "",
                "effective_xpath": "",
                "scope": "manual",
                "xpath_matches": 0,
                "fallback_source": "manual_detail_url",
                "detail_url": manual_detail_url,
                "wait_xpath": wait_xpath,
                "wait_xpath_matched": None,
                "title": "",
                "final_url": "",
                "diagnostics": "",
                "fields": [],
                "error": "",
            }
            emit_progress({"type": "detail_start", "block_index": 1, "detail": dict(detail_sample)})
            try:
                await page.goto(manual_detail_url, wait_until="domcontentloaded", timeout=60000)
                result["title"] = await page.title()
                result["final_url"] = page.url
                detail_sample["title"] = result["title"]
                detail_sample["final_url"] = result["final_url"]
                emit(f"Detail loaded title={result['title']!r} url={page.url}")
                emit_progress({"type": "detail_update", "block_index": 1, "detail": dict(detail_sample)})

                try:
                    await page.wait_for_function("() => document.body || document.documentElement", timeout=20000)
                    detail_sample["diagnostics"] = await _page_diagnostics(page)
                    emit(f"Detail DOM ready. {detail_sample['diagnostics']}")
                except Exception as exc:
                    detail_sample["error"] = f"Detail DOM wait warning: {exc}"
                    detail_sample["diagnostics"] = await _page_diagnostics(page)
                    emit(f"Detail DOM wait warning: {exc}. {detail_sample['diagnostics']}")
                emit_progress({"type": "detail_update", "block_index": 1, "detail": dict(detail_sample)})

                if detail_scrolls:
                    emit(f"Scrolling detail page {detail_scrolls} time(s) before extraction.")
                    await _scroll_detail_page(page, detail_scrolls)
                    detail_sample["diagnostics"] = await _page_diagnostics(page)
                    emit(f"Detail page after scroll. {detail_sample['diagnostics']}")
                    emit_progress({"type": "detail_update", "block_index": 1, "detail": dict(detail_sample)})

                if wait_xpath:
                    try:
                        await page.wait_for_selector(f"xpath={wait_xpath}", timeout=15000)
                        detail_sample["wait_xpath_matched"] = True
                        emit(f"Wait XPath matched: {wait_xpath}")
                    except Exception as exc:
                        detail_sample["wait_xpath_matched"] = False
                        detail_sample["error"] = f"Wait XPath did not match: {exc}"
                        emit(f"Wait XPath did not match: {wait_xpath}; {exc}")
                    emit_progress({"type": "detail_update", "block_index": 1, "detail": dict(detail_sample)})

                settle_metrics = await _wait_for_detail_page_settle(page)
                emit(f"Detail page settled before extraction. {_settle_summary(settle_metrics)}")
                detail_sample["diagnostics"] = await _page_diagnostics(page)
                emit_progress({"type": "detail_update", "block_index": 1, "detail": dict(detail_sample)})

                for field in slow_fields:
                    label = _field_label(field)
                    xpath = str(field.get("xpath") or "").strip()
                    regex = str(field.get("regex") or "").strip()
                    values, use_content, strip_html = await _field_xpath_values(page, field)
                    value, field_meta = await _extract_field_value_with_meta(page, field)
                    attempts = _regex_attempts(values, regex)
                    detail_sample["fields"].append({
                        "label": label,
                        "xpath": xpath,
                        "regex": regex,
                        "matches": len(values),
                        "value": value,
                        "previews": compact_values(values),
                        "regex_attempts": attempts,
                        "matched_attempt_index": _matched_attempt_index(attempts),
                        "run_regex_within_xpath_content": use_content,
                        "strip_html_before_regex": strip_html,
                        "fallback_source": field_meta.get("fallback_source", ""),
                        "email_candidates": field_meta.get("email_candidates", {}),
                    })
                    emit(
                        f"Detail field {label!r} matches={len(values)} "
                        f"value={value[:160]!r} fallback={field_meta.get('fallback_source', '') or '-'}"
                    )
                    if field_meta.get("email_candidates"):
                        emit(f"Detail field {label!r} email_candidates={field_meta.get('email_candidates')}")
                    emit_progress({"type": "detail_field", "block_index": 1, "detail": dict(detail_sample)})

                try:
                    import base64
                    screenshot_bytes = await page.screenshot(type="jpeg", quality=70, full_page=False)
                    result["screenshot_base64"] = base64.b64encode(screenshot_bytes).decode("ascii")
                except Exception as exc:
                    emit(f"Screenshot failed: {exc}")

                hold_ms = max(0, int(float(detail_hold_seconds or 0) * 1000))
                if hold_ms:
                    emit(f"Holding detail page open for {hold_ms / 1000:.1f}s")
                    await page.wait_for_timeout(hold_ms)
            except Exception as exc:
                detail_sample["error"] = str(exc)
                result["errors"].append(str(exc))
                emit(f"Manual detail debug failed: {exc}")
                emit_progress({"type": "detail_update", "block_index": 1, "detail": dict(detail_sample)})

            result["detail_samples"].append(detail_sample)
            emit_progress({"type": "detail_complete", "block_index": 1, "detail": detail_sample})
            result["ok"] = True
            emit("Manual detail debug run completed.")
            return result

        emit(f"Opening {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_function("() => document.body || document.documentElement", timeout=20000)
        except Exception as exc:
            result["errors"].append(f"Page DOM wait failed: {exc}")
            emit(f"Page DOM wait failed: {exc}")
        await page.wait_for_timeout(1200)
        result["title"] = await page.title()
        result["final_url"] = page.url
        emit(f"Loaded title={result['title']!r} url={page.url}")

        if block_xpath:
            try:
                await page.wait_for_selector(f"xpath={block_xpath}", timeout=30000)
                emit("Initial block XPath matched.")
            except Exception as exc:
                warning = f"Initial block XPath did not match within 30s: {exc}"
                result["errors"].append(warning)
                emit(warning)

        await _navigate_results(page, debug_config, stopped)
        blocks = await _query_nodes(page, block_xpath)
        result["block_count"] = len(blocks)
        emit(f"Collected {len(blocks)} blocks with block XPath.")
        emit_progress({"type": "blocks_collected", "block_count": len(blocks)})

        try:
            import base64
            screenshot_bytes = await page.screenshot(type="jpeg", quality=70, full_page=False)
            result["screenshot_base64"] = base64.b64encode(screenshot_bytes).decode("ascii")
        except Exception as exc:
            emit(f"Screenshot failed: {exc}")

        for block_index, block in enumerate(blocks[:max(1, int(max_blocks or 1))], start=1):
            if stopped():
                break
            sample: Dict[str, Any] = {"block_index": block_index, "fields": [], "contact_keys": []}
            contact = await _extract_fields(page, fast_fields, block, page.url)
            sample["contact_keys"] = _contact_keys(contact)
            for field in fast_fields:
                label = _field_label(field)
                xpath = str(field.get("xpath") or "").strip()
                regex = str(field.get("regex") or "").strip()
                values, use_content, strip_html = await _field_xpath_values(page, field, root_handle=block)
                value, field_meta = await _extract_field_value_with_meta(page, field, root_handle=block)
                attempts = _regex_attempts(values, regex)
                sample["fields"].append({
                    "label": label,
                    "xpath": xpath,
                    "regex": regex,
                    "matches": len(values),
                    "value": value,
                    "previews": compact_values(values),
                    "regex_attempts": attempts,
                    "matched_attempt_index": _matched_attempt_index(attempts),
                    "run_regex_within_xpath_content": use_content,
                    "strip_html_before_regex": strip_html,
                    "fallback_source": field_meta.get("fallback_source", ""),
                    "email_candidates": field_meta.get("email_candidates", {}),
                })
            result["fast_samples"].append(sample)
            emit_progress({"type": "fast_block", "block_index": block_index, "sample": sample})

            if slow_enabled and len(result["detail_samples"]) < max(0, int(max_detail_pages or 0)):
                detail_lookup_xpath = _block_scoped_xpath(detail_url_xpath) if detail_url_within_block else detail_url_xpath
                detail_values = await _xpath_values(page, detail_lookup_xpath, root_handle=block if detail_url_within_block else None)
                detail_url = _absolute_url(page.url, detail_values[0] if detail_values else "")
                fallback_source = ""
                if not detail_url:
                    detail_url, fallback_source = _fallback_detail_url_from_contact(contact, page.url)
                detail_sample: Dict[str, Any] = {
                    "block_index": block_index,
                    "detail_url_xpath": detail_url_xpath,
                    "effective_xpath": detail_lookup_xpath,
                    "scope": "block" if detail_url_within_block else "page",
                    "xpath_matches": len(detail_values),
                    "fallback_source": fallback_source,
                    "detail_url": detail_url,
                    "wait_xpath": wait_xpath,
                    "wait_xpath_matched": None,
                    "title": "",
                    "final_url": "",
                    "diagnostics": "",
                    "fields": [],
                    "error": "",
                }
                emit_progress({"type": "detail_start", "block_index": block_index, "detail": dict(detail_sample)})
                if not detail_url:
                    detail_sample["error"] = "No detail URL from XPath or fast-field fallback."
                    emit(f"Block {block_index}: no detail URL. effective_xpath={detail_lookup_xpath!r} scope={detail_sample['scope']}")
                    emit_progress({"type": "detail_update", "block_index": block_index, "detail": dict(detail_sample)})
                else:
                    emit(f"Block {block_index}: opening detail URL {detail_url}")
                    detail_page = await context.new_page()
                    try:
                        await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                        detail_sample["title"] = await detail_page.title()
                        detail_sample["final_url"] = detail_page.url
                        emit(
                            f"Block {block_index}: detail loaded title={detail_sample['title']!r} "
                            f"url={detail_sample['final_url']}"
                        )
                        emit_progress({"type": "detail_update", "block_index": block_index, "detail": dict(detail_sample)})
                        try:
                            await detail_page.wait_for_function("() => document.body || document.documentElement", timeout=15000)
                            detail_sample["diagnostics"] = await _page_diagnostics(detail_page)
                            emit(f"Block {block_index}: detail DOM ready. {detail_sample['diagnostics']}")
                        except Exception as exc:
                            detail_sample["error"] = f"Detail DOM wait warning: {exc}"
                            detail_sample["diagnostics"] = await _page_diagnostics(detail_page)
                            emit(f"Block {block_index}: detail DOM wait warning: {exc}. {detail_sample['diagnostics']}")
                        emit_progress({"type": "detail_update", "block_index": block_index, "detail": dict(detail_sample)})
                        if detail_scrolls:
                            emit(f"Block {block_index}: scrolling detail page {detail_scrolls} time(s) before extraction.")
                            await _scroll_detail_page(detail_page, detail_scrolls)
                            detail_sample["diagnostics"] = await _page_diagnostics(detail_page)
                            emit(f"Block {block_index}: detail page after scroll. {detail_sample['diagnostics']}")
                            emit_progress({"type": "detail_update", "block_index": block_index, "detail": dict(detail_sample)})
                        if wait_xpath:
                            try:
                                await detail_page.wait_for_selector(f"xpath={wait_xpath}", timeout=15000)
                                detail_sample["wait_xpath_matched"] = True
                                emit(f"Block {block_index}: wait XPath matched: {wait_xpath}")
                            except Exception as exc:
                                detail_sample["wait_xpath_matched"] = False
                                detail_sample["error"] = f"Wait XPath did not match: {exc}"
                                emit(f"Block {block_index}: wait XPath did not match: {wait_xpath}; {exc}")
                            emit_progress({"type": "detail_update", "block_index": block_index, "detail": dict(detail_sample)})
                        settle_metrics = await _wait_for_detail_page_settle(detail_page)
                        emit(f"Block {block_index}: detail page settled before extraction. {_settle_summary(settle_metrics)}")
                        detail_sample["diagnostics"] = await _page_diagnostics(detail_page)
                        emit_progress({"type": "detail_update", "block_index": block_index, "detail": dict(detail_sample)})

                        for field in slow_fields:
                            label = _field_label(field)
                            xpath = str(field.get("xpath") or "").strip()
                            regex = str(field.get("regex") or "").strip()
                            values, use_content, strip_html = await _field_xpath_values(detail_page, field)
                            value, field_meta = await _extract_field_value_with_meta(detail_page, field)
                            attempts = _regex_attempts(values, regex)
                            detail_sample["fields"].append({
                                "label": label,
                                "xpath": xpath,
                                "regex": regex,
                                "matches": len(values),
                                "value": value,
                                "previews": compact_values(values),
                                "regex_attempts": attempts,
                                "matched_attempt_index": _matched_attempt_index(attempts),
                                "run_regex_within_xpath_content": use_content,
                                "strip_html_before_regex": strip_html,
                                "fallback_source": field_meta.get("fallback_source", ""),
                                "email_candidates": field_meta.get("email_candidates", {}),
                            })
                            emit(
                                f"Block {block_index}: field {label!r} matches={len(values)} "
                                f"value={value[:160]!r} fallback={field_meta.get('fallback_source', '') or '-'}"
                            )
                            if field_meta.get("email_candidates"):
                                emit(f"Block {block_index}: field {label!r} email_candidates={field_meta.get('email_candidates')}")
                            emit_progress({"type": "detail_field", "block_index": block_index, "detail": dict(detail_sample)})
                    except Exception as exc:
                        detail_sample["error"] = str(exc)
                        emit(f"Block {block_index}: detail error {exc}")
                        emit_progress({"type": "detail_update", "block_index": block_index, "detail": dict(detail_sample)})
                    finally:
                        hold_ms = max(0, int(float(detail_hold_seconds or 0) * 1000))
                        if hold_ms:
                            emit(f"Block {block_index}: holding detail page open for {hold_ms / 1000:.1f}s")
                            await detail_page.wait_for_timeout(hold_ms)
                        await detail_page.close()
                        emit(f"Block {block_index}: detail page closed.")
                result["detail_samples"].append(detail_sample)
                emit_progress({"type": "detail_complete", "block_index": block_index, "detail": detail_sample})

        result["ok"] = True
        emit("Debug run completed.")
        return result
    except Exception as exc:
        result["errors"].append(str(exc))
        emit(f"Debug run failed: {exc}")
        return result
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

    detail_url_seen: Set[str] = set()
    detail_url_seen_lock = threading.Lock()

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
                total = asyncio.run(_scrape_request(
                    source,
                    item,
                    campaign_id,
                    batch_size,
                    api,
                    scrape_mode,
                    show_browser,
                    proxy_url,
                    should_stop,
                    detail_url_seen=detail_url_seen,
                    detail_url_seen_lock=detail_url_seen_lock,
                ))
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
