from __future__ import annotations

# === BEGIN: user's scraper (Playwright + DOM/XPath), emails disabled ===

import asyncio
import logging
import os
import random
import re
import sys
import unicodedata
from datetime import datetime
from urllib.parse import urlparse

LOCAL_DEPS = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)

from bs4 import BeautifulSoup
from lxml import html
from browser_backend import AsyncBrowserRuntime, backend_display_name, normalize_proxy_url

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Utility functions
def safeString(value):
    if value is None or value == "":
        return ""
    return str(value).replace('"', '""')

def cleanUrl(url):
    if not url or not isinstance(url, str):
        return ""
    url = re.sub(r'^"|"$', '', url).strip()
    if url == "":
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    try:
        return urlparse(url).geturl()
    except ValueError:
        logger.warning(f"Invalid URL format: {url}")
        return ""

def getDomain(url):
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
        'vg', 'vi', 'vn', 'vu', 'wf', 'ws', 'xk', 'ye', 'yt', 'za', 'zm', 'zw',
    }
    try:
        urlObj = urlparse(url)
        hostParts = urlObj.hostname.lower().split('.')
        return (tlds & set(hostParts)).pop() if tlds & set(hostParts) else hostParts[-2]
    except Exception:
        return ""

def decodeCfEmail(encoded):
    if not encoded or len(encoded) < 2:
        return ""
    key = int(encoded[:2], 16)
    decoded = ""
    for i in range(2, len(encoded), 2):
        charCode = int(encoded[i:i+2], 16) ^ key
        decoded += chr(charCode)
    return decoded

async def extractEmail(url: str, name: str, browser, recursive: bool = True, max_concurrent: int = 5) -> dict:
    logger.setLevel(logging.CRITICAL)
    try:
        url = cleanUrl(url)
        if not url:
            return {"email": set()}

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        try:
            response = await asyncio.wait_for(page.goto(url, wait_until="domcontentloaded"), timeout=5000)
            content = await page.content() if response and response.status == 200 else ""
        except Exception:
            await context.close()
            return {"email": set()}
        await context.close()

        content = unicodedata.normalize('NFKC', content.encode().decode('utf-8', errors='ignore'))
        result = {"email": set()}
        linkedPages = set()

        # Multiple email patterns to catch different formats
        patterns = [
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?![^<]*>)",
            r"mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
            r"href=['\"]mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})['\"]"
        ]

        soup = BeautifulSoup(content, 'html.parser')

        # Search in all text content
        for pattern in patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                email = match if isinstance(match, str) else match[0] if match else ""
                if email and not (re.search(r'\{.*\}', email) or len(email.split()) > 1):
                    result["email"].add(email.strip().lower())

        # Also search in specific HTML elements
        for element in soup.find_all(['a', 'span', 'div', 'p', 'td', 'li']):
            text = element.get_text() if element else ""
            href = element.get('href', '') if element.name == 'a' else ""

            for pattern in patterns:
                if text:
                    matches = re.findall(pattern, text, re.IGNORECASE)
                    for match in matches:
                        email = match if isinstance(match, str) else match[0] if match else ""
                        if email:
                            result["email"].add(email.strip().lower())

                if href and href.startswith('mailto:'):
                    email = href.replace('mailto:', '').strip().lower()
                    if '@' in email and '.' in email.split('@')[-1]:
                        result["email"].add(email)

        cfEmail = soup.select_one('.__cf_email__')
        if cfEmail and cfEmail.get('data-cfemail'):
            result["email"].add(decodeCfEmail(cfEmail['data-cfemail']))

        if recursive:
            baseUrl = urlparse(url).geturl()
            links = [a.get('href') for a in soup.find_all('a', href=True)]
            targetPaths = [
                '/contact', '/contact-us', '/contact-me', '/contacts', '/contact-info', '/contact-information',
                '/about', '/about-me', '/about-us', '/team', '/our-team', '/meet-the-team',
                '/support', '/customer-service', '/feedback', '/help', '/sales', '/reach-us',
                'privacy', 'return', 'location', 'policy', 'faq', '/get-in-touch', '/reach-out',
                '/info', '/information', '/details', '/services', '/quote', '/estimate'
            ]
            for link in links:
                try:
                    fullUrl = urlparse(link, baseUrl).geturl()
                    for path in targetPaths:
                        if path in fullUrl:
                            linkedPages.add(fullUrl)
                            break
                except Exception:
                    continue

            if linkedPages:
                async def fetchPage(pageUrl: str) -> dict:
                    return await extractEmail(pageUrl, "", browser, False)
                tasks = [fetchPage(pageUrl) for pageUrl in list(linkedPages)[:max_concurrent]]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, dict):
                        result["email"].update(res["email"])

        allEmails = set()
        domainEmails = set()
        invalidPatterns = [
            '.png', '.jpg', '.jpeg', '.gif', '.webp', 'wixpress.com', 'sentry.io',
            'noreply', 'abuse', 'no-reply', 'subscribe', 'mailer-daemon', 'domain.com',
            'email.com', 'yourname', 'wix.com',
        ]
        domain = getDomain(url)

        for email in result["email"]:
            cleanedEmail = email.replace('u003e', '').replace('&lt;', '').replace('&gt;', '').lower().strip()
            # Skip obviously invalid emails
            if (any(pattern in cleanedEmail for pattern in invalidPatterns) or
                not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', cleanedEmail) or
                cleanedEmail.count('@') != 1):
                continue
            allEmails.add(cleanedEmail)
            if domain and (domain in cleanedEmail or cleanedEmail.split('@')[1].endswith('.' + domain)):
                domainEmails.add(cleanedEmail)

        result["email"] = domainEmails if domainEmails else allEmails
        return result

    except Exception:
        return {"email": set()}
    finally:
        logger.setLevel(logging.INFO)

