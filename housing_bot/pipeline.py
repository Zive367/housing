"""The end-to-end flow for one listing, and for one reply."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import apply, bandit
from .compose import compose
from .config import DATA_DIR, Config
from .decide import decide, rank, total_rent
from .extract import extract
from .fetch import fetch, normalize_phone
from .geo import bike_minutes, geocode_address, haversine_km, locate, neighbourhood_score, walk_minutes
from .ingest import LISTING_PATTERNS, canonical_url, domain_of, listing_id
from .llm import LLM, BudgetExceeded, LLMError
from .mail import Email, Mailbox
from .models import POSITIVE, Listing, Status
from .notify import Notifier
from .replies import address_key, analyze, match_listing, next_status
from .scam import assess
from .sheet import Sheet
from .store import Store

log = logging.getLogger(__name__)
PLATFORM_EMAIL = ("funda.nl", "pararius.nl", "pararius.com", "huurwoningen.nl", "kamernet.nl")
# The same home often arrives from two sites within seconds; claim its address atomically.
_address_claim = threading.Lock()


@dataclass
class Context:
    cfg: Config
    store: Store
    llm: LLM | None
    mailbox: Mailbox | None
    sheet: Sheet | None
    notifier: Notifier
    http: httpx.Client

    def mirror(self, listing: Listing) -> None:
        self.store.save(listing)
        if self.sheet:
            try:
                self.sheet.upsert(listing)
            except Exception as e:  # gspread raises several unrelated types; the DB stays the source of truth
                log.warning("sheet update failed for %s: %s", listing.id, e)

    def work_location(self) -> tuple[float, float] | None:
        w = self.cfg.work
        if w.lat is not None and w.lon is not None:
            return w.lat, w.lon
        cached = self.store.kv_get(f"work:{w.address}")
        if cached:
            lat, lon = cached.split(",")
            return float(lat), float(lon)
        found = geocode_address(self.http, w.address)
        if found:
            self.store.kv_set(f"work:{w.address}", f"{found[0]},{found[1]}")
        return found


def build_context(cfg: Config, with_sheet: bool = True) -> Context:
    store = Store(DATA_DIR / "housing.db")
    s = cfg.secrets
    mailbox = Mailbox(cfg, store) if s.bot_email and s.bot_email_app_password else None
    llm = LLM(cfg, store) if s.anthropic_api_key else None
    sheet = None
    if with_sheet and s.google_service_account_file and s.google_sheet_id:
        sheet = Sheet(s.google_service_account_file, s.google_sheet_id)
    http = httpx.Client(timeout=25, follow_redirects=True)
    return Context(cfg, store, llm, mailbox, sheet, Notifier(cfg, mailbox), http)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("Europe/Amsterdam")).strftime("%d %b %H:%M")


def process_candidate(ctx: Context, url: str, source: str, snippet: str = "", force: bool = False) -> Listing | None:
    listing = Listing(id=listing_id(url), url=url, source=source, domain=domain_of(url), alert_text=snippet)
    if not ctx.store.insert_if_new(listing):
        if not force:
            return None
        listing = ctx.store.get(listing.id) or listing
    try:
        _process(ctx, listing)
    except (LLMError, BudgetExceeded) as e:
        listing.status, listing.notes = Status.FAILED, f"Claude unavailable: {e}"
        ctx.mirror(listing)
        ctx.notifier.send("Listing needs a manual look", f"{listing.address or listing.url}: {e}", listing.url)
    except Exception as e:  # keep the bot alive; the listing shows up as FAILED with the reason
        log.exception("processing %s failed", url)
        listing.status, listing.notes = Status.FAILED, f"{type(e).__name__}: {e}"[:300]
        ctx.mirror(listing)
    return listing


def _process(ctx: Context, listing: Listing) -> None:
    cfg, store = ctx.cfg, ctx.store
    page = fetch(listing.url, ctx.http)
    if canonical_url(page.url) != canonical_url(listing.url):
        final_id = listing_id(page.url)
        if final_id != listing.id and store.get(final_id):
            listing.status, listing.duplicate_of = Status.DUPLICATE, final_id
            store.save(listing)
            return
        listing.url, listing.domain = page.url, domain_of(page.url)
    listing.page_blocked = page.blocked
    if ctx.llm is None:
        raise LLMError("no ANTHROPIC_API_KEY configured")

    d = extract(ctx.llm, page, listing.alert_text)
    listing.details = d
    if not d.is_rental_listing:
        if page.blocked and any(p.search(listing.url) for p in LISTING_PATTERNS):
            # A real listing we couldn't read (bot protection) and the alert had too little text.
            listing.status, listing.apply_error = Status.MANUAL_APPLY, "Page blocked by bot protection: check it yourself"
            ctx.mirror(listing)
            ctx.notifier.send("Couldn't read a new listing", "Bot protection blocked the page. Have a quick look.",
                              listing.url)
            return
        # A navigation or overview link, not a home: remember it, but keep it out of the sheet.
        listing.status, listing.notes = Status.UNAVAILABLE, "not a rental listing"
        store.save(listing)
        return
    if d.still_available is False:
        listing.status, listing.filter_reasons = Status.UNAVAILABLE, ["Already rented / under option"]
        ctx.mirror(listing)
        return

    # Where is it, does it exist, how far from work, what's the area like?
    place = locate(ctx.http, d.street, d.house_number, d.postcode, d.city)
    if place:
        listing.address = place.display if place.kind in ("adres", "weg") else (place.display or d.title or "")
        listing.municipality, listing.buurt, listing.buurt_code = place.municipality, place.buurt, place.buurt_code
        listing.lat, listing.lon, listing.address_verified = place.lat, place.lon, place.verified
        if place.verified:
            listing.address_key = address_key(place.postcode, place.number)
        work = ctx.work_location()
        if work and place.lat is not None:
            listing.distance_km = round(haversine_km(work[0], work[1], place.lat, place.lon), 2)
            listing.bike_minutes = bike_minutes(listing.distance_km)
            listing.walk_minutes = walk_minutes(listing.distance_km)
        listing.neighbourhood_score = neighbourhood_score(ctx.http, store, cfg.neighbourhood.cbs_table,
                                                          place.buurt_code, place.wijk_code)
    else:
        listing.address = " ".join(x for x in [d.street, d.house_number, d.city] if x) or (d.title or "")

    # Same home seen before via another site?
    with _address_claim:
        other = next((o for o in store.by_address_key(listing.address_key)
                      if o.id != listing.id and o.status != Status.DUPLICATE), None)
        if other:
            listing.status, listing.duplicate_of = Status.DUPLICATE, other.id
        store.save(listing)  # saving the address key claims it for this listing
    if other:
        return

    listing.agency, listing.agent = d.agency_name or "", d.agent_name or ""
    phones = [p for p in (normalize_phone(x) for x in d.agent_phones + page.phones) if p]
    listing.phone = phones[0] if phones else ""
    emails = [e for e in d.agent_emails if not e.endswith(PLATFORM_EMAIL)] or d.agent_emails
    listing.email = emails[0] if emails else ""
    listing.total_rent = total_rent(d)

    decision = decide(cfg, listing)
    listing.apply_as, listing.apply_reason, listing.permit_note = decision.apply_as, decision.apply_reason, \
        decision.permit_note
    if not decision.qualifies:
        listing.status, listing.filter_reasons = Status.FILTERED, decision.reasons
        listing.rank = rank(cfg, listing)
        ctx.mirror(listing)
        return

    verdict = assess(cfg, ctx.llm, listing, listing.alert_text if page.blocked else page.text)
    listing.scam_score, listing.scam_reasons = verdict.score, verdict.reasons
    listing.rank = rank(cfg, listing)
    if verdict.score >= cfg.scam.block_score:
        listing.status = Status.LIKELY_SCAM
        ctx.mirror(listing)
        return

    languages = ["en"] if d.language == "en" else cfg.apply.languages
    variant = bandit.choose(store, languages)
    composed = compose(cfg, ctx.llm, listing, variant)
    listing.variant, listing.message = variant["name"], composed.message
    ctx.mirror(listing)  # visible in the sheet while the application is being sent

    outcome = apply.send(cfg, store, ctx.llm, ctx.mailbox, listing, composed.subject, composed.message)
    listing.status, listing.apply_method = outcome.status, outcome.method
    listing.call_needed, listing.call_reason = outcome.call_needed, outcome.call_reason
    if outcome.status == Status.APPLIED:
        listing.notes = outcome.detail
        listing.applied_at = time.time()
        listing.time_to_apply_s = listing.applied_at - listing.first_seen
    else:
        listing.apply_error = outcome.detail
    ctx.mirror(listing)

    where = f"{listing.address} · €{listing.total_rent or '?'} · {listing.bike_minutes or '?'} min by bike"
    if listing.status == Status.MANUAL_APPLY:
        # Laid out for a phone: tap the link, copy the message, done.
        call = f"\nOr call: {listing.phone}" if listing.call_needed else ""
        rent = f" €{listing.total_rent:,.0f}" if listing.total_rent else ""
        ctx.notifier.send(f"Apply: {listing.address or listing.domain}{rent}",
                          f"Open: {listing.url}{call}\n\nMessage to paste:\n\n{listing.message}\n\n"
                          f"---\n{where}\nWhy you: {outcome.detail}", important=True)
    elif listing.status == Status.APPLIED and outcome.detail.startswith("⚠"):
        ctx.notifier.send("Check this application", f"{where}. {outcome.detail[2:]}. If it didn't go through, "
                          f"apply yourself with this message:\n\n{listing.message}", listing.url)
    elif listing.status == Status.APPLIED and verdict.score >= cfg.scam.warn_score:
        ctx.notifier.send("Applied, but verify first",
                          f"{where}. Scam risk {verdict.score}/100: {'; '.join(verdict.reasons[:3])}. "
                          "Never pay before a viewing and a signed contract.", listing.url)


def record_outcome(store: Store, listing: Listing, won: bool) -> None:
    if listing.outcome or not listing.variant or not listing.applied_at:
        return
    listing.outcome = "win" if won else "loss"
    store.record_outcome(listing.variant, won)


def process_reply(ctx: Context, mail: Email) -> None:
    if ctx.llm is None:
        return
    try:
        analysis = analyze(ctx.llm, mail)
    except (LLMError, BudgetExceeded) as e:
        ctx.notifier.send("Couldn't read an email", f"From {mail.sender}: {mail.subject} ({e})", important=True)
        return
    if analysis.category in ("unrelated", "new_listing_alert"):
        return
    listing = match_listing(ctx.store, mail, analysis)
    ctx.store.log(listing.id if listing else None, "reply", category=analysis.category, sender=mail.sender,
                  subject=mail.subject)

    if listing:
        new = next_status(listing.status, analysis.category)
        if new:
            listing.status = new
        listing.last_reply = f"{_when(mail.date)} {analysis.category}: {analysis.summary}"[:500]
        if analysis.viewing_datetime:
            listing.viewing_at = analysis.viewing_datetime
        if analysis.booking_link:
            listing.viewing_link = analysis.booking_link
        if analysis.agent_phone and not listing.phone:
            listing.phone = normalize_phone(analysis.agent_phone) or listing.phone
        if listing.status in POSITIVE:
            record_outcome(ctx.store, listing, won=True)
        elif listing.status == Status.REJECTED:
            record_outcome(ctx.store, listing, won=False)
        ctx.mirror(listing)

    where = listing.address if listing else (analysis.property_address or mail.subject)
    link = analysis.booking_link or (listing.url if listing else "")
    agent_phone = (listing.phone if listing else "") or analysis.agent_phone or ""
    contact = " · ".join(x for x in [analysis.agent_name, agent_phone] if x)
    contact = f" · {contact}" if contact else ""
    if analysis.category == "viewing_confirmed":
        ctx.notifier.send("🚩 VIEWING BOOKED", f"{where} · {analysis.viewing_datetime or 'see email'}{contact}",
                          link, important=True)
    elif analysis.category == "viewing_invitation":
        ctx.notifier.send("Book the viewing NOW", f"{where}: {analysis.summary}{contact}", link, important=True)
    elif analysis.category == "documents_requested":
        ctx.notifier.send("Agent asks for documents", f"{where}: {analysis.summary} (send them yourself)", link,
                          important=True)
    elif analysis.category in ("question", "platform_notification", "viewing_cancelled"):
        ctx.notifier.send("Reply needed", f"{where}: {analysis.summary}", link, important=True)
