"""The 24/7 loop: poll the bot mailbox, process listings in parallel, run the daily and weekly jobs."""

from __future__ import annotations

import imaplib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import bandit, learn
from .ingest import extract_candidates, is_alert, might_be_message
from .mail import Email
from .models import TERMINAL, Status
from .pipeline import Context, process_candidate, process_reply, record_outcome

log = logging.getLogger(__name__)
SILENCE_HOURS = 12
STALL_SECONDS = 600
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_last_beat = time.time()


def _watchdog() -> None:
    """If the main loop hangs (stuck IMAP socket, deadlock), exit so Docker restarts the bot."""
    while True:
        time.sleep(60)
        if time.time() - _last_beat > STALL_SECONDS:
            log.error("main loop stalled for %ss, exiting for a restart", STALL_SECONDS)
            os._exit(1)


def _logged(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # worker exceptions would otherwise vanish inside their futures
        log.exception("%s failed", fn.__name__)


def submit(pool: ThreadPoolExecutor, fn, *args) -> None:
    pool.submit(_logged, fn, *args)


def handle_alert(ctx: Context, pool: ThreadPoolExecutor, mail: Email) -> None:
    candidates = extract_candidates(mail, ctx.http)   # may follow redirects: runs in a worker, not the main loop
    log.info("alert from %s: %d listing link(s)", mail.sender_domain, len(candidates))
    for c in candidates:
        submit(pool, process_candidate, ctx, c.url, mail.sender_domain, c.snippet)
    if not candidates or might_be_message(mail):
        process_reply(ctx, mail)


def handle_mail(ctx: Context, pool: ThreadPoolExecutor, mail: Email) -> None:
    alert = is_alert(mail, ctx.cfg.alerts.sender_domains)
    ctx.store.mark_email(mail.message_id, "alert" if alert else "reply")
    if alert:
        ctx.store.kv_set("last_alert", str(time.time()))
        submit(pool, handle_alert, ctx, pool, mail)
    else:
        submit(pool, process_reply, ctx, mail)


def sweep_no_response(ctx: Context) -> None:
    """Applications without any reply after N days: count as 'no response' and flag a follow-up call."""
    days = ctx.cfg.schedule.no_response_days
    cutoff = time.time() - days * 86400
    for listing in ctx.store.all({Status.APPLIED}):
        if listing.applied_at and listing.applied_at < cutoff:
            listing.status = Status.NO_RESPONSE
            record_outcome(ctx.store, listing, won=False)
            if listing.phone:
                listing.call_needed = True
                listing.call_reason = f"No reply after {days} days: a call may still get you a viewing"
            ctx.mirror(listing)


def digest_text(ctx: Context, since: float) -> str:
    listings = ctx.store.all()
    new = [x for x in listings if x.first_seen >= since and x.status != Status.DUPLICATE
           and x.notes != "not a rental listing"]

    def count(*statuses: Status) -> int:
        return sum(1 for x in new if x.status in statuses)

    lines = [f"{len(new)} new listings: {count(Status.APPLIED)} applied, {count(Status.READY)} ready (dry run), "
             f"{count(Status.MANUAL_APPLY)} for you to apply, {count(Status.LIKELY_SCAM)} scams blocked, "
             f"{count(Status.FILTERED)} didn't fit."]
    viewings = [x for x in listings if x.status == Status.VIEWING_BOOKED]
    if viewings:
        lines.append("Viewings: " + "; ".join(f"{x.address} {x.viewing_at}" for x in viewings[:5]))
    calls = [x for x in listings if x.call_needed and x.status not in TERMINAL]
    if calls:
        lines.append("Call: " + "; ".join(f"{x.address} {x.phone}" for x in calls[:5]))
    open_items = [x for x in listings if x.status in (Status.VIEWING_INVITED, Status.DOCS_REQUESTED,
                                                       Status.NEEDS_REPLY)]
    if open_items:
        lines.append("Waiting on you: " + "; ".join(f"{x.address} ({x.status.value})" for x in open_items[:5]))
    top = sorted((x for x in new if x.status in (Status.APPLIED, Status.READY, Status.MANUAL_APPLY)),
                 key=lambda x: -(x.rank or 0))[:3]
    if top:
        lines.append("Best new: " + "; ".join(f"{x.address} €{x.total_rent or '?'}" for x in top))
    lines.append(f"Claude spend today so far: ${ctx.store.spend_today():.2f}")
    return "\n\n".join(lines)


def scheduled(ctx: Context, now: datetime) -> None:
    store, sched = ctx.store, ctx.cfg.schedule
    today = now.date().isoformat()

    if now.strftime("%H:%M") >= sched.digest_time and store.kv_get("digest:date") != today:
        since = float(store.kv_get("digest:ts") or time.time() - 86400)
        ctx.notifier.send("Daily summary", digest_text(ctx, since))
        store.kv_set("digest:date", today)
        store.kv_set("digest:ts", str(time.time()))

    if time.time() - float(store.kv_get("sweep:ts") or 0) > 3600:
        sweep_no_response(ctx)
        store.kv_set("sweep:ts", str(time.time()))

    week = f"{now.isocalendar().year}-{now.isocalendar().week}"
    if (DAYS[now.weekday()] == sched.weekly_learn_day.lower()[:3] and now.strftime("%H:%M") >= sched.weekly_learn_time
            and store.kv_get("learn:week") != week):
        store.kv_set("learn:week", week)
        learn.weekly(ctx)

    last_alert = float(store.kv_get("last_alert") or store.kv_get("started") or time.time())
    if time.time() - last_alert > SILENCE_HOURS * 3600 and store.kv_get("silence:date") != today:
        store.kv_set("silence:date", today)
        ctx.notifier.send("No listing alerts for 12 hours",
                          "Check that your alert services still email the bot mailbox (and not its spam).",
                          important=True)


def run(ctx: Context) -> None:
    if ctx.mailbox is None:
        raise SystemExit("BOT_EMAIL / BOT_EMAIL_APP_PASSWORD are not set in .env")
    cfg, store = ctx.cfg, ctx.store
    tz = ZoneInfo(cfg.schedule.timezone)
    bandit.ensure_defaults(store)
    store.kv_set("started", str(time.time()))
    mode = "DRY RUN (nothing is sent)" if cfg.apply.dry_run else "LIVE (applications are sent)"
    log.info("housing bot running: %s, polling every %ss", mode, cfg.schedule.inbox_poll_seconds)

    global _last_beat
    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="worker") as pool:
        while True:
            started = _last_beat = time.time()
            try:
                for mail in ctx.mailbox.fetch_new():
                    handle_mail(ctx, pool, mail)
            except (imaplib.IMAP4.error, OSError) as e:
                log.warning("mailbox poll failed: %s", e)
            try:
                scheduled(ctx, datetime.now(tz))
            except Exception:  # a failing digest/review must never stop the watcher
                log.exception("scheduled job failed")
            store.kv_set("heartbeat", str(time.time()))
            time.sleep(max(1.0, cfg.schedule.inbox_poll_seconds - (time.time() - started)))
