#!/usr/bin/env python3
"""
Batch Email Scraper wired to your Campaign API (timeout + hardened extraction)

Highlights
----------
• Per-page Playwright timeouts + per-domain total timeout (--domain-timeout)
• On timeout, kills & relaunches Chromium (toggle with --no-kill-on-timeout)
• Low-CPU fast pass first (JS disabled + asset blocking); only then JS-enabled retry
• JS-enabled pass allows third-party scripts (lots of sites render via CDNs)
• Smarter email extraction: JSON-LD, Cloudflare /cdn-cgi protection, HTML entities, obfuscated "at"/"dot"
• Limits extra same-site pages (--links)
• Saves each found email immediately to /api/campaign/{id}/email_update
• Clear console markers: [WILL SEARCH], [START], [RETRY], [TIMEOUT], DONE lines with durations
"""

import asyncio
import argparse
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urljoin

LOCAL_DEPS = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)

from bs4 import BeautifulSoup
from browser_backend import (
    AsyncBrowserRuntime,
    backend_display_name,
    install_async_blocked_resource_routes,
    normalize_proxy_url,
    should_block_resource_url,
)
from email_quality import (
    filter_valid_emails,
    is_allowed_domain as quality_is_allowed_domain,
    is_same_business_domain,
    pick_best_business_email,
    set_min_domain_letters,
)

# Prefer 'requests' for better CA bundle; fall back to urllib
try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

import ssl
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_BASE_URL = "https://scrapiq.leadtechx.com"

# ----- Logging -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", force=True)
logger = logging.getLogger("email_scraper")

try:
    sys.stdout.reconfigure(line_buffering=True)  # ensure immediate print flush
except Exception:
    pass