async def process_business(business, browser, extract_emails: bool = False) -> dict:
    try:
        name = business.xpath(".//div[contains(@class, 'fontHeadlineSmall')]/text()")[0] if business.xpath(".//div[contains(@class, 'fontHeadlineSmall')]/text()") else "N/A"
        logger.info(f"🔍 Processing: {name}")
        maps_url = business.xpath(".//a[starts-with(@href, 'https://www.google.com/maps/place/')]/@href")[0] if business.xpath(".//a[starts-with(@href, 'https://www.google.com/maps/place/')]/@href") else "N/A"
        rating = business.xpath(".//span[@role='img' and contains(@aria-label, 'stars')]/span[1]/text()")[0] if business.xpath(".//span[@role='img' and contains(@aria-label, 'stars')]/span[1]/text()") else "N/A"
        reviews = business.xpath(".//span[@role='img' and contains(@aria-label, 'stars')]//span[contains(text(), '(')]/text()")[0] if business.xpath(".//span[@role='img' and contains(@aria-label, 'stars')]//span[contains(text(), '(')]/text()") else "N/A"
        if reviews != "N/A":
            reviews = reviews.strip('()')
        business_type = business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']][1]//span[not(@aria-hidden='true') and not(span[@aria-hidden='true']) and not(contains(text(), 'Closed')) and not(contains(text(), 'Open')) and not(starts-with(text(), '('))][1]/span/text()")[0] if business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']][1]//span[not(@aria-hidden='true') and not(span[@aria-hidden='true']) and not(contains(text(), 'Closed')) and not(contains(text(), 'Open')) and not(starts-with(text(), '('))][1]/span/text()") else "N/A"
        address = business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']]//span[preceding-sibling::span[@aria-hidden='true' and text()='·'] and not(.//a[@href])]/text()")[0] if business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']]//span[preceding-sibling::span[@aria-hidden='true' and text()='·'] and not(.//a[@href])]/text()") else "N/A"
        hours = business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']]//span[contains(text(), 'Open') or contains(text(), 'Closed')]/text()")[0] if business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']]//span[contains(text(), 'Open') or contains(text(), 'Closed')]/text()") else "N/A"
        phone = business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']]//span[starts-with(text(), '(') and contains(text(), ')') and contains(text(), '-') and string-length(normalize-space(text())) >= 10]/text()")[0] if business.xpath(".//div[.//span[@aria-hidden='true' and text()='·']]//span[starts-with(text(), '(') and contains(text(), ')') and contains(text(), '-') and string-length(normalize-space(text())) >= 10]/text()") else "N/A"
        website = business.xpath(".//a[@data-value='Website']/@href")[0] if business.xpath(".//a[@data-value='Website']/@href") else "N/A"
        if website != "N/A":
            parsed_url = urlparse(website)
            website = f"{parsed_url.scheme}://{parsed_url.netloc}"
        if extract_emails and website != "N/A":
            email_result = await extractEmail(website, name, browser)
            email = ",".join(email_result["email"]) if email_result["email"] else "N/A"
        else:
            email = "N/A"
        sponsored = business.xpath(".//h1[.//span[text()='Sponsored']]//span[text()='Sponsored']/text()")[0] if business.xpath(".//h1[.//span[text()='Sponsored']]//span[text()='Sponsored']/text()") else "Not Sponsored"

        return {
            "Name": name,
            "Rating": rating,
            "Reviews": reviews,
            "Business Type": business_type,
            "Address": address,
            "Hours": hours,
            "Phone": phone,
            "Website": website,
            "Url": maps_url,
            "Email": email,
            "Sponsored": sponsored
        }
    except Exception as e:
        logger.error(f"Error processing business: {e}")
        return None

