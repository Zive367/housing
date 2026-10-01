"""Turn alert emails into candidate listing URLs."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .mail import Email

log = logging.getLogger(__name__)

# Detail-page URL shapes on the big Dutch platforms.
LISTING_PATTERNS = [
    re.compile(r"funda\.nl/(?:en/)?(?:detail/)?huur/[^/]+/[^/]+"),
    re.compile(r"pararius\.(?:nl|com)/[a-z-]+-(?:te-huur|for-rent)/[^/]+/[0-9a-f]{6,}"),
    re.compile(r"huurwoningen\.nl/(?:huren|in)/[^/]+/[0-9a-f]{6,}"),
    re.compile(r"kamernet\.nl/(?:en/)?(?:huren|for-rent)/[^/]+/[^/]+/[^/]*\d{4,}"),
    re.compile(r"housinganywhere\.com/room/[a-z]*\d+"),
]

# Aggregators link to their own pages or redirectors; we follow those to the original listing.
AGGREGATOR_DOMAINS = ("stekkies.com", "stekkies.nl", "findify.nl", "uprent.nl", "rentbird.nl", "rentslam.com")

SKIP_LINK = re.compile(
    r"unsubscribe|afmelden|uitschrijven|preferences|voorkeuren|instellingen|settings|account|login|inloggen|"
    r"privacy|terms|voorwaarden|cookie|help|support|faq|contact-us|apps\.apple|play\.google|facebook\.com|"
    r"instagram\.com|linkedin\.com|twitter\.com|x\.com/|youtube\.com|tiktok\.com|mailto:|tel:|"
    r"\.(?:png|jpe?g|gif|svg|webp)(?:\?|$)", re.I)

TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|source|campaign|medium|_hs.*|trk.*)$", re.I)
REPLY_HINTS = re.compile(
    r"bericht|reactie|bezichtiging|viewing|message|reply|uitnodiging|afspraak|appointment|invitation", re.I)


@dataclass
class Candidate:
    url: str
    snippet: str


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path) or "/"
    if host == "funda.nl":
        path = path.removeprefix("/en")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    return urlunsplit(("https", host, path, query, ""))


def listing_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12]


def domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def looks_like_listing(url: str) -> bool:
    if any(p.search(url) for p in LISTING_PATTERNS):
        return True
    parts = urlsplit(url)
    if not parts.netloc or SKIP_LINK.search(url):
        return False
    segments = [s for s in parts.path.split("/") if s]
    # Agency sites: detail pages have a slug and usually an id, e.g. /aanbod/huur/den-haag/straat-12/123456
    has_id = bool(re.search(r"\d{3,}|[0-9a-f]{8,}", parts.path + parts.query))
    rental_words = bool(re.search(r"huur|rent|aanbod|woning|apartment|appartement|object|listing|property", url, re.I))
    return len(segments) >= 2 and has_id and rental_words


def is_alert(mail: Email, alert_domains: list[str]) -> bool:
    domain = mail.sender_domain
    return any(domain == d or domain.endswith("." + d) for d in alert_domains)


def might_be_message(mail: Email) -> bool:
    """Platforms also email you when an agent replies; those must go to reply triage too."""
    return bool(REPLY_HINTS.search(mail.subject))


def snippets_by_link(html: str) -> dict[str, str]:
    """For each link, the text of the block around it (price, address, size in alert emails)."""
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        node, text = a, a.get_text(" ", strip=True)
        for _ in range(5):
            if len(text) > 80 or node.parent is None:
                break
            node = node.parent
            text = node.get_text(" ", strip=True)
        out.setdefault(a["href"].strip(), text[:600])
    return out


def resolve(url: str, client: httpx.Client) -> str:
    """Follow redirect/tracking links to the final URL without downloading the page."""
    try:
        with client.stream("GET", url, follow_redirects=True, timeout=15) as response:
            return str(response.url)
    except httpx.HTTPError as e:
        log.debug("could not resolve %s: %s", url, e)
        return url


def extract_candidates(mail: Email, client: httpx.Client, max_links: int = 15) -> list[Candidate]:
    snippets = snippets_by_link(mail.html)
    seen: set[str] = set()
    out: list[Candidate] = []
    for link in mail.links:
        if len(out) >= max_links:
            break
        if SKIP_LINK.search(link):
            continue
        url = link
        if not looks_like_listing(url):
            # Tracking redirects and aggregator short links only reveal the target after resolving.
            if not (domain_of(url).endswith(AGGREGATOR_DOMAINS) or "click" in url or "track" in url
                    or "redirect" in url or "/r/" in url or "/l/" in url):
                continue
            url = resolve(url, client)
            if not (looks_like_listing(url) or domain_of(url).endswith(AGGREGATOR_DOMAINS)):
                continue
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(Candidate(url=url, snippet=snippets.get(link, "")))
    return out