# -----------------------------
# URL helpers
# -----------------------------
def clean_url(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    url = url.strip().strip('"')
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        return parsed.geturl()
    except Exception:
        return ""


def get_host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def get_domain_from_url(url: str) -> str:
    """Extract domain from URL using extension's logic with comprehensive TLD list"""
    try:
        # Comprehensive TLD list from extension
        tlds = {
            'ac', 'ad', 'ae', 'af', 'ag', 'ai', 'al', 'am', 'an', 'ao', 'aq', 'ar', 'as', 'at', 'au',
            'aw', 'ax', 'az', 'ba', 'bb', 'bd', 'be', 'bf', 'bg', 'bh', 'bi', 'bj', 'bm', 'bn', 'bo',
            'br', 'bs', 'bt', 'bv', 'bw', 'by', 'bz', 'ca', 'cc', 'cd', 'cf', 'cg', 'ch', 'ci', 'ck',
            'cl', 'cm', 'cn', 'co', 'cr', 'cu', 'cv', 'cw', 'cx', 'cy', 'cz', 'de', 'dj', 'dk', 'dm',
            'do', 'dz', 'ec', 'ee', 'eg', 'eh', 'er', 'es', 'et', 'eu', 'fi', 'fj', 'fk', 'fm', 'fo',
            'fr', 'ga', 'gb', 'gd', 'ge', 'gf', 'gg', 'gh', 'gi', 'gl', 'gm', 'gn', 'gp', 'gq', 'gr',
            'gs', 'gt', 'gu', 'gw', 'gy', 'hk', 'hm', 'hn', 'hr', 'ht', 'hu', 'id', 'ie', 'il', 'im',
            'in', 'io', 'iq', 'ir', 'is', 'it', 'je', 'jm', 'jo', 'jp', 'ke', 'kg', 'kh', 'ki', 'km',
            'kn', 'kp', 'kr', 'kw', 'ky', 'kz', 'la', 'lb', 'lc', 'li', 'lk', 'lr', 'ls', 'lt', 'lu',
            'lv', 'ly', 'ma', 'mc', 'md', 'me', 'mf', 'mg', 'mh', 'mk', 'ml', 'mm', 'mn', 'mo', 'mp',
            'mq', 'mr', 'ms', 'mt', 'mu', 'mv', 'mw', 'mx', 'my', 'mz', 'na', 'nc', 'ne', 'nf', 'ng',
            'ni', 'nl', 'no', 'np', 'nr', 'nu', 'nz', 'om', 'pa', 'pe', 'pf', 'pg', 'ph', 'pk', 'pl',
            'pm', 'pn', 'pr', 'ps', 'pt', 'pw', 'py', 'qa', 're', 'ro', 'rs', 'ru', 'rw', 'sa', 'sb',
            'sc', 'sd', 'se', 'sg', 'sh', 'si', 'sj', 'sk', 'sl', 'sm', 'sn', 'so', 'sr', 'ss', 'st',
            'su', 'sv', 'sx', 'sy', 'sz', 'tc', 'td', 'tf', 'tg', 'th', 'tj', 'tk', 'tl', 'tm', 'tn',
            'to', 'tr', 'tt', 'tv', 'tw', 'tz', 'ua', 'ug', 'uk', 'us', 'uy', 'uz', 'va', 'vc', 've',
            'vg', 'vi', 'vn', 'vu', 'wf', 'ws', 'xk', 'ye', 'yt', 'za', 'zm', 'zw'
        }

        url_obj = urlparse(url)
        host_parts = url_obj.netloc.lower().split('.')

        # Remove www prefix if present
        if host_parts[0] == 'www':
            host_parts = host_parts[1:]

        if len(host_parts) < 2:
            return ""

        # Use extension's logic: if last part is TLD, use third-to-last, otherwise second-to-last
        if tlds and host_parts[-1] in tlds:
            return host_parts[-3] if len(host_parts) >= 3 else host_parts[-2]
        else:
            return host_parts[-2] if len(host_parts) >= 2 else host_parts[-1]
    except Exception:
        return ""


def same_site(a: str, b: str) -> bool:
    ha, hb = get_host(a), get_host(b)
    if not ha or not hb:
        return False
    if ha.startswith("www."):
        ha = ha[4:]
    if hb.startswith("www."):
        hb = hb[4:]
    return ha == hb


def decode_cf_email(encoded: str) -> str:
    """Decode Cloudflare data-cfemail obfuscation - same logic as extension."""
    if not encoded or len(encoded) < 2:
        return ""
    try:
        decoded = ''
        key = int(encoded[:2], 16)
        for i in range(2, len(encoded), 2):
            char_code = int(encoded[i:i+2], 16) ^ key
            decoded += chr(char_code)
        return decoded
    except Exception:
        return ""


def decode_cf_href(href: str) -> str:
    """
    Decode Cloudflare '/cdn-cgi/l/email-protection#<hex...>' style links.
    """
    try:
        if "/cdn-cgi/l/email-protection" not in href:
            return ""
        hex_part = href.split("#", 1)[1]
        return decode_cf_email(hex_part)
    except Exception:
        return ""


# Email regex pattern - same as extension
EMAIL_RE_LIST = [re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")]

# Enhanced TARGET_PATHS from email_scraper9 - more comprehensive and specific
TARGET_PATHS = [
    "/contact", "/contact-us", "/contact-me",
    "/about", "/about-us", "/about-me",
    "/team", "/our-team", "/meet-the-team",
    "/support", "/customer-service", "/help", "/feedback",
    "/sales", "privacy", "return", "location", "policy", "faq",
    "/estimate", "/free-estimate", "/request-quote", "/consultation",
    "/appointment", "/schedule", "/get-in-touch", "/book",
    "/company", "/who-we-are", "/staff", "/locations",
    "/impressum", "/legal", "/privacy-policy", "/info", "/information",
    "/services", "/quote", "/imprint",
]

# Smart prioritization system from email_scraper9 - prioritize contact-like pages first
CONTACT_PRIORITY = [
    ("contact-us", 0), ("contactus", 0), ("contact-me", 0), ("contact", 0),
    ("get-in-touch", 0), ("consultation", 0), ("request-quote", 0),
    ("estimate", 0), ("free-estimate", 0),
    ("appointment", 1), ("book", 1), ("schedule", 1),
    ("about-us", 2), ("about-me", 2), ("about", 2),
    ("team", 2), ("our-team", 2), ("meet-the-team", 2),
    ("support", 2), ("customer-service", 2),
    ("privacy-policy", 3), ("privacy", 3), ("legal", 3),
    ("services", 4), ("info", 4),
]

# Enhanced filtering - same patterns as extension for better junk email filtering
BLOCK_SUBSTRINGS = [
    ".png", ".jpg", ".jpeg", ".gif", ".webp", "wixpress.com", "sentry.io",
    "noreply", "no-reply", "abuse", "subscribe", "mailer-daemon",
    "example.com", "domain.com", "email.com", "yourname", "wix.com",
    # Extension additional patterns
    ".js", ".css", ".html", ".php", ".asp",
    # Phone number patterns in domain
    "-", "order", "call", "phone"
]

# Additional smart junk filtering from email_scraper9
JUNK_SUBSTRINGS = {
    "example.com", "domain.com", "email.com", "yourname", "no-reply", "noreply",
    "mailer-daemon", "wix.com", "wixpress.com"
}

# Public email providers for smart filtering
PUBLIC_PROVIDERS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "proton.me", "protonmail.com", "yandex.com", "gmx.com", "mail.com"
}

PREFERRED_MAILBOX_ORDER = ["info@", "contact@", "hello@", "support@", "sales@", "admin@"]

# Only accept emails from common business TLDs or known public providers.
ALLOWED_DOMAIN_SUFFIXES = {
    ".com", ".net", ".org", ".edu", ".gov", ".mil",
    ".us", ".uk", ".ca", ".au", ".nz",
    ".de", ".fr", ".it", ".es", ".nl", ".be", ".ch", ".at",
    ".se", ".no", ".dk", ".fi", ".ie", ".pt", ".pl", ".cz", ".sk", ".hu", ".ro", ".bg", ".gr",
    ".lt", ".lv", ".ee",
    ".il", ".tr", ".ua",
    ".br", ".mx", ".ar", ".cl", ".co", ".pe",
    ".za", ".ng", ".ke",
    ".cn", ".jp", ".kr", ".sg", ".my", ".id", ".th", ".vn", ".ph", ".hk", ".tw",
    ".ae", ".sa",
    ".io",
}


def is_allowed_domain(domain_part: str) -> bool:
    return quality_is_allowed_domain(domain_part)


def priority_score(url_or_text: str) -> int:
    """Smart prioritization based on contact relevance"""
    s = url_or_text.lower()
    for key, score in CONTACT_PRIORITY:
        if key in s:
            return score
    return 99


def deobfuscate_text_for_emails(text: str) -> str:
    """
    Turn things like 'name (at) example (dot) com' into 'name@example.com'.
    """
    t = " " + text.lower() + " "
    t = re.sub(r"\s*(?:\(|\[)?at(?:\)|\])?\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*(?:\(|\[)?dot(?:\)|\])?\s*", ".", t, flags=re.I)
    t = re.sub(r"\s+@\s+", "@", t)
    t = re.sub(r"\s*\.\s*", ".", t)
    return t.strip()


def strip_url_prefix(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("http://"):
        v = v[7:]
    elif v.startswith("https://"):
        v = v[8:]
    return v.lstrip("/")


def normalize_email_candidate(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip().strip("<>\"'")
    if s.lower().startswith("mailto:"):
        s = s.split(":", 1)[1]
    s = s.split("?", 1)[0]
    s = s.split("#", 1)[0]
    s = s.split("&", 1)[0]
    s = s.split(",", 1)[0]
    s = s.split(";", 1)[0]
    return s.strip().lower()


def collect_emails_from_jsonld(soup: BeautifulSoup) -> Set[str]:
    found: Set[str] = set()
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for k, v in item.items():
                    if k.lower() == "email" and isinstance(v, str):
                        found.add(v.lower())
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(item, list):
                stack.extend(item)
    return found


async def _extract_emails_from_html(html: str) -> Set[str]:
    found: Set[str] = set()
    if not html:
        return found

    # Regex over raw HTML (catches mailto in scripts, onclick, etc.)
    for rx in EMAIL_RE_LIST:
        for m in rx.findall(html):
            email = normalize_email_candidate(m)
            if email:
                found.add(email)

    soup = BeautifulSoup(html, "html.parser")

    # Normal mailto anchors
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if href.lower().startswith("mailto:"):
            email = normalize_email_candidate(href)
            if email:
                found.add(email)

    # Email attributes used by some builders (e.g., email="user@host.com")
    for tag in soup.find_all(True):
        for attr in ("email", "data-email"):
            val = tag.get(attr)
            if val and "@" in val:
                email = normalize_email_candidate(val)
                if email:
                    found.add(email)

    # Cloudflare protected links
    for a in soup.find_all("a"):
        href = (a.get("href") or "").lower()
        if "/cdn-cgi/l/email-protection" in href:
            dec = decode_cf_href(href)
            if dec:
                found.add(dec)

    # Cloudflare __cf_email__ spans
    for el in soup.select(".__cf_email__"):
        enc = el.get("data-cfemail")
        dec = decode_cf_email(enc)
        if dec:
            found.add(dec.lower())

    # JSON-LD email fields
    for e in collect_emails_from_jsonld(soup):
        email = normalize_email_candidate(e)
        if email:
            found.add(email)

    # Body text (entities decoded), then deobfuscate and regex
    body_text = soup.get_text(" ", strip=True)
    if body_text:
        body_text = deobfuscate_text_for_emails(body_text)
        for rx in EMAIL_RE_LIST:
            for m in rx.findall(body_text):
                email = normalize_email_candidate(m)
                if email:
                    found.add(email)

    return found


def find_facebook_page_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    """Find Facebook page links on the website"""
    facebook_links = []

    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if not href:
            continue

        # Convert relative URLs to absolute
        full_url = urljoin(base_url, href)

        # Check if it's a Facebook page link
        if ("facebook.com/" in full_url.lower() and
            "facebook.com/sharer" not in full_url.lower() and
            "facebook.com/tr" not in full_url.lower() and
            "facebook.com/login" not in full_url.lower()):
            facebook_links.append(full_url)

    return facebook_links


async def extract_emails_from_facebook(
    browser,
    facebook_url: str,
    timeout: float = 8.0
) -> Set[str]:
    """Extract emails from a Facebook page (relaxed validation)"""
    logger.info(f"📘 FACEBOOK: Starting extraction from {facebook_url}")

    context = await browser.new_context(
        java_script_enabled=True,  # Facebook needs JS
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"),
    )
    await install_async_blocked_resource_routes(context)

    # Allow third-party requests for Facebook
    context.set_default_timeout(int(timeout * 1000))
    context.set_default_navigation_timeout(int(timeout * 1000))

    page = await context.new_page()
    html = ""
    try:
        logger.info(f"📘 FACEBOOK: Navigating to {facebook_url}...")
        resp = await page.goto(facebook_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        if resp and resp.status == 200:
            logger.info(f"📘 FACEBOOK: Page loaded successfully (status {resp.status})")
            # Wait a bit longer for Facebook content to load
            await page.wait_for_timeout(2000)
            html = await page.content()
            logger.info(f"📘 FACEBOOK: Retrieved HTML content ({len(html)} chars)")
        else:
            logger.warning(f"📘 FACEBOOK: Bad response - status: {resp.status if resp else 'None'}")
    except Exception as e:
        logger.warning(f"📘 FACEBOOK: Navigation failed: {e}")
    finally:
        try:
            await context.close()
        except Exception:
            pass

    if not html:
        logger.warning(f"📘 FACEBOOK: No HTML content retrieved from {facebook_url}")
        return set()

    logger.info(f"📘 FACEBOOK: Extracting emails from HTML content...")

    # Facebook-specific extraction: prioritize span tag emails (where FB displays contact info)
    span_emails = set()
    all_emails = set()

    try:
        soup = BeautifulSoup(html, "html.parser")

        # First: look for emails specifically in span tags (legitimate FB contact info)
        for span in soup.find_all("span"):
            span_text = span.get_text()
            if span_text and '@' in span_text:
                for rx in EMAIL_RE_LIST:
                    for match in rx.findall(span_text):
                        span_emails.add(match.lower())

        logger.info(f"📘 FACEBOOK: Found {len(span_emails)} emails in span tags: {list(span_emails)}")

        # Fallback: extract from full HTML if no span emails found
        if not span_emails:
            all_emails = await _extract_emails_from_html(html)
            logger.info(f"📘 FACEBOOK: No span emails found, using full extraction: {len(all_emails)} emails")
        else:
            all_emails = span_emails

    except Exception as e:
        logger.warning(f"📘 FACEBOOK: BeautifulSoup parsing failed, using fallback extraction: {e}")
        all_emails = await _extract_emails_from_html(html)

    logger.info(f"📘 FACEBOOK: Raw extraction found {len(all_emails)} potential emails: {list(all_emails)[:5]}..." if len(all_emails) > 5 else f"📘 FACEBOOK: Raw extraction found emails: {list(all_emails)}")
    emails = all_emails

    # Facebook-specific filtering (less strict than main site)
    def is_valid_facebook_email(e: str) -> bool:
        if not e or "@" not in e:
            return False

        low = e.lower()

        # Check against basic block list
        for bad in BLOCK_SUBSTRINGS:
            if bad in low:
                return False

        try:
            username, host = low.split("@", 1)

            # Basic format validation
            if not username or not host or "." not in host:
                return False

            # Skip domains that look like fake/generated words (like 'last.schedule')
            domain_words = host.split('.')
            fake_patterns = ['last', 'schedule', 'first', 'next', 'previous', 'temp', 'tmp', 'placeholder']
            if any(fake in domain_words for fake in fake_patterns):
                return False

            # Allow only common business TLDs or known public providers
            if is_allowed_domain(host):
                return True

        except Exception:
            pass

        return False

    # Apply Facebook-specific validation
    valid_emails = {e for e in emails if is_valid_facebook_email(e)}
    logger.info(f"📘 FACEBOOK: After validation, {len(valid_emails)} emails remain: {list(valid_emails)}")

    return valid_emails


async def extract_emails_from_facebook_http(
    http_client,
    facebook_url: str,
    timeout: float = 8.0,
    facebook_proxy_url: str = "",
) -> Set[str]:
    if http_client is None:
        return set()
    candidates: Set[str] = set()
    targets = [facebook_url]
    if not facebook_url.rstrip("/").endswith("/about"):
        targets.append(facebook_url.rstrip("/") + "/about")

    proxy_url = normalize_proxy_url(facebook_proxy_url)
    for target in targets:
        try:
            html = http_client.get_text(
                target,
                headers={"Accept": "text/html,application/xhtml+xml"},
                proxy_url=proxy_url,
            )
        except Exception:
            html = ""
        if not html:
            continue
        found = await _extract_emails_from_html(html)
        candidates.update(found)
    return candidates


async def extract_emails_from_facebook_with_engine(
    browser,
    http_client,
    facebook_url: str,
    timeout: float,
    facebook_engine: str,
    facebook_proxy_url: str = "",
) -> Set[str]:
    engine = (facebook_engine or "camoufox").strip().lower()
    if engine == "scrapy":
        return await extract_emails_from_facebook_http(
            http_client,
            facebook_url,
            timeout=timeout,
            facebook_proxy_url=facebook_proxy_url,
        )
    return await extract_emails_from_facebook(browser, facebook_url, timeout=timeout)


async def extract_emails(
    browser,
    url: str,
    *,
    timeout: float = 8.0,
    recurse: bool = True,
    max_children: int = 5,
    js_enabled: bool = False,
    block_assets: bool = True,
    allow_third_party: bool = False,
    check_facebook: bool = False,
    http_client=None,
    facebook_engine: str = "camoufox",
    facebook_proxy_url: str = "",
    same_domain_only: bool = True,
) -> Set[str]:
    """
    One-shot fetch for a URL:
    - Creates a new context with js_enabled on/off
    - Optionally blocks heavy assets
    - Controls third-party requests
    - Sets Playwright default timeouts to 'timeout'
    """
    url = clean_url(url)
    if not url:
        return set()

    allowed_hosts = set()
    base_host = get_host(url)
    if base_host:
        allowed_hosts.add(base_host)

    context = await browser.new_context(
        java_script_enabled=js_enabled,
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"),
    )

    async def route_handler(route):
        if should_block_resource_url(route.request.url):
            return await route.abort()
        if not block_assets and allow_third_party:
            return await route.continue_()
        try:
            req_url = route.request.url
            if route.request.is_navigation_request():
                req_host = get_host(req_url)
                if req_host:
                    allowed_hosts.add(req_host)
                return await route.continue_()
        except Exception:
            pass
        rtype = route.request.resource_type
        # Always drop heavy assets
        if rtype in {"image", "media", "font", "stylesheet"}:
            return await route.abort()
        if not allow_third_party:
            try:
                req_url = route.request.url
                req_host = get_host(req_url)
                if req_host and req_host not in allowed_hosts:
                    return await route.abort()
            except Exception:
                pass
        return await route.continue_()

    await context.route("**/*", route_handler)
    context.set_default_timeout(int(timeout * 1000))
    context.set_default_navigation_timeout(int(timeout * 1000))

    page = await context.new_page()
    html = ""
    runtime_candidates: Set[str] = set()
    effective_url = url
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        if resp and resp.status == 200:
            effective_url = page.url or url
            if js_enabled:
                try:
                    await page.wait_for_timeout(1200)
                except Exception:
                    pass
            html = await page.content()
            try:
                mailtos = await page.eval_on_selector_all(
                    "a[href^='mailto:']",
                    "els => els.map(el => el.getAttribute('href')).filter(Boolean)"
                )
                for m in mailtos:
                    runtime_candidates.add(m)
            except Exception:
                pass
            try:
                attrs = await page.eval_on_selector_all(
                    "[email], [data-email]",
                    "els => els.map(el => (el.getAttribute('email') || el.getAttribute('data-email') || '')).filter(Boolean)"
                )
                for a in attrs:
                    runtime_candidates.add(a)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            await context.close()
        except Exception:
            pass

    if html:
        try:
            html = unicodedata.normalize("NFKC", html.encode().decode("utf-8", errors="ignore"))
        except Exception:
            pass

    emails = await _extract_emails_from_html(html)
    for cand in runtime_candidates:
        email = normalize_email_candidate(cand)
        if email:
            emails.add(email)

    if recurse and html:
        soup = BeautifulSoup(html, "html.parser")
        base = effective_url

        # Smart link discovery with prioritization
        priority_links: List[Tuple[int, str]] = []

        for a in soup.find_all("a"):
            href = a.get("href") or ""
            if href.lower().startswith("mailto:"):
                continue

            full = urljoin(base, href)
            if same_domain_only and not same_site(effective_url, full):
                continue

            path = urlparse(full).path.lower()
            link_text = (a.get_text() or "").lower()

            # Check if link matches target paths with smart prioritization
            path_matches = any(tp in path for tp in TARGET_PATHS)
            text_matches = any(tp in link_text for tp in TARGET_PATHS)

            if path_matches or text_matches:
                # Calculate priority score based on path and text
                path_score = priority_score(path)
                text_score = priority_score(link_text)
                final_score = min(path_score, text_score)
                priority_links.append((final_score, full))

        # Sort by priority (lower score = higher priority) and take top links
        priority_links.sort(key=lambda x: (x[0], x[1]))

        if len(priority_links) < max_children:
            existing = {link for _, link in priority_links}
            for target in TARGET_PATHS:
                path = target if target.startswith("/") else f"/{target}"
                full = urljoin(base, path)
                if full in existing:
                    continue
                if same_domain_only and not same_site(effective_url, full):
                    continue
                priority_links.append((priority_score(path), full))
                existing.add(full)

            priority_links.sort(key=lambda x: (x[0], x[1]))

        children = [link for _, link in priority_links[:max_children]]

        logger.debug(f"{url} → scanning {len(children)} prioritized child page(s)")

        async def fetch_child(u: str) -> Set[str]:
            return await extract_emails(
                browser,
                u,
                timeout=timeout,
                recurse=False,
                js_enabled=js_enabled,
                block_assets=block_assets,
                allow_third_party=allow_third_party,
                check_facebook=False,  # Don't check Facebook recursively
                same_domain_only=same_domain_only,
            )

        results = await asyncio.gather(*[fetch_child(u) for u in children], return_exceptions=True)
        for r in results:
            if isinstance(r, set):
                emails.update(r)

        # Check Facebook pages if enabled (only on main page, not recursively)
        if check_facebook:
            facebook_links = find_facebook_page_links(soup, effective_url)
            if facebook_links:
                logger.info(f"🔍 FACEBOOK DETECTION: Found {len(facebook_links)} Facebook page(s): {facebook_links[:3]}...") if len(facebook_links) > 3 else logger.info(f"🔍 FACEBOOK DETECTION: Found Facebook page(s): {facebook_links}")
                for fb_url in facebook_links[:2]:  # Only check first 2 Facebook links
                    try:
                        logger.info(f"📘 FACEBOOK SCRAPING: Attempting to scrape {fb_url}...")
                        fb_emails = await extract_emails_from_facebook_with_engine(
                            browser=browser,
                            http_client=http_client,
                            facebook_url=fb_url,
                            timeout=timeout,
                            facebook_engine=facebook_engine,
                            facebook_proxy_url=facebook_proxy_url,
                        )
                        if fb_emails:
                            logger.info(f"✅ FACEBOOK SUCCESS: Found {len(fb_emails)} email(s) on Facebook page {fb_url}: {list(fb_emails)}")
                            emails.update(fb_emails)
                        else:
                            logger.warning(f"❌ FACEBOOK EMPTY: No emails found on Facebook page: {fb_url}")
                    except Exception as e:
                        logger.warning(f"💥 FACEBOOK ERROR: Facebook scraping failed for {fb_url}: {e}")
            else:
                logger.info(f"🚫 FACEBOOK DETECTION: No Facebook page links found on {url}")

    # Shared quality rules for both Playwright and Scrapy engines.
    all_emails = filter_valid_emails(emails)
    domain_emails = {e for e in all_emails if is_same_business_domain(e, effective_url)}
    result_emails = domain_emails if domain_emails else all_emails

    logger.debug(f"Email extraction for {url}: found {len(emails)} raw, {len(all_emails)} valid, {len(domain_emails)} domain-specific, returning {len(result_emails)}")

    return result_emails


def _normalize_email_policy(value: str, default: str = "any_valid") -> str:
    policy = str(value or "").strip().lower()
    if policy in {"business_only", "business_or_public", "any_valid"}:
        return policy
    return default


def _policy_flags(policy: str) -> Tuple[bool, bool]:
    normalized = _normalize_email_policy(policy)
    if normalized == "business_only":
        return False, False
    if normalized == "business_or_public":
        return True, False
    return True, True


def pick_best_facebook_email(candidates: Set[str], domain: str, email_policy: str = "any_valid") -> Optional[str]:
    allow_public, allow_other_domains = _policy_flags(email_policy)
    selected = pick_best_business_email(
        candidates,
        domain,
        allow_public=allow_public,
        allow_other_domains=allow_other_domains,
    )
    if selected:
        logger.info(f"🎯 FACEBOOK SELECTION: selected {selected} for domain={domain}")
    return selected


def pick_best_email(domain: str, candidates: Set[str], email_policy: str = "any_valid") -> Optional[str]:
    allow_public, allow_other_domains = _policy_flags(email_policy)
    return pick_best_business_email(
        candidates,
        domain,
        allow_public=allow_public,
        allow_other_domains=allow_other_domains,
    )


# -----------------------------
# HTTP client (requests → urllib) with retries & optional insecure mode
# -----------------------------
class HttpClient:
    def __init__(self, timeout: float = 20.0, max_retries: int = 5, backoff: float = 2.0, insecure: bool = False):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.insecure = insecure
        if insecure:
            self.ssl_ctx = ssl._create_unverified_context()
        else:
            self.ssl_ctx = ssl.create_default_context()

    def _sleep(self, attempt: int):
        delay = self.backoff * (1 + attempt)
        delay *= (0.75 + random.random() * 0.5)
        time.sleep(delay)

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None, proxy_url: str = "") -> str:
        headers = headers or {"Accept": "application/json"}
        normalized_proxy = normalize_proxy_url(proxy_url)
        proxies = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else None
        for attempt in range(self.max_retries):
            try:
                if requests is not None:
                    r = requests.get(url, headers=headers, timeout=self.timeout, allow_redirects=True,
                                     verify=(False if self.insecure else True), proxies=proxies)
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                    return r.text or ""
                else:
                    req = Request(url, headers=headers)
                    with urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                        return resp.read().decode("utf-8", errors="ignore")
            except Exception as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"GET failed for {url}: {e}")
                    return ""
                logger.warning(f"GET retry {attempt + 1}/{self.max_retries} for {url}: {e}")
                self._sleep(attempt)
        return ""

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict:
        text = self.get_text(url, headers=headers)
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception as e:
            logger.error(f"JSON parse error for {url}: {e}; raw={text[:300]}")
            return {}

    def post_json(self, url: str, payload: Dict, headers: Optional[Dict[str, str]] = None) -> Dict:
        headers = headers or {"Accept": "application/json", "Content-Type": "application/json"}
        for attempt in range(self.max_retries):
            try:
                if requests is not None:
                    r = requests.post(url, json=payload, headers=headers, timeout=self.timeout, allow_redirects=True,
                                      verify=(False if self.insecure else True))
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                    return r.json() if r.text else {"status": "ok"}
                else:
                    body = json.dumps(payload).encode("utf-8")
                    req = Request(url, data=body, headers=headers, method="POST")
                    with urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                        txt = resp.read().decode("utf-8", errors="ignore")
                        return json.loads(txt) if txt else {"status": "ok"}
            except Exception as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"POST failed for {url}: {e}")
                    return {"error": str(e)}
                logger.warning(f"POST retry {attempt + 1}/{self.max_retries} for {url}: {e}")
                self._sleep(attempt)
        return {"error": "unknown"}


def _save_ok(resp: Dict) -> bool:
    return isinstance(resp, dict) and not resp.get("error")


def _save_email_immediate(
    http: HttpClient,
    base_url: str,
    campaign_id: int,
    contact_id: int,
    email: str,
    retries: int = 3,
) -> Tuple[bool, Dict]:
    url = f"{base_url}/api/campaign/{campaign_id}/email_update"
    payload_single = {"id": str(contact_id), "email": email}
    payload_batch = {"contacts": [{"id": str(contact_id), "email": email}]}
    last_resp: Dict = {"error": "unknown"}

    for attempt in range(1, max(1, retries) + 1):
        resp = http.post_json(url, payload_single)
        if _save_ok(resp):
            return True, resp
        last_resp = resp if isinstance(resp, dict) else {"error": str(resp)}

        # Some deployments only accept batch-shaped payloads; try both immediately.
        resp2 = http.post_json(url, payload_batch)
        if _save_ok(resp2):
            return True, resp2
        last_resp = resp2 if isinstance(resp2, dict) else {"error": str(resp2)}

        if attempt < retries:
            time.sleep(0.6 * attempt)

    return False, last_resp


def _save_email_batch(
    http: HttpClient,
    base_url: str,
    campaign_id: int,
    updates: List[Dict[str, str]],
    retries: int = 3,
) -> Tuple[bool, Dict]:
    url = f"{base_url}/api/campaign/{campaign_id}/email_update"
    payload = {"contacts": updates}
    last_resp: Dict = {"error": "unknown"}
    for attempt in range(1, max(1, retries) + 1):
        resp = http.post_json(url, payload)
        if _save_ok(resp):
            return True, resp
        last_resp = resp if isinstance(resp, dict) else {"error": str(resp)}
        if attempt < retries:
            time.sleep(0.8 * attempt)
    return False, last_resp


# -----------------------------
# Main scraping flow (immediate-save)
# -----------------------------
async def scrape_and_update_immediate(
    campaign: str,
    batch: int,
    max_batches: int,
    max_batches_facebook: int,
    base_url: str,
    concurrency: int,
    timeout: float,
    links: int,
    min_domain_letters: int,
    domain_timeout: float,
    kill_browser_on_timeout: bool,
    show_browser: bool = False,
    facebook_engine: str = "camoufox",
    facebook_proxy_url: str = "",
    same_domain_only: bool = True,
    facebook: bool = False,
    email_policy: str = "any_valid",
) -> None:
    set_min_domain_letters(min_domain_letters)
    email_policy = _normalize_email_policy(email_policy, default="any_valid")
    for attempt_insecure in (False, True):
        http = HttpClient(timeout=20.0, max_retries=5, backoff=2.0, insecure=attempt_insecure)
        try:
            # Resolve campaign id
            if str(campaign).strip().isdigit():
                campaign_id = int(str(campaign).strip())
                logger.info(f"Using campaign id={campaign_id}")
            else:
                data = http.get_json(f"{base_url}/api/campaigns/active")
                campaign_id = None
                for c in data.get("campaigns", []) if isinstance(data, dict) else []:
                    if str(c.get("name", "")).strip().lower() == str(campaign).strip().lower():
                        campaign_id = int(c.get("id"))
                        break
                if campaign_id is None:
                    names = ", ".join([str(c.get("name")) for c in data.get("campaigns", [])]) if isinstance(data, dict) else ""
                    raise RuntimeError(f"Campaign '{campaign}' not found. Available: {names}")
                logger.info(f"Matched campaign '{campaign}' -> id={campaign_id}")

            async def run_batches(
                use_facebook: bool,
                facebook_only: bool,
                max_batches_limit: int,
                phase_concurrency: int,
                phase_label: str,
            ) -> str:
                batch_count = 0
                pull_count = 0
                while True:
                    if max_batches_limit and pull_count >= max_batches_limit:
                        logger.info(
                            "Reached max %s pulls (%s); stopping %s phase.",
                            phase_label,
                            max_batches_limit,
                            phase_label,
                        )
                        return "done"

                    pull_count += 1
                    data = http.get_json(f"{base_url}/api/campaign/{campaign_id}/nomail?batch={batch}")
                    contacts = data.get("contacts", []) if isinstance(data, dict) else []
                    if not contacts:
                        if batch_count == 0 and not attempt_insecure:
                            logger.warning("No contacts returned; retrying with --insecure mode due to possible TLS issue...")
                            return "retry_insecure"
                        if max_batches_limit and pull_count < max_batches_limit:
                            logger.info(
                                "Pull %s/%s for %s phase returned 0 contacts; continuing.",
                                pull_count,
                                max_batches_limit,
                                phase_label,
                            )
                            continue
                        logger.info(f"No contacts returned for {phase_label} phase.")
                        return "done"

                    batch_count += 1
                    logger.info(f"Pulled {len(contacts)} contact(s) needing emails [batch {batch_count} / {phase_label}].")

                    def _resolve_domain(contact: Dict) -> str:
                        raw = (
                            contact.get("domain")
                            or contact.get("website")
                            or contact.get("url")
                            or ""
                        )
                        raw = str(raw).strip()
                        if not raw:
                            return ""
                        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
                        return parsed.netloc or strip_url_prefix(raw)

                    usable_contacts: List[Dict] = []
                    for c in contacts:
                        d = _resolve_domain(c)
                        if d:
                            c["_resolved_domain"] = d
                            usable_contacts.append(c)

                    if not usable_contacts:
                        logger.info("No usable domains found in this batch; skipping.")
                        continue
                    contacts = usable_contacts
                    logger.info(f"Usable domains in batch: {len(contacts)}")

                    results: List[Any] = []
                    runtime = AsyncBrowserRuntime(
                        headless=(not bool(show_browser)),
                        camoufox_options={"block_images": False},
                        proxy_url=(facebook_proxy_url if facebook else ""),
                    )
                    browser = await runtime.launch()
                    logger.info("Email browser backend: %s", backend_display_name(None))
                    try:
                        browser_lock = asyncio.Lock()
                        sem = asyncio.Semaphore(phase_concurrency)
                        allow_restart = kill_browser_on_timeout and phase_concurrency <= 1
                        restart_warned = False

                        async def restart_browser():
                            nonlocal browser
                            async with browser_lock:
                                browser = await runtime.restart()
                                logger.warning("Browser restarted after timeout.")

                        async def maybe_restart_browser():
                            nonlocal restart_warned
                            if not allow_restart:
                                if not restart_warned:
                                    logger.warning("Skipping browser restart with concurrency>1 to avoid aborting active tasks.")
                                    restart_warned = True
                                return
                            await restart_browser()

                        async def try_extract(domain: str, https: bool, js_enabled: bool, check_facebook: bool = False) -> Set[str]:
                            target = domain if https else "http://" + strip_url_prefix(domain)
                            scheme = "https" if https else "http"
                            js_flag = "JSon" if js_enabled else "JSoff"
                            print(f"[START] {target} [{scheme},{js_flag}], links≤{links}", flush=True)
                            per_page_timeout = max(timeout, 12.0) if js_enabled else timeout
                            return await extract_emails(
                                browser, target,
                                timeout=per_page_timeout, recurse=True, max_children=links,
                                js_enabled=js_enabled,
                                block_assets=True,
                                allow_third_party=js_enabled,
                                check_facebook=check_facebook,
                                http_client=http,
                                facebook_engine=facebook_engine,
                                facebook_proxy_url=facebook_proxy_url,
                                same_domain_only=same_domain_only,
                            )

                        async def scrape_one(seq: int, contact: Dict) -> Optional[Dict]:
                            async with sem:
                                cid = contact.get("id")
                                domain = (contact.get("_resolved_domain") or "").strip()
                                if not domain:
                                    raw_domain = (
                                        contact.get("domain")
                                        or contact.get("website")
                                        or contact.get("url")
                                        or ""
                                    )
                                    raw_domain = str(raw_domain).strip()
                                    if raw_domain:
                                        parsed = urlparse(raw_domain if "://" in raw_domain else f"https://{raw_domain}")
                                        domain = parsed.netloc or strip_url_prefix(raw_domain)
                                if not domain:
                                    logger.debug(f"Skipping contact {cid}: no domain/website/url.")
                                    return None

                                print(f"[WILL SEARCH] ({seq}/{len(contacts)}): {domain}", flush=True)
                                start_ts = time.monotonic()
                                best = None

                                if use_facebook:
                                    try:
                                        print(f"[FACEBOOK] ({seq}/{len(contacts)}): {domain} - checking Facebook pages first", flush=True)
                                        facebook_only_candidates = set()
                                        _ = await asyncio.wait_for(
                                            try_extract(domain, True, js_enabled=False, check_facebook=False),
                                            timeout=domain_timeout,
                                        )

                                        target = domain if True else "http://" + strip_url_prefix(domain)
                                        context = await browser.new_context(
                                            java_script_enabled=False,
                                            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                        "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"),
                                        )
                                        await install_async_blocked_resource_routes(context)
                                        context.set_default_timeout(int(timeout * 1000))
                                        context.set_default_navigation_timeout(int(timeout * 1000))
                                        page = await context.new_page()

                                        try:
                                            resp = await page.goto(f"https://{target}", wait_until="domcontentloaded", timeout=int(timeout * 1000))
                                            if resp and resp.status == 200:
                                                html = await page.content()
                                                soup = BeautifulSoup(html, "html.parser")
                                                facebook_links = find_facebook_page_links(soup, f"https://{target}")

                                                if facebook_links:
                                                    logger.info(f"🔍 FACEBOOK DETECTION: Found Facebook page(s): {facebook_links}")
                                                    for fb_url in facebook_links[:2]:
                                                        try:
                                                            logger.info(f"📘 FACEBOOK SCRAPING: Attempting to scrape {fb_url}...")
                                                            fb_emails = await extract_emails_from_facebook_with_engine(
                                                                browser=browser,
                                                                http_client=http,
                                                                facebook_url=fb_url,
                                                                timeout=timeout,
                                                                facebook_engine=facebook_engine,
                                                                facebook_proxy_url=facebook_proxy_url,
                                                            )
                                                            if fb_emails:
                                                                logger.info(f"✅ FACEBOOK SUCCESS: Found {len(fb_emails)} email(s) on Facebook page {fb_url}: {list(fb_emails)}")
                                                                facebook_only_candidates.update(fb_emails)
                                                            else:
                                                                logger.warning(f"❌ FACEBOOK EMPTY: No emails found on Facebook page: {fb_url}")
                                                        except Exception as e:
                                                            logger.warning(f"💥 FACEBOOK ERROR: Facebook scraping failed for {fb_url}: {e}")
                                                else:
                                                    logger.info(f"🚫 FACEBOOK DETECTION: No Facebook page links found on {target}")
                                        finally:
                                            try:
                                                await context.close()
                                            except Exception:
                                                pass

                                        if facebook_only_candidates:
                                            best = pick_best_facebook_email(
                                                facebook_only_candidates,
                                                domain,
                                                email_policy=email_policy,
                                            )
                                            if best:
                                                elapsed = time.monotonic() - start_ts
                                                print(f"[FACEBOOK SUCCESS] ({seq}/{len(contacts)}): {domain} -> {best} (from Facebook)", flush=True)
                                                ok_save, resp = _save_email_immediate(
                                                    http=http,
                                                    base_url=base_url,
                                                    campaign_id=campaign_id,
                                                    contact_id=cid,
                                                    email=best,
                                                    retries=3,
                                                )
                                                if ok_save:
                                                    logger.info(f"✓ DONE ({seq}/{len(contacts)}): {domain} -> {best} [FB priority] [{elapsed:.1f}s]")
                                                    return None
                                                logger.warning(f"SAVE DEFERRED ({seq}/{len(contacts)}): {domain} -> {best} [FB]; {resp}")
                                                return {"id": cid, "email": best}
                                    except asyncio.TimeoutError:
                                        print(f"[FB TIMEOUT] ({seq}/{len(contacts)}): {domain}", flush=True)
                                        if kill_browser_on_timeout:
                                            await restart_browser()
                                    except Exception as e:
                                        logger.debug(f"Facebook priority check failed for {domain}: {e}")

                                if not best and facebook_only:
                                    elapsed = time.monotonic() - start_ts
                                    logger.info(f"∅ DONE ({seq}/{len(contacts)}): {domain} (no email) [{elapsed:.1f}s]")
                                    return None

                                if not best and not facebook_only:
                                    print(f"[WEBSITE FALLBACK] ({seq}/{len(contacts)}): {domain} - checking website", flush=True)
                                    candidates: Set[str] = set()

                                    try:
                                        found = await asyncio.wait_for(
                                            try_extract(domain, True, js_enabled=False, check_facebook=False),
                                            timeout=domain_timeout,
                                        )
                                        candidates.update(found)
                                        if not candidates:
                                            print(f"[RETRY] ({seq}/{len(contacts)}): http://{domain} [http,JSoff]", flush=True)
                                            found2 = await asyncio.wait_for(
                                                try_extract(domain, False, js_enabled=False, check_facebook=False),
                                                timeout=domain_timeout,
                                            )
                                            candidates.update(found2)
                                    except asyncio.TimeoutError:
                                        print(f"[TIMEOUT] ({seq}/{len(contacts)}): {domain} (JSoff)", flush=True)
                                        if kill_browser_on_timeout:
                                            await maybe_restart_browser()
                                        return None
                                    except Exception as e:
                                        logger.debug(f"FAST PASS error for {domain}: {e}")

                                    if not candidates:
                                        try:
                                            found = await asyncio.wait_for(
                                                try_extract(domain, True, js_enabled=True, check_facebook=False),
                                                timeout=domain_timeout,
                                            )
                                            candidates.update(found)
                                            if not candidates:
                                                print(f"[RETRY] ({seq}/{len(contacts)}): http://{domain} [http,JSon]", flush=True)
                                                found2 = await asyncio.wait_for(
                                                    try_extract(domain, False, js_enabled=True, check_facebook=False),
                                                    timeout=domain_timeout,
                                                )
                                                candidates.update(found2)
                                        except asyncio.TimeoutError:
                                            print(f"[TIMEOUT] ({seq}/{len(contacts)}): {domain} (JSon)", flush=True)
                                            if kill_browser_on_timeout:
                                                await maybe_restart_browser()
                                            return None
                                        except Exception as e:
                                            logger.debug(f"FULL PASS error for {domain}: {e}")

                                    best = pick_best_email(domain, candidates, email_policy=email_policy)

                                elapsed = time.monotonic() - start_ts
                                if not best:
                                    logger.info(f"∅ DONE ({seq}/{len(contacts)}): {domain} (no email) [{elapsed:.1f}s]")
                                    return None

                                ok_save, resp = _save_email_immediate(
                                    http=http,
                                    base_url=base_url,
                                    campaign_id=campaign_id,
                                    contact_id=cid,
                                    email=best,
                                    retries=3,
                                )
                                if ok_save:
                                    logger.info(f"✓ DONE ({seq}/{len(contacts)}): {domain} -> {best} [{elapsed:.1f}s]")
                                    return None
                                logger.warning(f"SAVE DEFERRED ({seq}/{len(contacts)}): {domain} -> {best}; {resp}")
                                return {"id": cid, "email": best}

                        results = await asyncio.gather(
                            *[scrape_one(i, c) for i, c in enumerate(contacts, 1)],
                            return_exceptions=True,
                        )
                    finally:
                        await runtime.close()

                    unsaved = [r for r in results if isinstance(r, dict) and r.get("email")]
                    if unsaved:
                        updates = [{"id": str(u["id"]), "email": str(u["email"])} for u in unsaved]
                        ok_batch, resp = _save_email_batch(
                            http=http,
                            base_url=base_url,
                            campaign_id=campaign_id,
                            updates=updates,
                            retries=3,
                        )
                        if ok_batch:
                            logger.info(f"BATCH SAVE: {len(unsaved)} email(s) → {resp}")
                        else:
                            # Final fallback: try each deferred item immediately again.
                            recovered = 0
                            for u in updates:
                                ok_single, _ = _save_email_immediate(
                                    http=http,
                                    base_url=base_url,
                                    campaign_id=campaign_id,
                                    contact_id=int(str(u.get("id", "0")) or 0),
                                    email=str(u.get("email", "")),
                                    retries=2,
                                )
                                if ok_single:
                                    recovered += 1
                            logger.warning(
                                "BATCH SAVE DEFERRED: %d/%d recovered by final single-save fallback; last_resp=%s",
                                recovered,
                                len(updates),
                                resp,
                            )

            effective_regular_max_batches = int(max_batches or 0)
            if (
                facebook
                and int(max_batches_facebook or 0) > 0
                and effective_regular_max_batches <= 0
            ):
                effective_regular_max_batches = int(max_batches_facebook)
                logger.info(
                    "Facebook phase is enabled (max_batches_facebook=%s). "
                    "Regular phase max batches was unlimited, so it is auto-capped to %s "
                    "to ensure Facebook phase starts.",
                    max_batches_facebook,
                    effective_regular_max_batches,
                )

            status = await run_batches(
                use_facebook=False,
                facebook_only=False,
                max_batches_limit=effective_regular_max_batches,
                phase_concurrency=concurrency,
                phase_label="regular",
            )
            if status == "retry_insecure":
                continue

            if facebook and max_batches_facebook and max_batches_facebook > 0:
                logger.info("Starting Facebook-only phase with concurrency=1.")
                await run_batches(
                    use_facebook=True,
                    facebook_only=True,
                    max_batches_limit=max_batches_facebook,
                    phase_concurrency=1,
                    phase_label="facebook",
                )

            break  # success with this insecure level
        except Exception as e:
            if attempt_insecure:
                raise RuntimeError(f"Failed even with insecure TLS: {e}")
            else:
                logger.warning(f"Secure failed: {e}; retrying with --insecure TLS")
                continue


