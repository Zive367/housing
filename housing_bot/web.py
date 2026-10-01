"""Phone-friendly dashboard: what needs you first, one tap to act, and the settings you can change yourselves.

Served by the bot itself (stdlib only). Security: password login with a signed cookie, every page escapes scraped
text, every change is POST-only with a CSRF token, and docker-compose publishes the port on localhost only
(reach it through Tailscale).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote_plus, urlsplit
from zoneinfo import ZoneInfo

from . import settings
from .models import Listing, Status
from .pipeline import Context, record_outcome

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")
COOKIE = "hb_session"
SESSION_DAYS = 90

# What needs you, most urgent first.
DO_NOW = [Status.VIEWING_INVITED, Status.MANUAL_APPLY, Status.DOCS_REQUESTED, Status.NEEDS_REPLY]
CLOSED = {Status.CLOSED, Status.REJECTED, Status.FILTERED, Status.LIKELY_SCAM, Status.DUPLICATE, Status.UNAVAILABLE}
LABELS = {
    Status.VIEWING_INVITED: ("Book viewing", "urgent"), Status.MANUAL_APPLY: ("Apply yourself", "urgent"),
    Status.DOCS_REQUESTED: ("Send documents", "warn"), Status.NEEDS_REPLY: ("Reply needed", "warn"),
    Status.VIEWING_BOOKED: ("Viewing booked", "good"), Status.OFFER: ("Offer", "good"),
    Status.READY: ("Ready (practice)", "info"), Status.APPLIED: ("Applied", "info"),
    Status.NO_RESPONSE: ("No response", "muted"), Status.REJECTED: ("Rejected", "muted"),
    Status.FILTERED: ("Doesn't fit", "muted"), Status.LIKELY_SCAM: ("Likely scam", "bad"),
    Status.UNAVAILABLE: ("Gone", "muted"), Status.FAILED: ("Failed", "bad"), Status.CLOSED: ("Closed", "muted"),
    Status.NEW: ("Processing", "info"), Status.DUPLICATE: ("Duplicate", "muted"),
}
# Buttons: action -> (label, statuses it shows on)
ACTIONS = {
    "applied": ("Mark applied", {Status.MANUAL_APPLY, Status.READY, Status.FAILED}),
    "booked": ("Viewing booked", {Status.VIEWING_INVITED, Status.APPLIED, Status.NEEDS_REPLY, Status.NO_RESPONSE}),
    "done": ("Done", {Status.DOCS_REQUESTED, Status.NEEDS_REPLY}),
    "offer": ("Got an offer", {Status.VIEWING_BOOKED}),
    "rejected": ("Rejected", {Status.APPLIED, Status.VIEWING_INVITED, Status.VIEWING_BOOKED, Status.NEEDS_REPLY,
                              Status.DOCS_REQUESTED, Status.NO_RESPONSE}),
    "skip": ("Skip", {Status.MANUAL_APPLY, Status.READY, Status.FAILED, Status.VIEWING_INVITED}),
}
ICON = {
    "phone": "<svg viewBox='0 0 24 24'><path d='M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25 11.4 11.4 0 0 0 "
             "3.6.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.25.2 2.45.57 "
             "3.57a1 1 0 0 1-.25 1z'/></svg>",
    "gear": "<svg viewBox='0 0 24 24'><path d='M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4"
            "l-.4 2.9a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 1.7 "
            "1L11 21h4l.4-2.9a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5zM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z'/></svg>",
    "back": "<svg viewBox='0 0 24 24'><path d='M15.4 7.4 14 6l-6 6 6 6 1.4-1.4L10.8 12z'/></svg>",
}


def e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(url: str) -> str:
    return url if urlsplit(url or "").scheme in ("http", "https") else ""


def when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d %b %H:%M") if ts else ""


def ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = time.time() - ts
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return when(ts)


def euro(value: float | None) -> str:
    return f"€{value:,.0f}".replace(",", ".") if value else ""


def apply_action(ctx: Context, listing: Listing, action: str, value: str = "") -> None:
    """Change a listing's status from the dashboard; the sheet and the learning loop follow."""
    now = time.time()
    if action == "applied":
        listing.status, listing.apply_method = Status.APPLIED, "you (dashboard)"
        listing.applied_at = listing.applied_at or now
        listing.time_to_apply_s = listing.applied_at - listing.first_seen
        ctx.store.log(listing.id, "applied", method="manual", agency=(listing.agency or listing.domain).lower())
    elif action == "booked":
        listing.status = Status.VIEWING_BOOKED
        if value:
            listing.viewing_at = value
        record_outcome(ctx.store, listing, won=True)
    elif action == "offer":
        listing.status = Status.OFFER
        record_outcome(ctx.store, listing, won=True)
    elif action == "done":
        listing.status = Status.APPLIED
    elif action == "rejected":
        listing.status = Status.REJECTED
        record_outcome(ctx.store, listing, won=False)
    elif action == "skip":
        listing.status = Status.CLOSED
    elif action == "called":
        listing.call_needed, listing.call_reason = False, ""
        listing.notes = f"{listing.notes} · called {when(now)}".strip(" ·")
    else:
        raise ValueError(action)
    ctx.store.log(listing.id, "dashboard", action=action)
    ctx.mirror(listing)


