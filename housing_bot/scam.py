"""Scam risk: hard rules first (cheap, explainable), then Claude's judgement on top."""

from __future__ import annotations

import logging
import re

from .config import Config
from .llm import LLM, BudgetExceeded, LLMError
from .models import Listing, ScamVerdict

log = logging.getLogger(__name__)

# (pattern, points, reason). Matched against the page text, the alert text and Claude's red-flag quotes.
RED_FLAGS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(p, re.I), pts, why) for p, pts, why in [
        (r"western union|moneygram|bitcoin|crypto(?:currency)?|gift ?cards?", 40, "Asks for untraceable payment"),
        (r"\bairbnb\b.{0,60}\b(?:pay|payment|betal)|\bescrow\b", 35, "Payment via Airbnb/escrow (classic scam)"),
        (r"(?:keys?|sleutels?)\s+(?:will\s+be\s+|worden\s+|can\s+be\s+)?(?:sent|send|mailed|posted|shipped|"
         r"verstuurd|opgestuurd|toegestuurd)", 35, "Keys sent by post"),
        # Paying before key handover is normal in NL (after signing). Paying before a viewing is not.
        (r"(?:pay|transfer|wire|betaal|betalen|overmaken|storten).{0,50}(?:before|prior to|vooraf(?:gaand)? aan|"
         r"voordat)\s.{0,30}(?:viewing|bezichtiging|visit|we meet|ontmoeten)", 35, "Wants payment before a viewing"),
        (r"no viewings?|viewing (?:is )?not possible|cannot show|geen bezichtiging|bezichtiging (?:is )?niet mogelijk",
         30, "No viewing possible"),
        (r"\b(?:i am|i'm|we are|currently|momenteel|ik ben|wij zijn)\b.{0,30}\b(?:abroad|overseas|in the uk|"
         r"in england|in spain|in france|in germany|in nigeria|buitenland)\b", 25, "Landlord says they are abroad"),
        (r"god bless|missionar|my late (?:husband|wife)|passed away|overleden", 20, "Emotional story typical of scams"),
        (r"(?:deposit|borg|first month|eerste maand).{0,40}(?:reserve|reserveren|secure|vastleggen|hold the)",
         20, "Pay to reserve the home"),
        (r"(?:whatsapp|telegram)\s+only|only\s+(?:via|on|through)\s+(?:whatsapp|telegram)|alleen\s+(?:via\s+)?whatsapp",
         15, "Contact only via WhatsApp/Telegram"),
        (r"(?:copy|kopie|scan|photo|foto)\s+(?:of\s+)?(?:your\s+|uw\s+|je\s+)?(?:passport|paspoort|id|"
         r"identiteitsbewijs)", 15, "Asks for an ID copy up front"),
    ]
]
CLASSIFIEDS = ("marktplaats.nl", "facebook.com", "2dehands.be", "kijiji", "craigslist")
FREE_MAIL = re.compile(r"@(gmail|hotmail|outlook|live|yahoo|icloud|aol|gmx|proton(mail)?|mail)\.", re.I)

SYSTEM = """You judge whether a Dutch rental listing is a scam. Rental scams are common in the Netherlands.

Typical scams: price far below market for the area; landlord "abroad"; deposit or first month before a viewing;
keys by post; payment via Airbnb/escrow/Western Union/crypto; WhatsApp-only contact; ID copy before a viewing;
copied photos/text; pressure to decide fast; address that doesn't exist.
Typical legitimate signs: an established letting agency or institutional landlord with a real office and website,
a viewing offered before any payment, a market-level price, a verifiable address, a contract signed after viewing.

Score: 0-20 normal listing, 21-39 minor doubts, 40-69 suspicious (verify before any viewing), 70-100 likely scam.
Weigh the rule-based signals you're given, but they can be false positives (e.g. "deposit" mentioned normally).
Reasons must be short and specific."""


def rule_signals(cfg: Config, listing: Listing, page_text: str) -> list[tuple[int, str]]:
    d = listing.details
    signals: list[tuple[int, str]] = []
    haystack = "\n".join([page_text[:20000], listing.alert_text, *(d.red_flag_quotes if d else [])])
    for pattern, points, reason in RED_FLAGS:
        if pattern.search(haystack):
            signals.append((points, reason))

    if listing.total_rent and d and d.size_m2:
        expected = cfg.scam.expected_price_per_m2.get(listing.municipality,
                                                      cfg.scam.expected_price_per_m2.get("default", 20))
        ratio = listing.total_rent / d.size_m2 / expected
        if ratio < 0.55:
            signals.append((35, f"Price far below market (€{listing.total_rent / d.size_m2:.0f}/m² vs ~€{expected:.0f})"))
        elif ratio < 0.7:
            signals.append((15, f"Price below market (€{listing.total_rent / d.size_m2:.0f}/m² vs ~€{expected:.0f})"))

    if listing.address_verified is False:
        if d and d.house_number:
            signals.append((30, "Address does not exist in the national address register (BAG)"))
        else:
            signals.append((25, "Street not found in that city"))

    foreign = [p for p in ([listing.phone] if listing.phone else []) + (d.agent_phones if d else [])
               if p.startswith("+") and not p.startswith("+31")]
    if foreign:
        signals.append((15, f"Foreign phone number ({foreign[0][:4]}…)"))

    trusted = any(listing.domain == t or listing.domain.endswith("." + t) for t in cfg.scam.trusted_domains)
    if any(c in listing.domain for c in CLASSIFIEDS):
        signals.append((15, "Classifieds site (high scam rate)"))
    if d and d.landlord_type == "private" and not trusted and any(FREE_MAIL.search(e) for e in d.agent_emails):
        signals.append((10, "Private landlord with a free email address"))
    return signals


def assess(cfg: Config, llm: LLM | None, listing: Listing, page_text: str) -> ScamVerdict:
    signals = rule_signals(cfg, listing, page_text)
    rule_score = min(100, sum(points for points, _ in signals))
    reasons = [reason for _, reason in signals]
    if llm is None or rule_score >= 90:
        return ScamVerdict(score=rule_score, reasons=reasons)

    d = listing.details
    facts = {
        "url": listing.url, "address": listing.address, "address_in_register": listing.address_verified,
        "municipality": listing.municipality, "total_rent_eur": listing.total_rent,
        "size_m2": d.size_m2 if d else None, "landlord_type": d.landlord_type if d else None,
        "agency": d.agency_name if d else None, "phones": d.agent_phones if d else [],
        "emails": d.agent_emails if d else [], "application_method": d.application_method if d else None,
        "red_flag_quotes": d.red_flag_quotes if d else [], "rule_signals": reasons,
    }
    prompt = f"Listing facts: {facts}\n\nListing text (excerpt):\n{(page_text or listing.alert_text)[:8000]}"
    try:
        verdict = llm.parse(ScamVerdict, SYSTEM, prompt, effort="low", max_tokens=4000)
    except (LLMError, BudgetExceeded) as e:
        log.warning("scam LLM check skipped: %s", e)
        return ScamVerdict(score=rule_score, reasons=reasons)
    # Conservative: the higher of the two scores wins.
    score = max(rule_score, max(0, min(100, verdict.score)))
    merged = reasons + [r for r in verdict.reasons if r not in reasons]
    return ScamVerdict(score=score, reasons=merged[:6])