async def scrape_business_info(page, browser, max_concurrent: int = 5):
    html_content = await page.content()
    tree = html.fromstring(html_content)
    businesses = tree.xpath("//div[@role='feed']//a[starts-with(@href, 'https://www.google.com/maps/place/')]/parent::div")

    logger.info(f"Found {len(businesses)} businesses to process with max {max_concurrent} concurrent")

    # Create semaphore to control concurrency
    semaphore = asyncio.Semaphore(max_concurrent)

    async def process_with_semaphore(business, idx):
        async with semaphore:
            logger.info(f"🔄 Starting business {idx + 1}/{len(businesses)}")
            result = await process_business(business, browser, extract_emails=False)
            if result:
                logger.info(f"✅ COMPLETED {idx + 1}/{len(businesses)}: {result['Name']} | {result['Rating']} ⭐ ({result['Reviews']} reviews) | {result['Business Type']} | {result['Address']} | {result['Phone']} | {result['Website']} | {result['Email']}")
            else:
                logger.info(f"❌ FAILED {idx + 1}/{len(businesses)}: Could not process business")
            return result

    # Process all businesses with controlled concurrency
    tasks = [process_with_semaphore(business, idx) for idx, business in enumerate(businesses)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter successful results
    all_results = []
    for result in results:
        if isinstance(result, dict) and result is not None:
            all_results.append(result)
        elif isinstance(result, Exception):
            logger.error(f"Exception occurred: {result}")

    logger.info(f"✅ TOTAL COMPLETED: {len(all_results)}/{len(businesses)} businesses processed successfully")
    return all_results


def _canonical_website(url: str) -> str:
    if not url or url == "N/A":
        return "N/A"
    try:
        parsed = urlparse(str(url).strip())
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return str(url).strip()
    except Exception:
        return str(url).strip() or "N/A"


async def extract_place_details(page) -> dict:
    raw = await page.evaluate(
        """() => {
            const text = (el) => (el && el.textContent ? el.textContent.replace(/\\s+/g, ' ').trim() : '');
            const findByItem = (itemId) => document.querySelector(`[data-item-id="${itemId}"]`);
            const addressEl = findByItem('address');
            const locatedInEl = findByItem('locatedin');
            const plusCodeEl = findByItem('oloc');
            const phoneEl = document.querySelector('button[data-item-id^="phone:"]');
            const websiteEl = document.querySelector('a[data-item-id="authority"]');
            const bookingEl = document.querySelector('a[data-item-id^="action:"]');

            const ratingNode = document.querySelector('span[role="img"][aria-label*="stars"]');
            const ratingLabel = ratingNode ? (ratingNode.getAttribute('aria-label') || '') : '';
            const ratingMatch = ratingLabel.match(/([0-9]+(?:\\.[0-9]+)?)/);
            const rating = ratingMatch ? ratingMatch[1] : '';

            const extractNumber = (value) => {
                const src = (value || '').toString();
                const match = src.match(/([\\d][\\d,.]*)/);
                return match ? match[1] : '';
            };

            let reviews = '';
            const reviewNodes = Array.from(
                document.querySelectorAll('button[aria-label], a[aria-label], span[role="img"][aria-label], div[role="img"][aria-label]')
            );
            for (const node of reviewNodes) {
                const label = (node.getAttribute('aria-label') || '').trim();
                if (!/review/i.test(label)) continue;
                reviews = extractNumber(label);
                if (reviews) break;
            }

            if (!reviews) {
                const reviewNode = document.querySelector('button[aria-label*="reviews"], a[aria-label*="reviews"]');
                const reviewText = reviewNode ? (reviewNode.getAttribute('aria-label') || text(reviewNode)) : '';
                reviews = extractNumber(reviewText);
            }

            if (!reviews && ratingNode) {
                const ratingContainerText = text(ratingNode.parentElement);
                const parenMatch = ratingContainerText.match(/\\(([\\d][\\d,.]*)\\)/);
                reviews = parenMatch ? parenMatch[1] : '';
            }

            const categoryNode =
                document.querySelector('button[jsaction*="pane.rating.category"]') ||
                document.querySelector('button[jsaction*="category"]');

            const hoursSummaryNode =
                document.querySelector('div[jsaction*="openhours"] .ZDu9vd') ||
                document.querySelector('div[jsaction*="openhours"]');
            const hoursRows = Array.from(document.querySelectorAll('table.eK4R0e tr'))
                .map((row) => {
                    const tds = row.querySelectorAll('td');
                    if (!tds || tds.length < 2) return '';
                    const day = text(tds[0]);
                    const val = text(tds[1]);
                    return day && val ? `${day}: ${val}` : '';
                })
                .filter(Boolean);

            return {
                name: text(document.querySelector('h1')),
                address: text(addressEl ? (addressEl.querySelector('.Io6YTe') || addressEl) : null),
                locatedIn: text(locatedInEl ? (locatedInEl.querySelector('.Io6YTe') || locatedInEl) : null),
                plusCode: text(plusCodeEl ? (plusCodeEl.querySelector('.Io6YTe') || plusCodeEl) : null),
                phone: text(phoneEl ? (phoneEl.querySelector('.Io6YTe') || phoneEl) : null),
                website: websiteEl ? (websiteEl.getAttribute('href') || '') : '',
                bookingLink: bookingEl ? (bookingEl.getAttribute('href') || '') : '',
                hoursSummary: text(hoursSummaryNode),
                hoursRows: hoursRows,
                rating: rating,
                reviews: reviews,
                category: text(categoryNode),
                url: window.location.href || '',
            };
        }"""
    )

    if not isinstance(raw, dict):
        return {}

    hours_rows = raw.get("hoursRows") or []
    if isinstance(hours_rows, list):
        hours_detail = "; ".join([str(v).strip() for v in hours_rows if str(v).strip()])
    else:
        hours_detail = ""

    out = {
        "Name": str(raw.get("name") or "").strip() or "N/A",
        "Address": str(raw.get("address") or "").strip() or "N/A",
        "Phone": str(raw.get("phone") or "").strip() or "N/A",
        "Website": _canonical_website(str(raw.get("website") or "").strip()),
        "Booking Link": str(raw.get("bookingLink") or "").strip() or "N/A",
        "Located In": str(raw.get("locatedIn") or "").strip() or "N/A",
        "Plus Code": str(raw.get("plusCode") or "").strip() or "N/A",
        "Hours": str(raw.get("hoursSummary") or "").strip() or "N/A",
        "Hours Detail": hours_detail or "N/A",
        "Rating": str(raw.get("rating") or "").strip() or "N/A",
        "Reviews": str(raw.get("reviews") or "").strip() or "N/A",
        "Business Type": str(raw.get("category") or "").strip() or "N/A",
        "Url": str(raw.get("url") or "").strip() or "N/A",
    }
    return out


async def enrich_businesses_with_place_pages(page, businesses):
    if not businesses:
        return []

    logger.info("Slow mode enabled: opening each place page to collect detailed business info.")
    detail_cache = {}

    for idx, business in enumerate(businesses, 1):
        maps_url = str(business.get("Url", "") or "").strip()
        if not maps_url or maps_url == "N/A":
            continue

        if maps_url in detail_cache:
            detail = detail_cache[maps_url]
        else:
            try:
                logger.info(f"[slow] Opening {idx}/{len(businesses)}: {business.get('Name', 'N/A')}")
                await page.goto(maps_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_selector("h1, [data-item-id='address'], [data-item-id='authority']", timeout=20000)
                await page.wait_for_timeout(700 + int(random.random() * 700))
                detail = await extract_place_details(page)
                detail_cache[maps_url] = detail
            except Exception as exc:
                logger.warning(f"[slow] Failed detail extraction for '{business.get('Name','N/A')}': {exc}")
                continue

        for key in (
            "Name",
            "Address",
            "Phone",
            "Website",
            "Rating",
            "Reviews",
            "Business Type",
            "Hours",
            "Url",
            "Located In",
            "Plus Code",
            "Booking Link",
            "Hours Detail",
        ):
            value = str(detail.get(key, "") or "").strip()
            if value and value != "N/A":
                business[key] = value

    return businesses

async def scroll_google_maps(search_query: str, max_concurrent: int = 3):
    runtime = AsyncBrowserRuntime(
        headless=False,
        chromium_args=[
            '--disable-blink-features=AutomationControlled',
            '--disable-extensions',
            '--disable-plugins',
            '--disable-images',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
        ],
        camoufox_options={"block_images": True},
        proxy_url=DEFAULT_PROXY_URL,
    )
    try:
        browser = await runtime.launch()
        logger.info("Maps browser backend: %s", backend_display_name(None))
        try:
            page = await browser.new_page(viewport={'width': 1280, 'height': 720})

            search_url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}"
            logger.info(f"Navigating to {search_url}")
            await page.goto(search_url)
            logger.info("Page loaded")

            await asyncio.sleep(5)
            feed_container = page.locator('div[aria-label*="Results"][role="feed"]')
            if await feed_container.count() == 0:
                logger.error("Feed container not found")
                await page.screenshot(path="screenshot.png")
                return

            logger.info("Found feed container")
            await feed_container.scroll_into_view_if_needed()

            max_scroll_time = 600
            start_time = asyncio.get_event_loop().time()
            logger.info("Starting scroll loop")

            while (asyncio.get_event_loop().time() - start_time) < max_scroll_time:
                logger.info("Scrolling...")
                await feed_container.evaluate("""
                    (element) => {
                        return new Promise((resolve) => {
                            let totalHeight = 0;
                            const distance = 300;
                            let scrollTimeout;

                            const scroll = () => {
                                const scrollHeight = element.scrollHeight;
                                element.scrollBy(0, distance);
                                totalHeight += distance;

                                if (totalHeight >= scrollHeight) {
                                    clearTimeout(scrollTimeout);
                                    resolve();
                                } else {
                                    scrollTimeout = setTimeout(scroll, 500);
                                }
                            };

                            scroll();
                        });
                    }
                """)

                timestamp = datetime.now().strftime("%H:%M:%S")
                delay = random.uniform(2.0, 3.0)
                logger.info(f"Scrolled down at {timestamp} with delay {delay:.1f} seconds")
                await asyncio.sleep(delay)

                page_end = await page.locator('//span[contains(text(), "reached the end of the list")]').count()
                if page_end > 0:
                    logger.info("Reached the end of the list")
                    break

            logger.info(f"Scraping business info from feed with {max_concurrent} concurrent processes")
            await scrape_business_info(page, browser, max_concurrent)

            await page.screenshot(path="screenshot.png")
            logger.info("Screenshot saved as screenshot.png")
            logger.info("Browser session completed")

        except Exception as e:
            logger.error(f"Error occurred: {e}")
            await page.screenshot(path="screenshot.png")
    finally:
        await runtime.close()
        logger.info("Browser closed")

async def main():
    search_query = "locksmith kenosha"
    max_concurrent = 3  # Adjust this number to control concurrency (1-10 recommended)
    await scroll_google_maps(search_query, max_concurrent)

# (removed old __main__ runner)

# === END: user's scraper ===


# === BEGIN: API Orchestration (ported from extension) ===
import os
import json
import time
import random
import logging
import ssl
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.environ.get(
    "LEADS_API_BASE_URL",
    "https://scrapiq.leadtechx.com/api",
)
BATCH_SIZE = int(os.environ.get("LEADS_BATCH_SIZE", "20"))
REQUEST_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT", "20"))
MAX_RETRIES = int(os.environ.get("HTTP_MAX_RETRIES", "5"))
RETRY_BACKOFF_S = float(os.environ.get("HTTP_RETRY_BACKOFF", "2.0"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
CONTACTS_SEND_MAX_RETRIES = int(os.environ.get("CONTACTS_SEND_MAX_RETRIES", "5"))
CONTACTS_SEND_RETRY_BACKOFF_S = float(os.environ.get("CONTACTS_SEND_RETRY_BACKOFF_S", "1.5"))
DEFAULT_SCRAPE_MODE = os.environ.get("MAPS_SCRAPE_MODE", "fast")
DEFAULT_SHOW_BROWSER = os.environ.get("MAPS_SHOW_BROWSER", "0").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_SLOW_PLACE_PAUSE_MIN_S = float(os.environ.get("MAPS_SLOW_PLACE_PAUSE_MIN_S", "0.8"))
DEFAULT_SLOW_PLACE_PAUSE_MAX_S = float(os.environ.get("MAPS_SLOW_PLACE_PAUSE_MAX_S", "1.8"))
DEFAULT_SCROLL_PAUSE_MIN_S = float(os.environ.get("MAPS_SCROLL_PAUSE_MIN_S", "0.8"))
DEFAULT_SCROLL_PAUSE_MAX_S = float(os.environ.get("MAPS_SCROLL_PAUSE_MAX_S", "0.8"))
DEFAULT_DETAIL_WORKERS = int(os.environ.get("MAPS_DETAIL_WORKERS", "1"))
DEFAULT_PROXY_URL = normalize_proxy_url(os.environ.get("MAPS_PROXY_URL", ""))
VALID_SCRAPE_MODES = {"fast", "slow"}


def normalize_scrape_mode(mode: Optional[str]) -> str:
    value = str(mode or "").strip().lower()
    if value in VALID_SCRAPE_MODES:
        return value
    return "fast"


def normalize_pause_range(min_s: Optional[float], max_s: Optional[float]) -> Tuple[float, float]:
    try:
        low = float(DEFAULT_SLOW_PLACE_PAUSE_MIN_S if min_s is None else min_s)
    except Exception:
        low = float(DEFAULT_SLOW_PLACE_PAUSE_MIN_S)
    try:
        high = float(DEFAULT_SLOW_PLACE_PAUSE_MAX_S if max_s is None else max_s)
    except Exception:
        high = float(DEFAULT_SLOW_PLACE_PAUSE_MAX_S)
    low = max(0.0, low)
    high = max(0.0, high)
    if high < low:
        low, high = high, low
    return low, high


def normalize_scroll_pause_range(min_s: Optional[float], max_s: Optional[float]) -> Tuple[float, float]:
    try:
        low = float(DEFAULT_SCROLL_PAUSE_MIN_S if min_s is None else min_s)
    except Exception:
        low = float(DEFAULT_SCROLL_PAUSE_MIN_S)
    try:
        high = float(DEFAULT_SCROLL_PAUSE_MAX_S if max_s is None else max_s)
    except Exception:
        high = float(DEFAULT_SCROLL_PAUSE_MAX_S)
    low = max(0.0, low)
    high = max(0.0, high)
    if high < low:
        low, high = high, low
    return low, high


def normalize_show_browser(value: Optional[Any]) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(DEFAULT_SHOW_BROWSER)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def normalize_detail_workers(value: Optional[Any]) -> int:
    if value is None:
        base = DEFAULT_DETAIL_WORKERS
    else:
        base = value
    try:
        workers = int(base)
    except Exception:
        workers = int(DEFAULT_DETAIL_WORKERS)
    return max(1, workers)


def normalize_maps_proxy_url(value: Optional[Any]) -> str:
    if value is None:
        return normalize_proxy_url(DEFAULT_PROXY_URL)
    return normalize_proxy_url(str(value))

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore


class HttpClient:
    def __init__(self, timeout: float = REQUEST_TIMEOUT_S, max_retries: int = MAX_RETRIES, backoff: float = RETRY_BACKOFF_S):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.insecure = False
        self._insecure_logged = False

    def _sleep(self, attempt: int):
        delay = self.backoff * (1 + attempt)
        delay *= (0.75 + random.random() * 0.5)
        time.sleep(delay)

    @staticmethod
    def _is_ssl_verify_error(exc: Exception) -> bool:
        text = str(exc)
        return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text.lower()

    def _switch_to_insecure(self) -> None:
        self.insecure = True
        if not self._insecure_logged:
            logger.warning("SSL verification failed; switching to insecure HTTPS for API calls.")
            self._insecure_logged = True

    def get_text(self, url: str, headers: Optional[Dict[str,str]] = None) -> str:
        for attempt in range(self.max_retries):
            try:
                if requests is not None:
                    r = requests.get(
                        url,
                        headers=headers,
                        timeout=self.timeout,
                        allow_redirects=True,
                        verify=(not self.insecure),
                    )
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                    return r.text or ""
                else:
                    req = urllib.request.Request(url, headers=headers or {})
                    ctx = ssl._create_unverified_context() if self.insecure else None
                    with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                        return resp.read().decode("utf-8", errors="ignore")
            except Exception as e:
                if self._is_ssl_verify_error(e) and not self.insecure:
                    self._switch_to_insecure()
                    continue
                if attempt == self.max_retries - 1:
                    logger.error(f"GET failed for {url}: {e}")
                    return ""
                logger.warning(f"GET retry {attempt+1}/{self.max_retries} for {url}: {e}")
                self._sleep(attempt)
        return ""

    def get_json(self, url: str, headers: Optional[Dict[str,str]] = None) -> Dict[str,Any]:
        text = self.get_text(url, headers=headers)
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception as e:
            logger.error(f"JSON parse error for {url}: {e}; raw={text[:300]}")
            return {}

    def post_json(self, url: str, payload: Any, headers: Optional[Dict[str,str]] = None) -> Tuple[int,str]:
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(self.max_retries):
            try:
                if requests is not None:
                    r = requests.post(
                        url,
                        data=json.dumps(payload),
                        headers={"Content-Type":"application/json", **(headers or {})},
                        timeout=self.timeout,
                        allow_redirects=True,
                        verify=(not self.insecure),
                    )
                    return r.status_code, r.text or ""
                else:
                    req = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json", **(headers or {})})
                    ctx = ssl._create_unverified_context() if self.insecure else None
                    with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                        code = getattr(resp, "status", 200) or 200
                        text = resp.read().decode("utf-8", errors="ignore")
                        return code, text
            except Exception as e:
                if self._is_ssl_verify_error(e) and not self.insecure:
                    self._switch_to_insecure()
                    continue
                if attempt == self.max_retries - 1:
                    logger.error(f"POST failed for {url}: {e}")
                    return 599, ""
                logger.warning(f"POST retry {attempt+1}/{self.max_retries} for {url}: {e}")
                self._sleep(attempt)
        return 599, ""


@dataclass
class Campaign:
    id: str
    name: str


@dataclass
class RequestItem:
    id: str
    req_text: str
    status: str = ""


class LeadsApiClient:
    def __init__(self, base_url: str, http: Optional[HttpClient] = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or HttpClient()

    def get_active_campaigns(self) -> List[Campaign]:
        url = f"{self.base_url}/campaigns/active"
        data = self.http.get_json(url)
        out: List[Campaign] = []
        for c in (data.get("campaigns") or []):
            cid = str(c.get("id","")).strip()
            name = str(c.get("name","")).strip()
            if cid and name:
                out.append(Campaign(id=cid, name=name))
        return out

    def get_requests_for_campaign_name(self, campaign_name: str, include_inuse: bool = False) -> List[RequestItem]:
        enc = urllib.parse.quote(campaign_name)
        suffix = "?include_inuse=true" if include_inuse else ""
        url = f"{self.base_url}/campaign/{enc}/requests{suffix}"
        data = self.http.get_json(url)
        out: List[RequestItem] = []
        for r in (data.get("requests") or []):
            rid = str(r.get("id","")).strip()
            txt = str(r.get("req_text","")).strip()
            status = str(r.get("status","")).strip().lower()
            if rid:
                out.append(RequestItem(id=rid, req_text=txt, status=status))
        return out

    def get_campaign_request_progress(self, campaign_id: str) -> Dict[str, Any]:
        enc = urllib.parse.quote(str(campaign_id))
        url = f"{self.base_url}/dashboard/runtime-status?campaign_ids={enc}"
        data = self.http.get_json(url)
        campaigns = data.get("campaigns") if isinstance(data, dict) else {}
        item = campaigns.get(str(campaign_id)) if isinstance(campaigns, dict) else {}
        progress = item.get("requests") if isinstance(item, dict) else {}
        return progress if isinstance(progress, dict) else {}

    def set_request_status(self, request_id: str, state: str) -> None:
        url = f"{self.base_url}/request/{request_id}/status/{state}"
        _ = self.http.get_text(url)

    def complete_campaign(self, campaign_id: str) -> None:
        url = f"{self.base_url}/campaign/{campaign_id}/complete"
        _ = self.http.get_text(url)

    def send_contacts(self, contacts: List[Dict[str,Any]]) -> bool:
        url = f"{self.base_url}/contacts"
        retries = max(1, int(CONTACTS_SEND_MAX_RETRIES))
        for attempt in range(1, retries + 1):
            code, text = self.http.post_json(url, contacts)
            if 200 <= int(code or 0) < 300:
                if attempt > 1:
                    logger.info(
                        "/contacts succeeded on retry %d/%d (batch_size=%d)",
                        attempt,
                        retries,
                        len(contacts),
                    )
                return True

            snippet = (text or "")[:200]
            retryable = int(code or 0) >= 500 or int(code or 0) in {408, 429, 599}
            logger.error(
                "/contacts failed attempt %d/%d: code=%s text=%s",
                attempt,
                retries,
                code,
                snippet,
            )
            if (not retryable) or attempt >= retries:
                break

            delay = CONTACTS_SEND_RETRY_BACKOFF_S * attempt * (0.75 + random.random() * 0.5)
            time.sleep(delay)

        return False


PLACE_ID_RE = re.compile(r"place_id:([^\\\"/]+)")

def _strip_quotes(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r'(^\\\"|\\\"$)', "", str(s)).strip()


def _sanitize_domain_value(raw: Optional[str]) -> str:
    value = _strip_quotes(raw).strip()
    if not value:
        return "none"
    lowered = value.lower()
    if lowered in {"n/a", "na", "none", "null"}:
        return "none"
    if "google.com/maps/place" in lowered or "google.com/maps/search" in lowered or "maps.google.com" in lowered:
        return "none"
    return value


def _sanitize_rating_value(raw: Optional[str]) -> str:
    value = _strip_quotes(raw).strip()
    if not value:
        return "0"
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
    if not m:
        return "0"
    try:
        return str(float(m.group(1)))
    except Exception:
        return "0"


def format_contacts_for_api(batch: List[Dict[str, Any]], campaign_id: str, request_id: str) -> List[Dict[str, Any]]:
    formatted: List[Dict[str, Any]] = []
    for contact in batch:
        url = _strip_quotes(contact.get("url", ""))
        m = PLACE_ID_RE.search(url or "")
        place_id = m.group(1) if m else ""

        def _g(key: str, default: str = "") -> str:
            v = contact.get(key, default)
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            return _strip_quotes(str(v))

        formatted.append(
            {
                "campaign_id": str(campaign_id),
                "request_id": str(request_id),
                "business_name": _g("companyName") or "Unknown Business",
                "address": _g("address"),
                "category": _g("category"),
                "rating": _sanitize_rating_value(_g("rating")),
                "review_count": int(re.sub(r'[^\d]', '', _g("reviews") or "0") or "0"),
                "phone": _g("phone"),
                "domain": _sanitize_domain_value(_g("website")),
                "email": "",
                "facebook": "",
                "instagram": "",
                "twitter": "",
                "yelp": "",
                "place_id": place_id,
            }
        )
    return formatted


def write_batch_csv(batch: List[Dict[str, Any]], out_path: str, write_header: bool = False) -> None:
    fieldnames = [
        "companyName",
        "address",
        "phone",
        "rating",
        "reviews",
        "category",
        "website",
        "url",
        "locatedIn",
        "hours",
        "hoursDetail",
        "plusCode",
        "bookingLink",
        "email",
        "facebook",
        "instagram",
        "twitter",
        "yelp",
    ]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    import csv
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for row in batch:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


class CampaignProcessor:
    def __init__(
        self,
        api: LeadsApiClient,
        batch_size: int = BATCH_SIZE,
        csv_dir: Optional[str] = None,
        scrape_mode: Optional[str] = None,
        show_browser: Optional[bool] = None,
        slow_place_pause_min_s: Optional[float] = None,
        slow_place_pause_max_s: Optional[float] = None,
        scroll_pause_min_s: Optional[float] = None,
        scroll_pause_max_s: Optional[float] = None,
        detail_workers: Optional[int] = None,
        proxy_url: Optional[str] = None,
        request_workers: Optional[int] = None,
    ):
        self.api = api
        self.batch_size = batch_size
        self.csv_dir = csv_dir
        self.scrape_mode = normalize_scrape_mode(scrape_mode or DEFAULT_SCRAPE_MODE)
        self.show_browser = normalize_show_browser(show_browser)
        self.slow_place_pause_min_s, self.slow_place_pause_max_s = normalize_pause_range(
            slow_place_pause_min_s,
            slow_place_pause_max_s,
        )
        self.scroll_pause_min_s, self.scroll_pause_max_s = normalize_scroll_pause_range(
            scroll_pause_min_s,
            scroll_pause_max_s,
        )
        self.detail_workers = normalize_detail_workers(detail_workers)
        self.proxy_url = normalize_maps_proxy_url(proxy_url)
        if request_workers is None:
            request_workers = MAX_CONCURRENT
        try:
            self.request_workers = max(1, int(request_workers))
        except Exception:
            self.request_workers = max(1, int(MAX_CONCURRENT))
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _store_unsent_batch(campaign_id: str, request_id: str, payload: List[Dict[str, Any]]) -> None:
        try:
            failed_dir = os.path.join("queue", "failed")
            os.makedirs(failed_dir, exist_ok=True)
            path = os.path.join(failed_dir, "maps_unsent_contacts.jsonl")
            record = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "campaign_id": str(campaign_id),
                "request_id": str(request_id),
                "size": len(payload),
                "contacts": payload,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.error("Stored unsent batch locally: %s (size=%d)", path, len(payload))
        except Exception as exc:
            logger.error("Failed to persist unsent batch locally: %s", exc)

    def _on_batch(self, campaign_id: str, request_id: str, batch: List[Dict[str, Any]], total_seen: int) -> None:
        if not batch:
            return
        if self.csv_dir:
            out = os.path.join(self.csv_dir, f"leads_{campaign_id}_{request_id}.csv")
            write_batch_csv(batch, out, write_header=not os.path.exists(out))
        payload = format_contacts_for_api(batch, campaign_id, request_id)
        failed_payload = list(payload)
        ok = self.api.send_contacts(payload)
        if not ok and len(payload) > 1:
            logger.warning(
                "Batch send failed for request %s; retrying %d leads individually.",
                request_id,
                len(payload),
            )
            sent_individual = 0
            still_failed: List[Dict[str, Any]] = []
            for lead in payload:
                if self.api.send_contacts([lead]):
                    sent_individual += 1
                else:
                    still_failed.append(lead)
            ok = sent_individual == len(payload)
            failed_payload = still_failed
            if sent_individual:
                logger.info(
                    "Individual resend recovered %d/%d leads for request %s",
                    sent_individual,
                    len(payload),
                    request_id,
                )
            if still_failed:
                failed_names = [str(l.get("business_name", "")) for l in still_failed[:5]]
                logger.error(
                    "Still failed after individual retries for request %s: %d lead(s). sample=%s",
                    request_id,
                    len(still_failed),
                    failed_names,
                )
        if ok:
            logger.info(f"Sent batch of {len(batch)} leads (total_seen={total_seen}) for request {request_id}")
        else:
            logger.error(f"Failed sending batch for request {request_id}")
            self._store_unsent_batch(campaign_id, request_id, failed_payload)

    def process_campaign(self, campaign: Campaign) -> None:
        logger.info(f"Starting campaign: {campaign.name} (ID: {campaign.id})")
        if self.scrape_mode == "slow":
            self._process_campaign_slow(campaign)
            return
        self._process_campaign_fast(campaign)

    def _request_workers_for_pending(self, pending_count: int) -> int:
        if pending_count <= 0:
            return 1
        return max(1, min(self.request_workers, pending_count))

    def _process_single_request_fast(self, campaign: Campaign, request: RequestItem) -> None:
        logger.info(f"[fast] Processing request {request.id} | query='{request.req_text}'")
        total_seen = 0
        dedupe_keys: Set[str] = set()
        stage_failed = False

        def batch_callback(batch: List[Dict[str, Any]]):
            nonlocal total_seen, dedupe_keys
            if not batch:
                return
            cleaned: List[Dict[str, Any]] = []
            for lead in batch:
                key = (lead.get("url", ""), lead.get("companyName", ""), lead.get("address", ""))
                if key in dedupe_keys:
                    continue
                dedupe_keys.add(key)
                cleaned.append(lead)
            if not cleaned:
                return
            total_seen += len(cleaned)
            for i in range(0, len(cleaned), self.batch_size):
                sub = cleaned[i : i + self.batch_size]
                self._on_batch(campaign.id, request.id, sub, total_seen)

        try:
            ok = run_scrape_and_yield_batches(
                request.req_text,
                self.batch_size,
                batch_callback,
                scrape_mode=self.scrape_mode,
                show_browser=self.show_browser,
                scroll_pause_min_s=self.scroll_pause_min_s,
                scroll_pause_max_s=self.scroll_pause_max_s,
                proxy_url=self.proxy_url,
            )
            if not ok:
                logger.warning(f"[fast] Scrape returned no data for request {request.id}")
        except Exception as e:
            stage_failed = True
            logger.exception(f"[fast] Scrape error for request {request.id}: {e}")
        finally:
            if self._stop:
                logger.warning(
                    "[fast] Stop requested while request %s was running; keeping request in current state for takeover.",
                    request.id,
                )
                return
            if stage_failed:
                logger.warning(
                    "[fast] Request %s stays in current state after scrape failure (API does not accept 'pending').",
                    request.id,
                )
            else:
                self.api.set_request_status(request.id, "completed")
                logger.info(f"[fast] Completed request {request.id} (total leads seen: {total_seen})")
            time.sleep(1.0)
        if stage_failed:
            raise RuntimeError(f"Maps scraping failed for request {request.id}")

    def _process_single_request_slow(self, campaign: Campaign, request: RequestItem) -> None:
        logger.info(f"[slow] Processing request {request.id} | query='{request.req_text}'")
        totals_by_request: Dict[str, int] = {request.id: 0}

        def on_batch_for_request(request_id: str, batch: List[Dict[str, Any]]) -> None:
            if not batch:
                return
            totals_by_request[request_id] = totals_by_request.get(request_id, 0) + len(batch)
            self._on_batch(campaign.id, request_id, batch, totals_by_request[request_id])

        stage_failed = False
        stage_error: Optional[Exception] = None
        try:
            ok = run_campaign_slow_dedup_and_yield_batches(
                [request],
                self.batch_size,
                on_batch_for_request,
                show_browser=self.show_browser,
                pause_min_s=self.slow_place_pause_min_s,
                pause_max_s=self.slow_place_pause_max_s,
                scroll_pause_min_s=self.scroll_pause_min_s,
                scroll_pause_max_s=self.scroll_pause_max_s,
                detail_workers=self.detail_workers,
                proxy_url=self.proxy_url,
                should_stop=lambda: self._stop,
            )
            if not ok:
                logger.warning("[slow] Request %s scrape returned no data.", request.id)
        except Exception as e:
            stage_failed = True
            stage_error = e
            logger.exception(f"[slow] Scrape error for request {request.id}: {e}")
        finally:
            if self._stop:
                logger.warning(
                    "[slow] Stop requested while request %s was running; keeping request in current state for takeover.",
                    request.id,
                )
                return
            if stage_failed:
                logger.warning(
                    "[slow] Request %s stays in current state after scrape failure (API does not accept 'pending').",
                    request.id,
                )
                time.sleep(1.0)
                return
            current_total = totals_by_request.get(request.id, 0)
            self.api.set_request_status(request.id, "completed")
            logger.info(
                "[slow] Completed request %s (total leads seen: %d)",
                request.id,
                current_total,
            )
            time.sleep(1.0)
        if stage_failed:
            raise RuntimeError(
                f"[slow] Campaign scrape failed for campaign {campaign.id}, request {request.id}: {stage_error}"
            ) from stage_error

    def _process_campaign_fast(self, campaign: Campaign) -> None:
        while not self._stop:
            reqs = self.api.get_requests_for_campaign_name(campaign.name)
            if not reqs:
                logger.info("No more requests; completing campaign.")
                self.api.complete_campaign(campaign.id)
                break
            pending: List[RequestItem] = []
            for request in reqs:
                if not request.req_text:
                    logger.warning(f"Empty req_text for request {request.id}; marking completed.")
                    self.api.set_request_status(request.id, "completed")
                    continue
                pending.append(request)

            if not pending:
                time.sleep(0.8)
                continue

            worker_count = self._request_workers_for_pending(len(pending))
            selected = pending[:worker_count]
            logger.info(
                "[fast] Request-level parallelism: workers=%d, selected_requests=%d, total_pending=%d",
                worker_count,
                len(selected),
                len(pending),
            )
            for request in selected:
                self.api.set_request_status(request.id, "inuse")

            errors: List[Exception] = []
            if worker_count == 1:
                request = selected[0]
                try:
                    self._process_single_request_fast(campaign, request)
                except Exception as exc:
                    errors.append(exc)
            else:
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="maps-fast-req") as pool:
                    fut_to_req = {
                        pool.submit(self._process_single_request_fast, campaign, request): request
                        for request in selected
                    }
                    for fut in as_completed(fut_to_req):
                        request = fut_to_req[fut]
                        try:
                            fut.result()
                        except Exception as exc:
                            logger.error("[fast] Request %s failed: %s", request.id, exc)
                            errors.append(exc)

            if errors:
                raise RuntimeError(f"[fast] Campaign scrape failed for campaign {campaign.id}: {errors[0]}")

    def _process_campaign_slow(self, campaign: Campaign) -> None:
        logger.info(
            "Slow mode campaign dedupe enabled. Place pause range: %.2fs..%.2fs | scroll pause range: %.2fs..%.2fs | detail_workers=%d",
            self.slow_place_pause_min_s,
            self.slow_place_pause_max_s,
            self.scroll_pause_min_s,
            self.scroll_pause_max_s,
            self.detail_workers,
        )
        while not self._stop:
            reqs = self.api.get_requests_for_campaign_name(campaign.name, include_inuse=True)
            if not reqs:
                logger.info("No more requests; completing campaign.")
                self.api.complete_campaign(campaign.id)
                break

            pending: List[RequestItem] = []
            for request in reqs:
                if not request.req_text:
                    logger.warning(f"Empty req_text for request {request.id}; marking completed.")
                    self.api.set_request_status(request.id, "completed")
                    continue
                pending.append(request)

            if not pending:
                time.sleep(1.0)
                continue

            worker_count = self._request_workers_for_pending(len(pending))
            selected = pending[:worker_count]
            logger.info(
                "[slow] Request-level parallelism: workers=%d, selected_requests=%d, total_pending=%d, detail_workers_per_request=%d",
                worker_count,
                len(selected),
                len(pending),
                self.detail_workers,
            )
            for request in selected:
                self.api.set_request_status(request.id, "inuse")

            errors: List[Exception] = []
            if worker_count == 1:
                request = selected[0]
                try:
                    self._process_single_request_slow(campaign, request)
                except Exception as exc:
                    errors.append(exc)
            else:
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="maps-slow-req") as pool:
                    fut_to_req = {
                        pool.submit(self._process_single_request_slow, campaign, request): request
                        for request in selected
                    }
                    for fut in as_completed(fut_to_req):
                        request = fut_to_req[fut]
                        try:
                            fut.result()
                        except Exception as exc:
                            logger.error("[slow] Request %s failed: %s", request.id, exc)
                            errors.append(exc)

            if self._stop:
                logger.warning(
                    "[slow] Stop requested for campaign %s; keeping active requests in current state for takeover.",
                    campaign.id,
                )
                time.sleep(1.0)
                continue

            if errors:
                raise RuntimeError(f"[slow] Campaign scrape failed for campaign {campaign.id}: {errors[0]}")


def run_all(
    csv_dir: Optional[str] = None,
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    scrape_mode: Optional[str] = None,
    show_browser: Optional[bool] = None,
    slow_place_pause_min_s: Optional[float] = None,
    slow_place_pause_max_s: Optional[float] = None,
    scroll_pause_min_s: Optional[float] = None,
    scroll_pause_max_s: Optional[float] = None,
    detail_workers: Optional[int] = None,
    proxy_url: Optional[str] = None,
) -> None:
    api = LeadsApiClient(DEFAULT_BASE_URL, HttpClient())
    processor = CampaignProcessor(
        api,
        batch_size=BATCH_SIZE,
        csv_dir=csv_dir,
        scrape_mode=scrape_mode or DEFAULT_SCRAPE_MODE,
        show_browser=show_browser,
        slow_place_pause_min_s=slow_place_pause_min_s,
        slow_place_pause_max_s=slow_place_pause_max_s,
        scroll_pause_min_s=scroll_pause_min_s,
        scroll_pause_max_s=scroll_pause_max_s,
        detail_workers=detail_workers,
        proxy_url=proxy_url,
    )
    if campaign_id and campaign_name:
        processor.process_campaign(Campaign(id=campaign_id, name=campaign_name))
        return
    active = api.get_active_campaigns()
    if not active:
        logger.info("No active campaigns found. Exiting.")
        return
    for camp in active:
        if processor._stop:
            break
        processor.process_campaign(camp)


def _map_result_to_contact(result: Dict[str, Any]) -> Dict[str, Any]:
    import re as _re

    def _pick(d: Dict[str, Any], *keys, default=""):
        for k in keys:
            if k in d and d[k] not in (None, "", "N/A"):
                return d[k]
        return default

    def _rev_to_str(v: Any) -> str:
        s = str(v)
        m = _re.search(r"([\d,]+)", s)
        return m.group(1).replace(",", "") if m else ""

    website = _sanitize_domain_value(_pick(result, "Website", "Site", default=""))
    rating = _sanitize_rating_value(_pick(result, "Rating", default=""))

    return {
        "companyName": _pick(result, "Name", "Business Name", "Company"),
        "address": _pick(result, "Address"),
        "phone": _pick(result, "Phone", "Telephone"),
        "rating": rating,
        "reviews": _rev_to_str(_pick(result, "Reviews", default="")),
        "category": _pick(result, "Business Type", "Category"),
        "website": website,
        "url": _pick(result, "Url", "URL", "Maps URL", "MapUrl", default=""),
        "locatedIn": _pick(result, "Located In", "LocatedIn"),
        "hours": _pick(result, "Hours"),
        "hoursDetail": _pick(result, "Hours Detail", "HoursDetail"),
        "plusCode": _pick(result, "Plus Code", "PlusCode"),
        "bookingLink": _pick(result, "Booking Link", "BookingLink"),
        "email": "",
        "facebook": "",
        "instagram": "",
        "twitter": "",
        "yelp": "",
    }


async def _collect_place_urls_for_query(
    page,
    query: str,
    pause_min_s: Optional[float] = None,
    pause_max_s: Optional[float] = None,
) -> List[str]:
    import urllib.parse

    url = f"https://www.google.com/maps/search/{urllib.parse.quote_plus(query)}"
    logger.info(f"[slow] Collecting place URLs for query: {query}")
    scroll_pause_low, scroll_pause_high = normalize_scroll_pause_range(pause_min_s, pause_max_s)
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_selector('//div[@role="feed"]', timeout=60000)

    last = -1
    stable = 0
    for _ in range(60):
        await page.evaluate(
            """(sel) => {
                const el = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                if (!el) return;
                el.scrollBy(0, el.scrollHeight);
            }""",
            '//div[@role="feed"]',
        )
        scroll_pause = random.uniform(scroll_pause_low, scroll_pause_high)
        await page.wait_for_timeout(int(scroll_pause * 1000))
        count = await page.evaluate(
            """(sel) => {
                const el = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                if (!el) return 0;
                return el.querySelectorAll('a[href^="https://www.google.com/maps/place/"]').length;
            }""",
            '//div[@role="feed"]',
        )
        if count == last:
            stable += 1
        else:
            stable = 0
        last = count
        if stable >= 3:
            break

    urls = await page.evaluate(
        """(sel) => {
            const el = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if (!el) return [];
            const seen = new Set();
            const out = [];
            for (const a of el.querySelectorAll('a[href^="https://www.google.com/maps/place/"]')) {
                const href = (a.getAttribute('href') || '').trim();
                if (!href || seen.has(href)) continue;
                seen.add(href);
                out.push(href);
            }
            return out;
        }""",
        '//div[@role="feed"]',
    )
    if not isinstance(urls, list):
        return []
    cleaned = [str(v).strip() for v in urls if str(v).strip()]
    logger.info(f"[slow] Query '{query}' returned {len(cleaned)} place URLs")
    return cleaned


def run_campaign_slow_dedup_and_yield_batches(
    requests: List[RequestItem],
    batch_size: int,
    on_batch_for_request: Callable[[str, List[Dict[str, Any]]], None],
    show_browser: Optional[bool] = None,
    pause_min_s: Optional[float] = None,
    pause_max_s: Optional[float] = None,
    scroll_pause_min_s: Optional[float] = None,
    scroll_pause_max_s: Optional[float] = None,
    detail_workers: Optional[int] = None,
    proxy_url: Optional[str] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    import asyncio

    pause_low, pause_high = normalize_pause_range(pause_min_s, pause_max_s)
    scroll_pause_low, scroll_pause_high = normalize_scroll_pause_range(scroll_pause_min_s, scroll_pause_max_s)
    show = normalize_show_browser(show_browser)
    workers = normalize_detail_workers(detail_workers)
    normalized_proxy = normalize_maps_proxy_url(proxy_url)
    logger.info(
        "Slow campaign scrape: %d requests, place pause %.2fs..%.2fs, scroll pause %.2fs..%.2fs, show_browser=%s, detail_workers=%d",
        len(requests),
        pause_low,
        pause_high,
        scroll_pause_low,
        scroll_pause_high,
        show,
        workers,
    )
    if normalized_proxy:
        logger.info("Maps proxy enabled for slow mode browser runtime.")

    async def _run() -> bool:
        runtime = AsyncBrowserRuntime(
            headless=(not show),
            chromium_args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-extensions',
                '--disable-plugins',
                '--disable-images',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
            ],
            camoufox_options={"block_images": True},
            proxy_url=normalized_proxy,
        )
        browser = await runtime.launch()
        logger.info("Maps browser backend: %s", backend_display_name(None))
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/114.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            sent_any = False
            try:
                seen_urls: Set[str] = set()
                ordered_targets: List[Tuple[str, str]] = []

                for idx, request in enumerate(requests, 1):
                    if callable(should_stop) and should_stop():
                        logger.warning("[slow] Stop requested while collecting URLs; ending campaign scrape early.")
                        break
                    query = (request.req_text or "").strip()
                    if not query:
                        continue
                    try:
                        urls = await _collect_place_urls_for_query(
                            page,
                            query,
                            pause_min_s=scroll_pause_low,
                            pause_max_s=scroll_pause_high,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[slow] Request %s (%d/%d) failed during URL collection; skipping request. query=%r error=%s",
                            request.id,
                            idx,
                            len(requests),
                            query,
                            exc,
                        )
                        continue
                    added = 0
                    for url in urls:
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        ordered_targets.append((url, request.id))
                        added += 1
                    logger.info(
                        "[slow] Request %s (%d/%d): %d URLs, %d new, %d total unique",
                        request.id,
                        idx,
                        len(requests),
                        len(urls),
                        added,
                        len(ordered_targets),
                    )

                if not ordered_targets:
                    return False

                request_buffers: Dict[str, List[Dict[str, Any]]] = {}
                buffer_lock = asyncio.Lock()
                callback_lock = asyncio.Lock()
                target_iter = iter(enumerate(ordered_targets, 1))
                target_iter_lock = asyncio.Lock()

                async def _emit_batch(request_id: str, batch: List[Dict[str, Any]]) -> None:
                    nonlocal sent_any
                    if not batch:
                        return
                    async with callback_lock:
                        try:
                            await asyncio.to_thread(on_batch_for_request, request_id, batch)
                            sent_any = True
                        except Exception as exc:
                            logger.exception("[slow] Batch callback failed for request %s: %s", request_id, exc)

                async def _worker(worker_id: int) -> None:
                    worker_page = await context.new_page()
                    try:
                        while True:
                            if callable(should_stop) and should_stop():
                                logger.warning("[slow][worker-%d] Stop requested; exiting worker loop.", worker_id)
                                break
                            async with target_iter_lock:
                                try:
                                    idx, (place_url, request_id) = next(target_iter)
                                except StopIteration:
                                    break
                            to_emit: Optional[List[Dict[str, Any]]] = None
                            try:
                                await worker_page.goto(place_url, wait_until="domcontentloaded", timeout=60000)
                                await worker_page.wait_for_selector("h1, [data-item-id='address'], [data-item-id='authority']", timeout=20000)
                                detail = await extract_place_details(worker_page)
                                if detail:
                                    contact = _map_result_to_contact(detail)
                                    async with buffer_lock:
                                        request_buffers.setdefault(request_id, []).append(contact)
                                        if len(request_buffers[request_id]) >= batch_size:
                                            to_emit = list(request_buffers[request_id])
                                            request_buffers[request_id] = []
                            except Exception as exc:
                                logger.warning(
                                    "[slow][worker-%d] Failed place %d/%d (%s): %s",
                                    worker_id,
                                    idx,
                                    len(ordered_targets),
                                    place_url,
                                    exc,
                                )

                            pause = random.uniform(pause_low, pause_high)
                            logger.info(
                                "[slow][worker-%d] Scraped %d/%d, pausing %.2fs",
                                worker_id,
                                idx,
                                len(ordered_targets),
                                pause,
                            )
                            await worker_page.wait_for_timeout(int(pause * 1000))

                            if to_emit:
                                await _emit_batch(request_id, to_emit)
                    finally:
                        await worker_page.close()

                worker_tasks = [asyncio.create_task(_worker(i + 1)) for i in range(workers)]
                await asyncio.gather(*worker_tasks)

                for request_id, pending in request_buffers.items():
                    if not pending:
                        continue
                    await _emit_batch(request_id, list(pending))
                return sent_any
            finally:
                try:
                    await page.screenshot(path="screenshot.png")
                except Exception:
                    pass
        finally:
            await runtime.close()

    return asyncio.run(_run())


def run_scrape_and_yield_batches(
    query: str,
    batch_size: int,
    on_batch: Callable[[List[Dict[str, Any]]], None],
    scrape_mode: Optional[str] = None,
    show_browser: Optional[bool] = None,
    scroll_pause_min_s: Optional[float] = None,
    scroll_pause_max_s: Optional[float] = None,
    proxy_url: Optional[str] = None,
) -> bool:
    import asyncio
    import urllib.parse

    mode = normalize_scrape_mode(scrape_mode or DEFAULT_SCRAPE_MODE)
    show = normalize_show_browser(show_browser)
    normalized_proxy = normalize_maps_proxy_url(proxy_url)
    scroll_pause_low, scroll_pause_high = normalize_scroll_pause_range(scroll_pause_min_s, scroll_pause_max_s)
    logger.info(
        "Maps scrape mode: %s | show_browser=%s | scroll pause range: %.2fs..%.2fs",
        mode,
        show,
        scroll_pause_low,
        scroll_pause_high,
    )
    if normalized_proxy:
        logger.info("Maps proxy enabled for fast mode browser runtime.")

    async def _run() -> List[Dict[str, Any]]:
        runtime = AsyncBrowserRuntime(
            headless=(not show),
            chromium_args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-extensions',
                '--disable-plugins',
                '--disable-images',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
            ],
            camoufox_options={"block_images": True},
            proxy_url=normalized_proxy,
        )
        browser = await runtime.launch()
        logger.info("Maps browser backend: %s", backend_display_name(None))
        try:
            context = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/114.0.0.0 Safari/537.36"
            ))
            page = await context.new_page()
            try:
                url = f"https://www.google.com/maps/search/{urllib.parse.quote_plus(query)}"
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_selector('//div[@role="feed"]', timeout=60000)
                last = -1; stable = 0
                for _ in range(60):
                    await page.evaluate("""(sel) => {
                        const el = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                        if (!el) return;
                        el.scrollBy(0, el.scrollHeight);
                    }""", '//div[@role="feed"]')
                    scroll_pause = random.uniform(scroll_pause_low, scroll_pause_high)
                    await page.wait_for_timeout(int(scroll_pause * 1000))
                    count = await page.evaluate("""(sel) => {
                        const el = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                        if (!el) return 0;
                        return el.querySelectorAll('a[href^="https://www.google.com/maps/place/"]').length;
                    }""", '//div[@role="feed"]')
                    if count == last: stable += 1
                    else: stable = 0
                    last = count
                    if stable >= 3: break
                results = await scrape_business_info(page, browser, max_concurrent=MAX_CONCURRENT)
                if mode == "slow":
                    results = await enrich_businesses_with_place_pages(page, results)
                try: await page.screenshot(path="screenshot.png")
                except Exception: pass
                return results or []
            except Exception as e:
                logger.error(f"[Adapter] Browser error: {e}")
                return []
        finally:
            await runtime.close()

    results: List[Dict[str, Any]] = asyncio.run(_run())
    if not results:
        return False

    batch: List[Dict[str, Any]] = []; sent_any = False
    for r in results:
        contact = _map_result_to_contact(r)
        batch.append(contact)
        if len(batch) >= batch_size:
            on_batch(batch); sent_any = True; batch = []
    if batch: on_batch(batch); sent_any = True
    return sent_any

# === END: API Orchestration ===

if __name__ == "__main__":
    run_all(csv_dir=None, campaign_id=None, campaign_name=None)