def sections(listings: list[Listing]) -> list[tuple[str, str, list[Listing], bool]]:
    """(id, title, items, open by default)."""
    newest = sorted(listings, key=lambda x: -x.first_seen)
    urgency = {s: i for i, s in enumerate(DO_NOW)}
    do_now = sorted((x for x in newest if x.status in urgency or (x.call_needed and x.status not in CLOSED)),
                    key=lambda x: (urgency.get(x.status, len(DO_NOW)), -x.first_seen))
    taken = {x.id for x in do_now}
    viewings = sorted((x for x in newest if x.status in (Status.VIEWING_BOOKED, Status.OFFER) and x.id not in taken),
                      key=lambda x: x.viewing_at or "9999")
    ready = [x for x in newest if x.status == Status.READY and x.id not in taken]
    applied = [x for x in newest if x.status in (Status.APPLIED, Status.NO_RESPONSE) and x.id not in taken][:60]
    skipped = [x for x in newest if x.status in (Status.FILTERED, Status.LIKELY_SCAM, Status.REJECTED, Status.CLOSED,
                                                 Status.FAILED) and x.id not in taken][:60]
    return [("now", "Do now", do_now, True), ("viewings", "Viewings", viewings, True),
            ("ready", "Ready to send", ready, True), ("applied", "Applied", applied, False),
            ("skipped", "Skipped & closed", skipped, False)]


def _form(listing: Listing, action: str, text: str, token: str, css: str = "chip-btn", extra: str = "") -> str:
    return (f"<form method=post action='/act'><input type=hidden name=id value='{e(listing.id)}'>"
            f"<input type=hidden name=action value={action}><input type=hidden name=t value='{token}'>"
            f"{extra}<button class='{css}'>{e(text)}</button></form>")


