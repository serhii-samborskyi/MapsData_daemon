#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

LOCAL_DEPS = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)

try:
    import scrapy
    from scrapy.crawler import CrawlerProcess
    from scrapy.exceptions import DontCloseSpider
    from scrapy import signals
except Exception:
    print("Scrapy is required. Install with: pip install scrapy")
    raise

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None  # type: ignore

import ssl
from urllib.request import Request, urlopen

from email_quality import (
    extract_candidate_emails_from_text,
    normalize_domain,
    pick_best_business_email,
    registrable_domain,
    set_min_domain_letters,
)

DEFAULT_BASE_URL = "https://scrapiq.leadtechx.com"

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", force=True)
logger = logging.getLogger("email_scraper_scrapy")


class HttpClient:
    def __init__(self, timeout: float = 20.0, max_retries: int = 3) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.insecure = False

    def _sleep(self, attempt: int) -> None:
        time.sleep(0.5 * (attempt + 1))

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        req_headers = headers or {"Accept": "text/html,application/json"}
        for attempt in range(self.max_retries):
            try:
                if requests is not None:
                    resp = requests.get(
                        url,
                        headers=req_headers,
                        timeout=self.timeout,
                        allow_redirects=True,
                        verify=(not self.insecure),
                    )
                    resp.raise_for_status()
                    return resp.text or ""
                req = Request(url, headers=req_headers)
                ctx = ssl._create_unverified_context() if self.insecure else None
                with urlopen(req, timeout=self.timeout, context=ctx) as resp:
                    return resp.read().decode("utf-8", "ignore")
            except Exception as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) and not self.insecure:
                    self.insecure = True
                    logger.warning("SSL verification failed; retrying GET with insecure HTTPS.")
                    continue
                if attempt == self.max_retries - 1:
                    logger.warning("GET failed for %s: %s", url, exc)
                    return ""
                self._sleep(attempt)
        return ""

    def get_json(self, url: str) -> Dict:
        text = self.get_text(url, headers={"Accept": "application/json"})
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception as exc:
            logger.warning("JSON parse failed for %s: %s", url, exc)
            return {}

    def post_json(self, url: str, payload: Dict) -> Dict:
        for attempt in range(self.max_retries):
            try:
                if requests is not None:
                    resp = requests.post(
                        url,
                        json=payload,
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                        timeout=self.timeout,
                        allow_redirects=True,
                        verify=(not self.insecure),
                    )
                    resp.raise_for_status()
                    return resp.json() if resp.text else {"status": "ok"}
                body = json.dumps(payload).encode("utf-8")
                req = Request(url, data=body, headers={"Accept": "application/json", "Content-Type": "application/json"})
                ctx = ssl._create_unverified_context() if self.insecure else None
                with urlopen(req, timeout=self.timeout, context=ctx) as resp:
                    text = resp.read().decode("utf-8", "ignore")
                return json.loads(text) if text else {"status": "ok"}
            except Exception as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) and not self.insecure:
                    self.insecure = True
                    logger.warning("SSL verification failed; retrying POST with insecure HTTPS.")
                    continue
                if attempt == self.max_retries - 1:
                    logger.warning("POST failed for %s: %s", url, exc)
                    return {"error": str(exc)}
                self._sleep(attempt)
        return {"error": "unknown"}


