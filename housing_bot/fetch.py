"""Download a listing page and pull out text, contact details and structured data."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import phonenumbers
from bs4 import BeautifulSoup

from .config import SECRETS_DIR
from .ingest import AGGREGATOR_DOMAINS, domain_of, looks_like_listing

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}
BLOCK_MARKERS = re.compile(
    r"just a moment|cf-browser-verification|challenge-platform|captcha|datadome|access denied|"
    r"are you a robot|ben je een robot|geen robot|echte mensen zijn|unusual traffic|request blocked", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+|00)?\d[\d\s().-]{7,16}\d")
IGNORED_EMAIL = re.compile(r"noreply|no-reply|privacy|example\.|sentry|\.png$|\.jpg$|wixpress", re.I)
MAX_TEXT = 24000

_browser_slots = threading.Semaphore(2)


@dataclass
class Page:
    url: str
    status: int = 0
    title: str = ""
    text: str = ""
    blocked: bool = False
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    jsonld: list = field(default_factory=list)


def session_file(domain: str) -> Path:
    return SECRETS_DIR / "sessions" / f"{domain}.json"


def normalize_phone(raw: str) -> str | None:
    try:
        number = phonenumbers.parse(raw, "NL")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def parse_html(url: str, html: str, status: int = 200) -> Page:
    soup = BeautifulSoup(html, "lxml")
    jsonld = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            jsonld.append(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            pass
    links = [a["href"] for a in soup.find_all("a", href=True)]
    tel_links = [h[4:] for h in links if h.lower().startswith("tel:")]
    mail_links = [h[7:].split("?")[0] for h in links if h.lower().startswith("mailto:")]
    title = soup.title.get_text(strip=True) if soup.title else ""

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        tag.decompose()
    text = re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n")).strip()

    phones: list[str] = []
    for raw in tel_links + PHONE_RE.findall(text):
        normalized = normalize_phone(raw)
        if normalized and normalized not in phones:
            phones.append(normalized)
    emails: list[str] = []
    for raw in mail_links + EMAIL_RE.findall(text):
        address = raw.strip().lower()
        if not IGNORED_EMAIL.search(address) and address not in emails:
            emails.append(address)

    blocked = status in (401, 403, 429, 503) or bool(BLOCK_MARKERS.search(text[:3000])) or len(text) < 300
    absolute = [str(httpx.URL(url).join(h)) for h in links if h.startswith(("http", "/"))]
    return Page(url=url, status=status, title=title, text=text[:MAX_TEXT], blocked=blocked,
                phones=phones[:5], emails=emails[:5], links=absolute, jsonld=jsonld[:5])


def fetch_http(url: str, client: httpx.Client) -> Page:
    try:
        response = client.get(url, headers=HEADERS, follow_redirects=True, timeout=25)
    except httpx.HTTPError as e:
        log.info("http fetch failed for %s: %s", url, e)
        return Page(url=url, blocked=True)
    return parse_html(str(response.url), response.text, response.status_code)


def fetch_browser(url: str) -> Page:
    """Real Chromium, with your saved login for that site if you created one (see `login` command)."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with _browser_slots, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            state = session_file(domain_of(url))
            context = browser.new_context(
                storage_state=str(state) if state.exists() else None, locale="nl-NL",
                timezone_id="Europe/Amsterdam", user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            return parse_html(page.url, page.content(), response.status if response else 0)
        except PlaywrightError as e:
            log.info("browser fetch failed for %s: %s", url, e)
            return Page(url=url, blocked=True)
        finally:
            browser.close()


def fetch(url: str, client: httpx.Client, use_browser: bool = True) -> Page:
    page = fetch_http(url, client)
    if page.blocked and use_browser:
        page = fetch_browser(url)
    # Aggregator pages: jump to the original listing they point to.
    if domain_of(page.url).endswith(AGGREGATOR_DOMAINS):
        original = next((link for link in page.links
                         if not domain_of(link).endswith(AGGREGATOR_DOMAINS) and looks_like_listing(link)), None)
        if original:
            return fetch(original, client, use_browser)
    return page