def card(listing: Listing, token: str) -> str:
    d = listing.details
    label, tone = LABELS.get(listing.status, (listing.status.value, "info"))
    furnished = {"furnished": "🛋 furnished", "upholstered": "gestoffeerd", "unfurnished": "unfurnished"}.get(
        d.furnished if d else "", "")
    facts = [f"{d.size_m2:.0f} m²" if d and d.size_m2 else "",
             f"🚲 {listing.bike_minutes} min" if listing.bike_minutes is not None else "",
             f"🚶 {listing.walk_minutes} min" if listing.walk_minutes is not None else "",
             furnished,
             f"★ area {listing.neighbourhood_score:g}/10" if listing.neighbourhood_score is not None else "",
             "👫 together" if listing.apply_as == "couple" else ""]
    chips = "".join(f"<span class=fact>{e(f)}</span>" for f in facts if f)
    if listing.scam_score is not None and listing.scam_score >= 40:
        chips += f"<span class='fact bad'>⚠ scam risk {listing.scam_score}</span>"
    area = ", ".join(x for x in [listing.buurt, listing.municipality.replace("'s-Gravenhage", "Den Haag")] if x)

    notes = []
    if listing.viewing_at:
        notes.append(f"<div class='note good'>📅 <b>{e(listing.viewing_at.replace('T', ' '))}</b></div>")
    if listing.status in (Status.VIEWING_INVITED, Status.DOCS_REQUESTED, Status.NEEDS_REPLY) and listing.last_reply:
        notes.append(f"<div class=note>💬 {e(listing.last_reply)}</div>")
    if listing.call_needed:
        notes.append(f"<div class='note warn'>📞 {e(listing.call_reason or 'Call the agent')}</div>")
    if listing.status in (Status.FILTERED, Status.LIKELY_SCAM):
        notes.append(f"<div class=note>{e('; '.join(listing.filter_reasons or listing.scam_reasons))}</div>")
    if listing.apply_error and listing.status == Status.MANUAL_APPLY:
        notes.append(f"<div class='note muted'>{e(listing.apply_error)}</div>")

    url = safe_url(listing.url)
    main = []
    if listing.message and listing.status in (Status.MANUAL_APPLY, Status.READY, Status.FAILED):
        main.append(f"<button class='btn primary' data-copy='{e(listing.message)}' data-open='{e(url)}'>"
                    "Apply: copy message &amp; open</button>")
    if listing.viewing_link and safe_url(listing.viewing_link):
        main.append(f"<a class='btn primary' href='{e(listing.viewing_link)}' target=_blank rel=noopener>"
                    "Pick a viewing slot</a>")
    if listing.phone:
        main.append(f"<a class='btn call' href='tel:{e(listing.phone)}' data-confirm='Call {e(listing.phone)}?'>"
                    f"{ICON['phone']}<span>Call now</span><small>{e(listing.phone)}</small></a>")
    elif listing.status not in CLOSED:
        who = listing.agency or listing.address or listing.domain
        main.append(f"<a class='btn call ghost' target=_blank rel=noopener "
                    f"href='https://www.google.com/search?q={quote_plus(who + ' telefoon')}'>"
                    f"{ICON['phone']}<span>Find number</span></a>")
    if url and not any("data-open" in b for b in main):
        main.append(f"<a class=btn href='{e(url)}' target=_blank rel=noopener>Open listing</a>")

    small = []
    for action, (text, statuses) in ACTIONS.items():
        if listing.status in statuses:
            extra = "<input name=value placeholder='when? (optional)'>" if action == "booked" else ""
            css = "chip-btn ghost" if action in ("skip", "rejected") else "chip-btn"
            small.append(_form(listing, action, text, token, css, extra))
    if listing.call_needed:
        small.append(_form(listing, "called", "Called ✓", token))
    message = (f"<details class=msg><summary>Message</summary><pre>{e(listing.message)}</pre></details>"
               if listing.message else "")
    contact = " · ".join(x for x in [listing.agency, listing.agent] if x)
    return f"""<article class='card {tone}'>
  <div class=card-top><span class='badge {tone}'>{e(label)}</span><span class=time>{e(ago(listing.first_seen))}</span></div>
  <h3>{e(listing.address or listing.domain)}</h3>
  {f'<div class=sub>{e(area)}</div>' if area else ''}
  <div class=price-row>{f'<span class=price>{euro(listing.total_rent)}<small>/mo</small></span>' if listing.total_rent else ''}
  <div class=facts>{chips}</div></div>
  {''.join(notes)}
  {f'<div class=contact>{e(contact)}</div>' if contact else ''}
  <div class=main-actions>{''.join(main)}</div>
  {f'<div class=small-actions>{"".join(small)}</div>' if small else ''}
  {message}
</article>"""