def main():
    p = argparse.ArgumentParser(description="Enhanced Email Scraper with Smart Filtering")
    p.add_argument("--campaign", required=True, help="Campaign ID (e.g., 95) or NAME (case-insensitive)")
    p.add_argument("--batch", type=int, default=10, help="How many contacts to pull from /nomail")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Campaign API base URL")
    p.add_argument("--concurrency", type=int, default=3, help="How many parallel browser contexts")
    p.add_argument("--timeout", type=float, default=8.0, help="Per-page timeout (seconds)")
    p.add_argument("--links", type=int, default=5, help="Max child pages to visit per domain")
    p.add_argument("--min-domain-letters", type=int, default=2, help="Minimum letters in email root domain")
    p.add_argument("--domain-timeout", type=float, default=60.0, help="Total timeout per domain")
    p.add_argument("--max-batches", type=int, default=0, help="Max batches per run (0 = unlimited)")
    p.add_argument("--max-batches-facebook", type=int, default=0, help="Max Facebook batches per run (0 = disabled)")
    p.add_argument("--no-kill-on-timeout", action="store_true", help="Don't restart browser on timeout")
    p.add_argument("--show-browser", dest="show_browser", action="store_true", help="Show browser window while scraping emails")
    p.add_argument("--hide-browser", dest="show_browser", action="store_false", help="Run email scraping headless")
    p.set_defaults(show_browser=False)
    p.add_argument("--facebook", action="store_true", help="Check Facebook pages linked from website for additional emails")
    p.add_argument("--facebook-engine", default="camoufox", choices=["camoufox", "playwright", "scrapy"], help="Engine for Facebook fallback")
    p.add_argument("--facebook-proxy", default="", help="Optional rotating proxy URL for Facebook fallback (user:pass@host:port)")
    p.add_argument("--same-domain-only", action="store_true", help="Only follow links within the company domain")
    p.add_argument(
        "--email-policy",
        default="any_valid",
        choices=["business_only", "business_or_public", "any_valid"],
        help="How to choose final email: business_only, business_or_public, or any_valid (legacy).",
    )
    args = p.parse_args()

    logger.info(f"Enhanced Email Scraper starting with smart filtering...")
    logger.info(f"Target paths: {len(TARGET_PATHS)} paths with priority scoring")
    logger.info(f"Public providers: {len(PUBLIC_PROVIDERS)} recognized providers")
    logger.info("Email browser visibility: %s", "headed" if args.show_browser else "headless")
    if args.facebook:
        logger.info(f"Facebook page scraping: ENABLED (engine={args.facebook_engine})")
        if str(args.facebook_proxy or "").strip():
            logger.info("Facebook proxy: ENABLED")
    if args.same_domain_only:
        logger.info("Same-domain-only crawling: ENABLED")
    logger.info("Email selection policy: %s", args.email_policy)

    asyncio.run(scrape_and_update_immediate(
        campaign=args.campaign,
        batch=args.batch,
        base_url=args.base_url,
        concurrency=args.concurrency,
        timeout=args.timeout,
        links=args.links,
        min_domain_letters=args.min_domain_letters,
        domain_timeout=args.domain_timeout,
        max_batches=args.max_batches,
        max_batches_facebook=args.max_batches_facebook,
        kill_browser_on_timeout=not args.no_kill_on_timeout,
        show_browser=bool(args.show_browser),
        facebook_engine=args.facebook_engine,
        facebook_proxy_url=str(args.facebook_proxy or ""),
        same_domain_only=args.same_domain_only,
        facebook=args.facebook,
        email_policy=args.email_policy,
    ))


if __name__ == "__main__":
    main()