def strip_url_prefix(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("http://"):
        v = v[7:]
    elif v.startswith("https://"):
        v = v[8:]
    return v.lstrip("/")


def normalize_base_url(base_url: str) -> str:
    base = (base_url or "").strip()
    if base.endswith("/api"):
        base = base[:-4]
    return base.rstrip("/")


def extract_emails_from_text(text: str) -> Set[str]:
    return extract_candidate_emails_from_text(text)


def pick_best_email(domain: str, emails: Iterable[str]) -> str:
    return pick_best_business_email(emails, domain, allow_public=True) or ""


def priority_score(url_or_text: str) -> int:
    s = (url_or_text or "").lower()
    for key, score in CONTACT_PRIORITY:
        if key in s:
            return score
    return 99


def same_site(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ha = normalize_domain(a)
    hb = normalize_domain(b)
    if not ha or not hb:
        return False
    return ha == hb or registrable_domain(ha) == registrable_domain(hb)


def resolve_campaign_id(http: HttpClient, base_url: str, campaign: str) -> int:
    campaign = str(campaign).strip()
    if campaign.isdigit():
        return int(campaign)
    data = http.get_json(f"{base_url}/api/campaigns/active")
    for c in data.get("campaigns", []) if isinstance(data, dict) else []:
        if str(c.get("name", "")).strip().lower() == campaign.lower():
            return int(c.get("id"))
    names = ", ".join([str(c.get("name")) for c in data.get("campaigns", [])]) if isinstance(data, dict) else ""
    raise RuntimeError(f"Campaign '{campaign}' not found. Available: {names}")


class EmailSpider(scrapy.Spider):
    name = "email_scraper_scrapy"

    def __init__(
        self,
        base_url: str,
        campaign_id: int,
        batch: int,
        max_batches: int,
        links: int,
        domain_timeout: float,
        http_timeout: float,
        http_max_retries: int,
        facebook: bool,
        max_batches_facebook: int,
        same_domain_only: bool,
    ) -> None:
        super().__init__()
        self.base_url = normalize_base_url(base_url)
        self.campaign_id = campaign_id
        self.batch = int(batch)
        self.max_batches = int(max_batches or 0)
        self.links = int(links)
        self.domain_timeout = float(domain_timeout)
        self.http = HttpClient(timeout=http_timeout, max_retries=http_max_retries)
        self.facebook = bool(facebook)
        self.max_batches_facebook = int(max_batches_facebook or 0)
        self.same_domain_only = bool(same_domain_only)
        self.batch_count = 0
        self.pull_attempt_count = 0
        self._no_more_batches = False
        self.contact_states: Dict[int, Dict] = {}
        self._saved_contacts: Set[int] = set()
        self._pw_manager = None
        self._pw_browser = None
        self._playwright_failed = False

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_idle, signal=signals.spider_idle)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        return spider

    def spider_closed(self, spider, reason) -> None:
        self._close_playwright_browser()

    def start_requests(self):
        for req in self._prepare_batch_requests():
            yield req

    def spider_idle(self):
        if self._no_more_batches:
            return
        requests = self._prepare_batch_requests()
        if not requests:
            if self.max_batches and self.pull_attempt_count < self.max_batches:
                raise DontCloseSpider
            self._no_more_batches = True
            return
        for req in requests:
            self.crawler.engine.crawl(req, spider=self)
        raise DontCloseSpider

    def _prepare_batch_requests(self) -> List[scrapy.Request]:
        if self.max_batches and self.pull_attempt_count >= self.max_batches:
            logger.info("Reached max pulls (%s); stopping.", self.max_batches)
            return []
        self.pull_attempt_count += 1

        data = self.http.get_json(f"{self.base_url}/api/campaign/{self.campaign_id}/nomail?batch={self.batch}")
        contacts = data.get("contacts", []) if isinstance(data, dict) else []
        if not contacts:
            if self.max_batches and self.pull_attempt_count < self.max_batches:
                logger.info(
                    "Pull %s/%s returned 0 contacts; continuing.",
                    self.pull_attempt_count,
                    self.max_batches,
                )
            else:
                logger.info("No contacts returned; stopping.")
            return []
        self.batch_count += 1
        facebook_for_batch = (
            self.facebook
            and not self.same_domain_only
            and self.max_batches_facebook > 0
            and self.batch_count <= self.max_batches_facebook
        )
        logger.info("Pulled %s contact(s) needing emails [batch %s].", len(contacts), self.batch_count)
        if facebook_for_batch:
            logger.info("Facebook fallback enabled for batch %s.", self.batch_count)

        requests: List[scrapy.Request] = []
        for contact in contacts:
            contact_id = contact.get("id")
            if contact_id is None:
                continue
            domain = self._resolve_domain(contact)
            if not domain:
                continue
            state = {
                "domain": domain,
                "start_time": time.monotonic(),
                "pages_seen": 0,
                "pages_queued": 0,
                "done": False,
                "seen_urls": set(),
                "facebook_links": set(),
                "facebook_enabled": facebook_for_batch,
            }
            self.contact_states[int(contact_id)] = state
            start_url = self._normalize_start_url(domain)
            state["pages_queued"] = 1
            state["seen_urls"].add(start_url)
            req = scrapy.Request(
                start_url,
                callback=self.parse,
                errback=self._errback,
                dont_filter=True,
                meta={
                    "contact_id": int(contact_id),
                    "domain": domain,
                    "http_fallback": False,
                },
            )
            requests.append(req)
        return requests

    def _resolve_domain(self, contact: Dict) -> str:
        raw = contact.get("domain") or contact.get("website") or contact.get("url") or ""
        raw = str(raw).strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        host = parsed.netloc or strip_url_prefix(raw)
        return host.strip().lower()

    @staticmethod
    def _normalize_start_url(domain: str) -> str:
        domain = strip_url_prefix(domain)
        return f"https://{domain}" if domain else ""

    def _errback(self, failure):
        request = failure.request
        contact_id = request.meta.get("contact_id")
        domain = request.meta.get("domain", "")
        if contact_id is None:
            return
        state = self.contact_states.get(int(contact_id))
        if not state or state.get("done"):
            return
        state["pages_seen"] += 1
        if self._domain_timed_out(state):
            state["done"] = True
            return
        if not request.meta.get("http_fallback") and request.url.startswith("https://"):
            http_url = request.url.replace("https://", "http://", 1)
            if http_url not in state["seen_urls"]:
                state["seen_urls"].add(http_url)
                state["pages_queued"] += 1
                yield scrapy.Request(
                    http_url,
                    callback=self.parse,
                    errback=self._errback,
                    dont_filter=True,
                    meta={
                        "contact_id": int(contact_id),
                        "domain": domain,
                        "http_fallback": True,
                    },
                )
        self._maybe_finalize_contact(int(contact_id), state)

    def parse(self, response: scrapy.http.Response):
        contact_id = response.meta.get("contact_id")
        domain = response.meta.get("domain")
        if contact_id is None or not domain:
            return
        state = self.contact_states.get(int(contact_id))
        if not state or state.get("done"):
            return

        state["pages_seen"] += 1
        if self._domain_timed_out(state):
            state["done"] = True
            return

        if state.get("facebook_enabled"):
            self._collect_facebook_links(response, state)

        emails = extract_emails_from_text(response.text or "")
        best = pick_best_email(domain, emails)
        if best:
            if self._save_email(contact_id, best):
                logger.info("FOUND %s -> %s", domain, best)
            state["done"] = True
            return

        max_pages = max(1, self.links + 1)
        if state["pages_seen"] >= max_pages:
            self._maybe_finalize_contact(int(contact_id), state)
            return

        links = self._extract_links(response, domain)
        if not links:
            self._maybe_finalize_contact(int(contact_id), state)
            return

        for link in links:
            if state["pages_queued"] >= max_pages:
                break
            if link in state["seen_urls"]:
                continue
            state["seen_urls"].add(link)
            state["pages_queued"] += 1
            yield scrapy.Request(
                link,
                callback=self.parse,
                errback=self._errback,
                dont_filter=True,
                meta={
                    "contact_id": int(contact_id),
                    "domain": domain,
                    "http_fallback": True,
                },
            )
        self._maybe_finalize_contact(int(contact_id), state)

    def _extract_links(self, response: scrapy.http.Response, domain: str) -> List[str]:
        links: List[str] = []
        for href in response.css("a::attr(href)").getall():
            if not href:
                continue
            href = href.strip()
            if href.startswith("mailto:") or href.startswith("javascript:") or href.startswith("tel:"):
                continue
            absolute = response.urljoin(href)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            if self.same_domain_only and not same_site(parsed.netloc, domain):
                continue
            links.append(absolute)
        unique_links = list(dict.fromkeys(links))
        unique_links.sort(key=priority_score)
        return unique_links

    def _domain_timed_out(self, state: Dict) -> bool:
        if not self.domain_timeout:
            return False
        return (time.monotonic() - state.get("start_time", 0.0)) > self.domain_timeout

    @staticmethod
    def _is_valid_facebook_link(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in {"facebook.com", "m.facebook.com", "mbasic.facebook.com"}:
            return False
        path = (parsed.path or "").lower()
        if not path or path in {"/", "/home.php"}:
            return False
        blocked = (
            "/sharer",
            "/share",
            "/dialog",
            "/plugins",
            "/tr",
            "/login",
            "/events",
            "/photo",
            "/watch",
            "/reel",
        )
        return not any(token in path for token in blocked)

    def _collect_facebook_links(self, response: scrapy.http.Response, state: Dict) -> None:
        links: Set[str] = state.get("facebook_links", set())
        for href in response.css("a::attr(href)").getall():
            if not href:
                continue
            absolute = response.urljoin(href.strip())
            if not self._is_valid_facebook_link(absolute):
                continue
            parsed = urlparse(absolute)
            clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            if clean:
                links.add(clean)
        state["facebook_links"] = links

    def _fetch_facebook_emails(self, domain: str, links: Set[str]) -> Optional[str]:
        if not links:
            return None
        candidates: Set[str] = set()
        candidates.update(self._fetch_facebook_emails_playwright(links))

        # Keep HTTP fallback for environments where Playwright fails or is unavailable.
        if not candidates:
            logger.info("Facebook Playwright fallback returned no emails; retrying with HTTP fetch.")
            for fb_url in list(links)[:2]:
                html = self.http.get_text(fb_url)
                if html:
                    candidates.update(extract_emails_from_text(html))
                if not fb_url.endswith("/about"):
                    about_url = fb_url.rstrip("/") + "/about"
                    about_html = self.http.get_text(about_url)
                    if about_html:
                        candidates.update(extract_emails_from_text(about_html))
        return pick_best_email(domain, candidates) if candidates else None

    def _get_playwright_browser(self):
        if self._pw_browser is not None:
            return self._pw_browser
        if self._playwright_failed:
            return None
        if sync_playwright is None:
            logger.warning("Playwright is not available for Scrapy Facebook fallback.")
            self._playwright_failed = True
            return None
        try:
            self._pw_manager = sync_playwright().start()
            self._pw_browser = self._pw_manager.chromium.launch(headless=True)
            return self._pw_browser
        except Exception as exc:
            logger.warning("Failed to start Playwright for Scrapy Facebook fallback: %s", exc)
            self._playwright_failed = True
            self._close_playwright_browser()
            return None

    def _close_playwright_browser(self) -> None:
        if self._pw_browser is not None:
            try:
                self._pw_browser.close()
            except Exception:
                pass
            self._pw_browser = None
        if self._pw_manager is not None:
            try:
                self._pw_manager.stop()
            except Exception:
                pass
            self._pw_manager = None

    def _fetch_facebook_emails_playwright(self, links: Set[str]) -> Set[str]:
        browser = self._get_playwright_browser()
        if browser is None:
            return set()

        candidates: Set[str] = set()
        page_timeout_ms = int(max(8.0, min(self.http.timeout, 30.0)) * 1000)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        for fb_url in list(links)[:2]:
            targets = [fb_url]
            if not fb_url.endswith("/about"):
                targets.append(fb_url.rstrip("/") + "/about")

            for target in targets:
                context = None
                try:
                    context = browser.new_context(java_script_enabled=True, user_agent=user_agent)
                    page = context.new_page()
                    page.goto(target, wait_until="domcontentloaded", timeout=page_timeout_ms)
                    page.wait_for_timeout(1200)
                    html = page.content() or ""
                    if html:
                        candidates.update(extract_emails_from_text(html))
                    try:
                        text = page.evaluate("() => document.body ? document.body.innerText : ''")
                        if text:
                            candidates.update(extract_emails_from_text(text))
                    except Exception:
                        pass
                    try:
                        mailtos = page.eval_on_selector_all(
                            "a[href^='mailto:']",
                            "els => els.map(el => el.getAttribute('href')).filter(Boolean)",
                        )
                        for m in mailtos:
                            candidates.update(extract_emails_from_text(str(m)))
                    except Exception:
                        pass
                except Exception:
                    continue
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
        return candidates

    def _maybe_finalize_contact(self, contact_id: int, state: Dict) -> None:
        if state.get("done"):
            return
        if state.get("pages_seen", 0) < state.get("pages_queued", 0):
            return

        best = None
        if state.get("facebook_enabled"):
            best = self._fetch_facebook_emails(state.get("domain", ""), state.get("facebook_links", set()))
        if best and self._save_email(contact_id, best):
            logger.info("FACEBOOK FOUND %s -> %s", state.get("domain", ""), best)
        state["done"] = True

    def _save_email(self, contact_id: int, email: str) -> bool:
        if contact_id in self._saved_contacts:
            return True
        payload = {"id": str(contact_id), "email": email}
        resp = self.http.post_json(f"{self.base_url}/api/campaign/{self.campaign_id}/email_update", payload)
        if isinstance(resp, dict) and not resp.get("error"):
            self._saved_contacts.add(contact_id)
            return True
        logger.warning("Email save failed for contact %s: %s", contact_id, resp)
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="Fast email scraper using Scrapy (no JS).")
    p.add_argument("--campaign", required=True, help="Campaign ID (e.g., 95) or NAME (case-insensitive)")
    p.add_argument("--batch", type=int, default=10, help="How many contacts to pull from /nomail")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Campaign API base URL")
    p.add_argument("--concurrency", type=int, default=8, help="Concurrent requests")
    p.add_argument("--timeout", type=float, default=8.0, help="Per-page timeout (seconds)")
    p.add_argument("--links", type=int, default=5, help="Max child pages to visit per domain")
    p.add_argument("--min-domain-letters", type=int, default=2, help="Minimum letters in email root domain")
    p.add_argument("--domain-timeout", type=float, default=60.0, help="Total timeout per domain")
    p.add_argument("--max-batches", type=int, default=0, help="Max batches per run (0 = unlimited)")
    p.add_argument("--max-batches-facebook", type=int, default=0, help="Max Facebook batches per run (0 = disabled)")
    p.add_argument("--facebook", action="store_true", help="Enable Facebook page scraping")
    p.add_argument("--same-domain-only", action="store_true", help="Only follow links within the company domain")
    args = p.parse_args()
    set_min_domain_letters(args.min_domain_letters)

    base_url = normalize_base_url(args.base_url)
    http = HttpClient(timeout=20.0, max_retries=5)
    campaign_id = resolve_campaign_id(http, base_url, args.campaign)
    logger.info("Scrapy email scraper starting for campaign id=%s", campaign_id)
    if args.same_domain_only:
        logger.info("Same-domain-only crawling: ENABLED")
    if args.facebook and args.max_batches_facebook > 0:
        if args.same_domain_only:
            logger.info("Facebook scraping is enabled but skipped because same-domain-only mode is on.")
        else:
            logger.info("Facebook fallback enabled for first %s batch(es).", args.max_batches_facebook)
    elif args.facebook:
        logger.info("Facebook checkbox enabled but max Facebook batches is 0; Facebook fallback is disabled.")

    settings = {
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "%(asctime)s - %(levelname)s - %(message)s",
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,
        "DOWNLOAD_TIMEOUT": float(args.timeout),
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 1,
        "CONCURRENT_REQUESTS": max(1, int(args.concurrency)),
        "CONCURRENT_REQUESTS_PER_DOMAIN": max(1, int(args.concurrency)),
        "DOWNLOAD_MAXSIZE": 2 * 1024 * 1024,
        "TELNETCONSOLE_ENABLED": False,
        "USER_AGENT": "Mozilla/5.0 (compatible; EmailScraper/1.0; +https://leadtechx.com)",
    }

    process = CrawlerProcess(settings=settings)
    process.crawl(
        EmailSpider,
        base_url=base_url,
        campaign_id=campaign_id,
        batch=args.batch,
        max_batches=args.max_batches,
        links=args.links,
        domain_timeout=args.domain_timeout,
        http_timeout=20.0,
        http_max_retries=5,
        facebook=args.facebook,
        max_batches_facebook=args.max_batches_facebook,
        same_domain_only=args.same_domain_only,
    )
    process.start()


if __name__ == "__main__":
    main()