CSS = """
:root{--bg:#f3f1ec;--card:#fff;--ink:#1c1b19;--muted:#7b776e;--line:#e8e4db;--soft:#f6f4ef;
--brand:#1f5c45;--brand2:#2f7d5d;--urgent:#d4541c;--warn:#c48a12;--good:#2f7d5d;--info:#3d6aa8;--bad:#c2372b;
--shadow:0 1px 2px rgba(30,25,15,.06),0 4px 14px rgba(30,25,15,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1c1b19;--ink:#efece5;--muted:#a29e94;--line:#2e2c28;
--soft:#242320;--brand:#1f5c45;--brand2:#5fb08a;--urgent:#f0874f;--warn:#e3b44f;--good:#5fb08a;--info:#8db1e6;
--bad:#f07a6e;--shadow:none}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",
Roboto,sans-serif;-webkit-font-smoothing:antialiased}
svg{width:20px;height:20px;fill:currentColor;flex:none}
.hero{background:linear-gradient(135deg,var(--brand),#2c8a63);color:#fff;padding:calc(18px + env(safe-area-inset-top)) 18px 22px;
border-radius:0 0 26px 26px}
.hero-top{display:flex;justify-content:space-between;align-items:center}
.hero h1{margin:0;font-size:26px;letter-spacing:-.02em}
.hero .status{font-size:13px;opacity:.9;margin-top:2px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
background:#8ef0b8;margin-right:6px;box-shadow:0 0 0 3px rgba(142,240,184,.25)}.dot.off{background:#ffb4a8;box-shadow:none}
.icon-btn{display:grid;place-items:center;width:42px;height:42px;border-radius:14px;background:rgba(255,255,255,.16);color:#fff}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:16px}
.stat{background:rgba(255,255,255,.13);border-radius:14px;padding:10px 8px;text-align:center;text-decoration:none;color:#fff}
.stat b{display:block;font-size:22px;line-height:1.1}.stat span{font-size:11px;opacity:.85}
.stat.hot{background:#fff;color:var(--urgent)}.stat.hot span{opacity:1}
main{padding:6px 14px 60px;max-width:640px;margin:auto}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:24px 4px 10px}
.card{background:var(--card);border-radius:20px;padding:16px;margin:12px 0;box-shadow:var(--shadow);
border:1px solid var(--line);position:relative;overflow:hidden}
.card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:5px;background:var(--info)}
.card.urgent:before{background:var(--urgent)}.card.warn:before{background:var(--warn)}.card.good:before{background:var(--good)}
.card.bad:before{background:var(--bad)}.card.muted:before{background:var(--line)}
.card-top{display:flex;justify-content:space-between;align-items:center}
.badge{font-size:12px;font-weight:700;padding:4px 10px;border-radius:99px;background:var(--soft);color:var(--info)}
.badge.urgent{background:color-mix(in srgb,var(--urgent) 14%,transparent);color:var(--urgent)}
.badge.warn{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}
.badge.good{background:color-mix(in srgb,var(--good) 15%,transparent);color:var(--good)}
.badge.bad{background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}.badge.muted{color:var(--muted)}
.time{font-size:12px;color:var(--muted)}
.card h3{margin:10px 0 0;font-size:18px;letter-spacing:-.01em;line-height:1.25}.sub{color:var(--muted);font-size:14px}
.price-row{margin-top:10px}.price{font-size:24px;font-weight:750;letter-spacing:-.02em}
.price small{font-size:13px;font-weight:500;color:var(--muted);margin-left:2px}
.facts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.fact{background:var(--soft);border-radius:8px;padding:3px 9px;font-size:13px}.fact.bad{color:var(--bad)}
.note{background:var(--soft);border-radius:12px;padding:9px 12px;margin-top:10px;font-size:14px}
.note.good{color:var(--good)}.note.warn{color:var(--warn)}.note.muted{color:var(--muted)}
.contact{color:var(--muted);font-size:14px;margin-top:10px}
.main-actions{display:grid;gap:8px;margin-top:14px}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;font:inherit;font-size:16px;font-weight:600;
border-radius:14px;padding:13px 16px;min-height:50px;text-decoration:none;cursor:pointer;border:1px solid var(--line);
background:var(--card);color:var(--ink)}
.btn.primary{background:var(--brand2);border-color:var(--brand2);color:#fff}
.btn.call{background:color-mix(in srgb,var(--good) 12%,var(--card));border-color:color-mix(in srgb,var(--good) 35%,transparent);
color:var(--good)}.btn.call small{font-weight:500;opacity:.8}.btn.call.ghost{background:var(--card);color:var(--muted)}
.small-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.small-actions form{display:flex;gap:6px;align-items:center}
.chip-btn{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);color:var(--ink);
border-radius:99px;padding:7px 13px;cursor:pointer}.chip-btn.ghost{color:var(--muted)}
.small-actions input{font:inherit;font-size:14px;border:1px solid var(--line);border-radius:99px;padding:7px 12px;
width:150px;background:var(--card);color:var(--ink)}
.msg summary{cursor:pointer;color:var(--muted);font-size:14px;margin-top:12px}
pre{white-space:pre-wrap;font:inherit;font-size:14px;background:var(--soft);padding:12px;border-radius:12px}
.empty{background:var(--card);border-radius:20px;padding:24px;text-align:center;color:var(--muted);border:1px dashed var(--line)}
details.group>summary{list-style:none;cursor:pointer}details.group>summary::-webkit-details-marker{display:none}
details.group>summary h2:after{content:" ▸"}details.group[open]>summary h2:after{content:" ▾"}
.toast{position:fixed;bottom:calc(20px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);
background:var(--ink);color:var(--bg);padding:12px 18px;border-radius:14px;display:none;font-size:15px;z-index:9}
.login{max-width:360px;margin:18vh auto;padding:0 20px;text-align:center}
.login .logo{width:64px;height:64px;margin:0 auto 12px;border-radius:18px;background:var(--brand2);display:grid;place-items:center}
.login .logo svg{width:34px;height:34px;fill:#fff}
.field input,.field select,.field textarea,.login input{width:100%;font:inherit;font-size:16px;border:1px solid var(--line);
border-radius:12px;padding:12px;background:var(--card);color:var(--ink)}
.login input{margin:14px 0 10px}.login .btn{width:100%}
.panel{background:var(--card);border-radius:20px;padding:16px;margin:12px 0;border:1px solid var(--line);box-shadow:var(--shadow)}
.panel h2{margin:0 0 6px;color:var(--ink);font-size:15px;text-transform:none;letter-spacing:0}
.field{margin:12px 0}.field label{display:block;font-size:14px;font-weight:600;margin-bottom:5px}
.field .hint{font-size:12px;color:var(--muted);margin-top:4px}
.switch{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:14px 0;font-size:15px}
.switch input{appearance:none;width:50px;height:30px;border-radius:99px;background:var(--line);position:relative;
flex:none;cursor:pointer;transition:.2s}
.switch input:before{content:"";position:absolute;width:24px;height:24px;border-radius:50%;background:#fff;top:3px;left:3px;
transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.25)}.switch input:checked{background:var(--brand2)}
.switch input:checked:before{left:23px}
.pills{display:flex;flex-wrap:wrap;gap:8px}.pills label{font-weight:500}.pills input{display:none}
.pills span{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:8px 14px;font-size:14px;cursor:pointer}
.pills input:checked+span{background:var(--brand2);border-color:var(--brand2);color:#fff}
.savebar{position:sticky;bottom:0;padding:12px 0 calc(12px + env(safe-area-inset-bottom));
background:linear-gradient(transparent,var(--bg) 30%)}
.alert{border-radius:14px;padding:12px 14px;margin:12px 0;font-size:14px}
.alert.ok{background:color-mix(in srgb,var(--good) 14%,var(--card));color:var(--good)}
.alert.err{background:color-mix(in srgb,var(--bad) 12%,var(--card));color:var(--bad)}
.topbar{display:flex;align-items:center;gap:10px;padding:calc(14px + env(safe-area-inset-top)) 14px 0;max-width:640px;margin:auto}
.topbar a{color:var(--ink)}.topbar h1{font-size:22px;margin:0}
"""

