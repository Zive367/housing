"""Choose how to apply (email, the site's form, or hand it to you) and do it."""

from __future__ import annotations

import logging
import smtplib
import time
from dataclasses import dataclass

from . import browser_agent
from .config import Config
from .llm import LLM
from .mail import Mailbox
from .models import Listing, Status
from .store import Store

log = logging.getLogger(__name__)

PLATFORM_DOMAINS = ("funda.nl", "pararius.nl", "pararius.com", "huurwoningen.nl", "kamernet.nl")


@dataclass
class Outcome:
    status: Status
    method: str
    detail: str = ""
    call_needed: bool = False
    call_reason: str = ""


def channels(cfg: Config, listing: Listing) -> list[str]:
    """Ordered list of ways to try. The last resort is always handing it to you."""
    d = listing.details
    domain = listing.domain
    if any(domain == m or domain.endswith("." + m) for m in cfg.apply.manual_only_domains) \
            or d.application_method == "booking_payment":
        return ["manual"]
    emails = [e for e in d.agent_emails if not e.endswith(PLATFORM_DOMAINS)]
    order: list[str] = []
    wants_form = d.application_method in ("web_form", "viewing_planner", "platform_message")
    if wants_form and cfg.apply.browser_forms:
        order.append("form")
    if emails:
        order.append("email")
    if d.application_method == "unknown" and cfg.apply.browser_forms and "form" not in order:
        order.append("form")
    order.append("manual")
    return order


def _agency_key(listing: Listing) -> str:
    d = listing.details
    return ((d.agency_name if d and d.agency_name else "") or listing.domain).strip().lower()


def over_limits(cfg: Config, store: Store, listing: Listing) -> str:
    now = time.time()
    if store.count_events("applied", now - 3600) >= cfg.apply.max_per_hour:
        return f"hourly cap of {cfg.apply.max_per_hour} applications reached"
    if store.count_events("applied", now - 86400, agency=_agency_key(listing)) >= cfg.apply.max_per_agency_per_day:
        return f"daily cap of {cfg.apply.max_per_agency_per_day} applications to this agency reached"
    return ""


def send(cfg: Config, store: Store, llm: LLM | None, mailbox: Mailbox | None, listing: Listing,
         subject: str, message: str) -> Outcome:
    d = listing.details
    phone_hint = listing.phone or (d.agent_phones[0] if d and d.agent_phones else "")
    if cfg.apply.dry_run:
        return Outcome(Status.READY, channels(cfg, listing)[0], "dry run: message written, not sent")
    limit = over_limits(cfg, store, listing)
    if limit:
        return Outcome(Status.MANUAL_APPLY, "manual", limit)

    notes: list[str] = []
    for channel in channels(cfg, listing):
        if channel == "email" and mailbox is not None:
            to = next(e for e in d.agent_emails if not e.endswith(PLATFORM_DOMAINS))
            try:
                full_name = f"{cfg.applicant.first_name} {cfg.applicant.last_name}".strip()
                listing.sent_message_id = mailbox.send(to, subject, message, from_name=full_name)
                listing.email = to
                store.log(listing.id, "applied", method="email", agency=_agency_key(listing), to=to)
                return Outcome(Status.APPLIED, "email", f"emailed {to}")
            except (smtplib.SMTPException, OSError) as e:  # try the next channel
                notes.append(f"email failed: {e}")
        elif channel == "form" and llm is not None:
            result = browser_agent.send_via_form(cfg, llm, listing, message)
            if result.outcome in ("submitted", "unconfirmed"):
                store.log(listing.id, "applied", method="form", agency=_agency_key(listing), detail=result.detail)
                detail = result.detail if result.outcome == "submitted" else f"⚠ {result.detail}"
                return Outcome(Status.APPLIED, "form", detail)
            hint = " (log in once with the `login` command)" if result.outcome == "needs_login" else ""
            notes.append(f"form: {result.detail}{hint}")
        elif channel == "manual":
            method = d.application_method if d else "unknown"
            needs_call = method == "phone" or (bool(phone_hint) and not d.agent_emails)
            reason = "Listing asks you to call" if method == "phone" else "Couldn't apply automatically"
            return Outcome(Status.MANUAL_APPLY, "manual", "; ".join(notes) or f"apply via {method}",
                           call_needed=needs_call and bool(phone_hint), call_reason=reason if needs_call else "")
    return Outcome(Status.FAILED, "none", "; ".join(notes))