SCRIPT = """
document.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(b.dataset.copy); toast('Message copied. Paste it in the form.'); }
  catch (err) { toast('Copy failed: open "Message" below and copy it'); }
  if (b.dataset.open) window.open(b.dataset.open, '_blank', 'noopener');
}));
document.querySelectorAll('[data-confirm]').forEach(a => a.addEventListener('click', ev => {
  if (!confirm(a.dataset.confirm)) ev.preventDefault();
}));
function toast(text){const t=document.querySelector('.toast');t.textContent=text;t.style.display='block';
  clearTimeout(window._tt);window._tt=setTimeout(()=>t.style.display='none',3500);}
"""


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes><meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=theme-color content="#1f5c45"><link rel=manifest href=/manifest.json><link rel=apple-touch-icon href=/icon.svg>
<title>{e(title)}</title><style>{CSS}</style></head>
<body>{body}<div class=toast></div><script>{SCRIPT}</script></body></html>"""


def dashboard(ctx: Context, token: str) -> str:
    listings = ctx.store.all()
    groups = sections(listings)
    counts = {gid: len(items) for gid, _, items, _ in groups}
    beat = float(ctx.store.kv_get("heartbeat") or 0)
    running = time.time() - beat < 300
    mode = "practice mode" if ctx.cfg.apply.dry_run else "live"
    week = sum(1 for x in listings if x.applied_at and x.applied_at > time.time() - 7 * 86400)
    today = sum(1 for x in listings if x.first_seen > time.time() - 86400 and x.status != Status.DUPLICATE)
    parts = []
    for gid, title, items, is_open in groups:
        if not items and gid != "now":
            continue
        cards = "".join(card(x, token) for x in items) or "<div class=empty>🎉 Nothing needs you right now.</div>"
        if is_open:
            parts.append(f"<section id={gid}><h2>{e(title)}</h2>{cards}</section>")
        else:
            parts.append(f"<section id={gid}><details class=group><summary><h2>{e(title)} ({len(items)})</h2>"
                         f"</summary>{cards}</details></section>")
    return page("Housing", f"""<header class=hero>
  <div class=hero-top><div><h1>Housing</h1><div class=status><span class='dot{'' if running else ' off'}'></span>
  {'Running' if running else 'Bot not running'} · {e(mode)} · Claude ${ctx.store.spend_today():.2f} today</div></div>
  <a class=icon-btn href=/settings aria-label=Settings>{ICON['gear']}</a></div>
  <div class=stats>
    <a class='stat{' hot' if counts['now'] else ''}' href=#now><b>{counts['now']}</b><span>do now</span></a>
    <a class=stat href=#viewings><b>{counts['viewings']}</b><span>viewings</span></a>
    <a class=stat href=#applied><b>{week}</b><span>applied 7d</span></a>
    <a class=stat href=#now><b>{today}</b><span>new today</span></a>
  </div></header><main>{''.join(parts)}</main>""")


def settings_page(ctx: Context, token: str, notice: str = "", errors: list[str] | None = None) -> str:
    cfg = ctx.cfg
    partner = cfg.partner.first_name or "Partner"
    titles = dict(settings.GROUPS, partner=partner)
    panels = []
    for group, title in titles.items():
        rows = []
        for f in (x for x in settings.FIELDS if x.group == group):
            value = settings.get(cfg, f.key)
            name = e(f.key)
            hint = f"<div class=hint>{e(f.hint)}</div>" if f.hint else ""
            if f.kind == "bool":
                rows.append(f"<label class=switch><span>{e(f.label)}</span><input type=hidden name='{name}' value=off>"
                            f"<input type=checkbox name='{name}' {'checked' if value else ''}></label>")
            elif f.kind == "multi":
                pills = "".join(
                    f"<label><input type=checkbox name='{name}' value='{e(o)}' {'checked' if o in (value or []) else ''}>"
                    f"<span>{e(settings.MUNICIPALITY_LABELS.get(o, {'nl': 'Dutch', 'en': 'English'}.get(o, o)))}</span>"
                    "</label>" for o in f.options)
                rows.append(f"<div class=field><label>{e(f.label)}</label><div class=pills>{pills}</div></div>")
            elif f.kind == "choice":
                opts = "".join(f"<option value='{e(o)}' {'selected' if o == value else ''}>{e(o or '—')}</option>"
                               for o in f.options)
                rows.append(f"<div class=field><label>{e(f.label)}</label><select name='{name}'>{opts}</select></div>")
            elif f.kind == "textarea":
                rows.append(f"<div class=field><label>{e(f.label)}</label>"
                            f"<textarea name='{name}' rows=2>{e(value)}</textarea>{hint}</div>")
            else:
                shown = f"{value:g}" if isinstance(value, float) else (value if value is not None else "")
                kind = {"number": "inputmode=decimal", "phone": "type=tel"}.get(f.kind, "")
                rows.append(f"<div class=field><label>{e(f.label)}</label>"
                            f"<input name='{name}' value='{e(shown)}' {kind}>{hint}</div>")
        panels.append(f"<section class=panel><h2>{e(title)}</h2>{''.join(rows)}</section>")
    alert = f"<div class='alert ok'>{e(notice)}</div>" if notice else ""
    if errors:
        alert = "<div class='alert err'>Not saved:<br>" + "<br>".join(e(x) for x in errors) + "</div>"
    return page("Settings", f"""<div class=topbar><a href=/ aria-label=Back>{ICON['back']}</a><h1>Settings</h1></div>
<main>{alert}<form method=post action=/settings><input type=hidden name=t value='{token}'>{''.join(panels)}
<p class=hint style='color:var(--muted);font-size:13px'>Saved on the server and used immediately. Keys and passwords
can only be changed on the server itself.</p>
<div class=savebar><button class='btn primary' style='width:100%'>Save settings</button></div></form></main>""")


MANIFEST = json.dumps({
    "name": "Housing", "short_name": "Housing", "start_url": "/", "display": "standalone",
    "background_color": "#f3f1ec", "theme_color": "#1f5c45",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
})
APP_ICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><defs><linearGradient id='g' x1='0' y1='0' "
            "x2='1' y2='1'><stop offset='0' stop-color='#1f5c45'/><stop offset='1' stop-color='#2c8a63'/></linearGradient>"
            "</defs><rect width='64' height='64' rx='14' fill='url(#g)'/><path d='M14 32 32 16l18 16v18H38V38H26v12H14z' "
            "fill='#fff'/></svg>")
LOGIN = """<form class=login method=post action=/login>
<div class=logo><svg viewBox='0 0 64 64'><path d='M14 32 32 16l18 16v18H38V38H26v12H14z'/></svg></div>
<h1 style='margin:0'>Housing</h1><input type=password name=password placeholder=Password autofocus
autocomplete=current-password><button class='btn primary'>Log in</button></form>"""


class LoginLimiter:
    """Stops password guessing: 5 wrong tries per address per 15 min, and 30 in total per hour."""

    PER_IP, WINDOW, GLOBAL, GLOBAL_WINDOW = 5, 900, 30, 3600

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def _recent(self, ip: str | None, window: int) -> list[float]:
        now = time.time()
        stamps = [t for ts in ([self._fails.get(ip, [])] if ip else self._fails.values()) for t in ts]
        return [t for t in stamps if now - t < window]

    def blocked_for(self, ip: str) -> int:
        """Seconds until this address may try again (0 = allowed)."""
        with self._lock:
            mine, everyone = self._recent(ip, self.WINDOW), self._recent(None, self.GLOBAL_WINDOW)
            if len(mine) >= self.PER_IP:
                return int(self.WINDOW - (time.time() - min(mine)))
            if len(everyone) >= self.GLOBAL:
                return int(self.GLOBAL_WINDOW - (time.time() - min(everyone)))
            return 0

    def fail(self, ip: str) -> int:
        with self._lock:
            now = time.time()
            self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < self.GLOBAL_WINDOW] + [now]
            return sum(len(v) for v in self._fails.values())

    def success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


class Auth:
    def __init__(self, ctx: Context):
        self.password = ctx.cfg.secrets.dashboard_password
        key = ctx.store.kv_get("web:key")
        if not key:
            key = secrets.token_hex(32)
            ctx.store.kv_set("web:key", key)
        self.key = key.encode()

    def sign(self, text: str) -> str:
        return hmac.new(self.key, text.encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        expires = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{expires}.{self.sign(expires)}"

    def valid(self, session: str | None) -> bool:
        if not session or "." not in session:
            return False
        expires, sig = session.split(".", 1)
        return hmac.compare_digest(sig, self.sign(expires)) and expires.isdigit() and int(expires) > time.time()

    def csrf(self, session: str) -> str:
        return self.sign("csrf:" + session)[:32]


def make_handler(ctx: Context, auth: Auth):
    limiter = LoginLimiter()
    public = ctx.cfg.secrets.dashboard_public

    class Handler(BaseHTTPRequestHandler):
        server_version = "housing"

        def _ip(self) -> str:
            # The port is only published on localhost, so a forwarded-for header can only come from Tailscale.
            forwarded = self.headers.get("X-Forwarded-For", "")
            return forwarded.split(",")[0].strip() or self.client_address[0]

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def _session(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie[COOKIE].value if COOKIE in cookie else None

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", headers=None) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; "
                             "form-action 'self'; base-uri 'none'")
            if public:
                self.send_header("Strict-Transport-Security", "max-age=31536000")

        def _redirect(self, where: str, headers=None) -> None:
            self.send_response(303)
            self._security_headers()
            self.send_header("Location", where)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict[str, list[str]]:
            length = min(int(self.headers.get("Content-Length") or 0), 20000)
            return parse_qs(self.rfile.read(length).decode(errors="replace"), keep_blank_values=True)

        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path == "/manifest.json":
                return self._send(200, MANIFEST, "application/manifest+json")
            if parts.path == "/icon.svg":
                return self._send(200, APP_ICON, "image/svg+xml")
            if parts.path == "/health":
                return self._send(200, "ok", "text/plain")
            session = self._session()
            if not auth.valid(session):
                return self._send(200, page("Log in", LOGIN))
            if parts.path == "/":
                return self._send(200, dashboard(ctx, auth.csrf(session)))
            if parts.path == "/settings":
                notice = "Saved. The bot uses the new settings right away." if "saved" in parts.query else ""
                return self._send(200, settings_page(ctx, auth.csrf(session), notice))
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            path = urlsplit(self.path).path
            form = self._form()
            first = {k: v[-1] for k, v in form.items()}
            if path == "/login":
                ip = self._ip()
                wait = limiter.blocked_for(ip)
                if wait:
                    return self._send(429, page("Log in", LOGIN.replace(
                        "<h1 style='margin:0'>Housing</h1>",
                        f"<h1 style='margin:0'>Housing</h1><p class='alert err'>Too many wrong passwords. "
                        f"Try again in {wait // 60 + 1} min.</p>")))
                if auth.password and hmac.compare_digest(first.get("password", ""), auth.password):
                    limiter.success(ip)
                    secure = "; Secure" if public or self.headers.get("X-Forwarded-Proto") == "https" else ""
                    return self._redirect("/", {"Set-Cookie": f"{COOKIE}={auth.new_session()}; Path=/; HttpOnly; "
                                                              f"SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"})
                total = limiter.fail(ip)
                log.warning("dashboard: wrong password from %s", ip)
                if total in (10, 30):
                    ctx.notifier.send("Someone is guessing your dashboard password",
                                      f"{total} wrong passwords in the last hour (latest from {ip}). Logins are "
                                      "being slowed down. If this keeps happening, set a longer DASHBOARD_PASSWORD "
                                      "or turn the public link off: tailscale funnel --bg off", important=True)
                time.sleep(1.5)  # slow down guessing
                return self._redirect("/")
            session = self._session()
            if not auth.valid(session) or not hmac.compare_digest(first.get("t", ""), auth.csrf(session)):
                return self._send(403, "forbidden", "text/plain")
            if path == "/act":
                listing = ctx.store.get(first.get("id", ""))
                if listing is None:
                    return self._send(404, "listing not found", "text/plain")
                try:
                    apply_action(ctx, listing, first.get("action", ""), first.get("value", "")[:60])
                except ValueError:
                    return self._send(400, "unknown action", "text/plain")
                return self._redirect("/#now")
            if path == "/settings":
                values, errors = settings.parse(form, ctx.cfg.partner.first_name or "Partner")
                if errors:
                    return self._send(400, settings_page(ctx, auth.csrf(session), errors=errors))
                settings.save(ctx.cfg, values)
                ctx.store.log(None, "settings", changed=sorted(values))
                return self._redirect("/settings?saved")
            self._send(404, "not found", "text/plain")

    return Handler


def serve(ctx: Context, port: int = 8080, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Start the dashboard in a background thread. Returns the server (call .shutdown() to stop)."""
    password = ctx.cfg.secrets.dashboard_password
    if not password:
        raise ValueError("DASHBOARD_PASSWORD is not set in .env")
    if ctx.cfg.secrets.dashboard_public and len(password) < 14:
        raise ValueError("DASHBOARD_PUBLIC is on: DASHBOARD_PASSWORD must be at least 14 characters")
    server = ThreadingHTTPServer((host, port), make_handler(ctx, Auth(ctx)))
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    log.info("dashboard on http://%s:%s", host, server.server_port)
    return server
