"""Phone-friendly dashboard: what needs you first, one tap to act. Served by the bot itself (stdlib only).

Security: password login with a signed cookie, every page escapes scraped text, actions are POST-only with a
CSRF token, and docker-compose publishes the port on localhost only (reach it through Tailscale).
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
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from .models import Listing, Status
from .pipeline import Context, record_outcome

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")
COOKIE = "hb_session"
SESSION_DAYS = 90

# What needs you, most urgent first.
DO_NOW = [Status.VIEWING_INVITED, Status.MANUAL_APPLY, Status.DOCS_REQUESTED, Status.NEEDS_REPLY]
LABELS = {
    Status.VIEWING_INVITED: ("Book viewing", "urgent"), Status.MANUAL_APPLY: ("Apply yourself", "urgent"),
    Status.DOCS_REQUESTED: ("Send documents", "warn"), Status.NEEDS_REPLY: ("Reply needed", "warn"),
    Status.VIEWING_BOOKED: ("Viewing booked", "good"), Status.OFFER: ("Offer", "good"),
    Status.READY: ("Ready (dry run)", "info"), Status.APPLIED: ("Applied", "info"),
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


def e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(url: str) -> str:
    return url if urlsplit(url or "").scheme in ("http", "https") else ""


def when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d %b %H:%M") if ts else ""


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
    do_now = sorted((x for x in newest if x.status in urgency or (x.call_needed and x.status not in {
        Status.CLOSED, Status.REJECTED, Status.FILTERED, Status.LIKELY_SCAM, Status.DUPLICATE, Status.UNAVAILABLE})),
        key=lambda x: (urgency.get(x.status, len(DO_NOW)), -x.first_seen))
    viewings = sorted((x for x in newest if x.status in (Status.VIEWING_BOOKED, Status.OFFER)),
                      key=lambda x: x.viewing_at or "9999")
    ready = [x for x in newest if x.status == Status.READY and x not in do_now]
    applied = [x for x in newest if x.status in (Status.APPLIED, Status.NO_RESPONSE) and x not in do_now][:60]
    skipped = [x for x in newest if x.status in (Status.FILTERED, Status.LIKELY_SCAM, Status.REJECTED, Status.CLOSED,
                                                 Status.FAILED) and x not in do_now][:60]
    return [("now", "Do now", do_now, True), ("viewings", "Viewings", viewings, True),
            ("ready", "Ready to send (dry run)", ready, True), ("applied", "Applied", applied, False),
            ("skipped", "Skipped & closed", skipped, False)]


def card(listing: Listing, token: str) -> str:
    d = listing.details
    label, tone = LABELS.get(listing.status, (listing.status.value, "info"))
    facts = [f"€{listing.total_rent:,.0f}" if listing.total_rent else "",
             f"{d.size_m2:.0f} m²" if d and d.size_m2 else "",
             f"🚲 {listing.bike_minutes} min" if listing.bike_minutes is not None else "",
             f"🚶 {listing.walk_minutes} min" if listing.walk_minutes is not None else "",
             {"furnished": "furnished", "upholstered": "upholstered", "unfurnished": "unfurnished"}.get(
                 d.furnished if d else "", ""),
             f"area {listing.neighbourhood_score}/10" if listing.neighbourhood_score is not None else "",
             "couple" if listing.apply_as == "couple" else ""]
    chips = "".join(f"<span class=chip>{e(f)}</span>" for f in facts if f)
    if listing.scam_score is not None and listing.scam_score >= 40:
        chips += f"<span class='chip bad'>scam risk {listing.scam_score}</span>"

    lines = []
    if listing.status == Status.VIEWING_INVITED and listing.last_reply:
        lines.append(e(listing.last_reply))
    if listing.viewing_at:
        lines.append(f"📅 <b>{e(listing.viewing_at.replace('T', ' '))}</b>")
    if listing.call_needed:
        lines.append(f"📞 {e(listing.call_reason or 'Call the agent')}")
    if listing.status in (Status.DOCS_REQUESTED, Status.NEEDS_REPLY) and listing.last_reply:
        lines.append(e(listing.last_reply))
    if listing.status in (Status.FILTERED, Status.LIKELY_SCAM):
        lines.append(e("; ".join(listing.filter_reasons or listing.scam_reasons)))
    if listing.apply_error and listing.status == Status.MANUAL_APPLY:
        lines.append(f"<span class=muted>{e(listing.apply_error)}</span>")
    contact = " · ".join(x for x in [listing.agency, listing.agent] if x)

    buttons = []
    url = safe_url(listing.url)
    if listing.message and listing.status in (Status.MANUAL_APPLY, Status.READY, Status.FAILED):
        buttons.append(f"<button class='btn primary' data-copy='{e(listing.message)}' data-open='{e(url)}'>"
                       "Apply: copy message &amp; open</button>")
    elif url:
        buttons.append(f"<a class='btn' href='{e(url)}' target=_blank rel=noopener>Open listing</a>")
    if listing.viewing_link and safe_url(listing.viewing_link):
        buttons.append(f"<a class='btn primary' href='{e(listing.viewing_link)}' target=_blank rel=noopener>"
                       "Pick a viewing slot</a>")
    if listing.phone:
        buttons.append(f"<a class='btn' href='tel:{e(listing.phone)}'>Call {e(listing.phone)}</a>")
    forms = []
    for action, (text, statuses) in ACTIONS.items():
        if listing.status in statuses:
            extra = ("<input name=value placeholder='date &amp; time (optional)'>"
                     if action == "booked" else "")
            forms.append(f"<form method=post action='/act'><input type=hidden name=id value='{e(listing.id)}'>"
                         f"<input type=hidden name=action value={action}><input type=hidden name=t value='{token}'>"
                         f"{extra}<button class='btn {'ghost' if action in ('skip', 'rejected') else ''}'>"
                         f"{e(text)}</button></form>")
    if listing.call_needed:
        forms.append(f"<form method=post action='/act'><input type=hidden name=id value='{e(listing.id)}'>"
                     f"<input type=hidden name=action value=called><input type=hidden name=t value='{token}'>"
                     "<button class=btn>Called</button></form>")
    message = (f"<details><summary>Message</summary><pre>{e(listing.message)}</pre></details>"
               if listing.message else "")
    return f"""<article class='card {tone}'>
  <div class=top><span class='pill {tone}'>{e(label)}</span><span class=muted>{e(when(listing.first_seen))}</span></div>
  <h3>{e(listing.address or listing.domain)}</h3>
  <div class=chips>{chips}</div>
  {''.join(f'<p>{x}</p>' for x in lines)}
  {f'<p class=muted>{e(contact)}</p>' if contact else ''}
  <div class=actions>{''.join(buttons)}{''.join(forms)}</div>
  {message}
</article>"""


CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--muted:#77756f;--line:#e6e3dc;--accent:#2f6f4f;
--urgent:#c2410c;--warn:#b7791f;--good:#2f6f4f;--info:#3b5b8c;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1e1c;--ink:#eceae4;--muted:#9c998f;
--line:#33312d;--accent:#5fb08a;--urgent:#f08a4b;--warn:#e0b04f;--good:#5fb08a;--info:#8fb0e0;--bad:#f07a6e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:14px 16px 8px;border-bottom:1px solid var(--line);z-index:2}
header h1{margin:0;font-size:20px}header p{margin:2px 0 0;color:var(--muted);font-size:13px}
nav{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px}nav a{white-space:nowrap;text-decoration:none;
color:var(--ink);border:1px solid var(--line);border-radius:99px;padding:4px 12px;font-size:14px}
main{padding:8px 16px 40px;max-width:720px;margin:auto}h2{font-size:16px;margin:22px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--info);border-radius:12px;
padding:12px 14px;margin:10px 0}.card.urgent{border-left-color:var(--urgent)}.card.warn{border-left-color:var(--warn)}
.card.good{border-left-color:var(--good)}.card.bad{border-left-color:var(--bad)}.card.muted{border-left-color:var(--line)}
.card h3{margin:6px 0;font-size:17px}.card p{margin:6px 0}.top{display:flex;justify-content:space-between;font-size:13px}
.pill{font-weight:600}.pill.urgent{color:var(--urgent)}.pill.warn{color:var(--warn)}.pill.good{color:var(--good)}
.pill.info{color:var(--info)}.pill.bad{color:var(--bad)}.pill.muted,.muted{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{background:var(--bg);border-radius:6px;padding:2px 8px;font-size:13px}
.chip.bad{color:var(--bad)}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.actions form{display:flex;gap:6px}
.btn{font:inherit;font-size:15px;border:1px solid var(--line);background:var(--card);color:var(--ink);
border-radius:10px;padding:9px 14px;text-decoration:none;cursor:pointer;min-height:42px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}.btn.ghost{color:var(--muted)}
input{font:inherit;font-size:15px;border:1px solid var(--line);border-radius:10px;padding:8px;background:var(--card);
color:var(--ink);max-width:180px}details summary{cursor:pointer;color:var(--muted);font-size:14px;margin-top:8px}
pre{white-space:pre-wrap;font:inherit;font-size:14px;background:var(--bg);padding:10px;border-radius:8px}
.empty{color:var(--muted);padding:8px 0}.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:10px;display:none}
.login{max-width:340px;margin:20vh auto;padding:0 16px}.login input{max-width:none;width:100%;margin:8px 0}
.login .btn{width:100%}
"""

SCRIPT = """
document.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(b.dataset.copy); toast('Message copied, paste it in the form'); }
  catch (err) { toast('Copy failed: open "Message" below and copy it'); }
  if (b.dataset.open) window.open(b.dataset.open, '_blank', 'noopener');
}));
function toast(text){const t=document.querySelector('.toast');t.textContent=text;t.style.display='block';
  setTimeout(()=>t.style.display='none',3500);}
"""


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes><meta name=theme-color content="#2f6f4f">
<link rel=manifest href=/manifest.json><title>{e(title)}</title><style>{CSS}</style></head>
<body>{body}<div class=toast></div><script>{SCRIPT}</script></body></html>"""


def dashboard(ctx: Context, token: str) -> str:
    listings = ctx.store.all()
    groups = sections(listings)
    mode = "dry run: nothing is sent" if ctx.cfg.apply.dry_run else "live"
    beat = float(ctx.store.kv_get("heartbeat") or 0)
    health = "running" if time.time() - beat < 300 else "⚠ bot not running"
    nav = "".join(f"<a href='#{gid}'>{e(title)} ({len(items)})</a>" for gid, title, items, _ in groups if items)
    parts = []
    for gid, title, items, is_open in groups:
        if not items and gid not in ("now",):
            continue
        cards = "".join(card(x, token) for x in items) or "<p class=empty>Nothing needs you right now.</p>"
        if is_open:
            parts.append(f"<section id={gid}><h2>{e(title)}</h2>{cards}</section>")
        else:
            parts.append(f"<section id={gid}><details><summary><h2 style='display:inline'>{e(title)} "
                         f"({len(items)})</h2></summary>{cards}</details></section>")
    today = sum(1 for x in listings if x.first_seen > time.time() - 86400)
    return page("Housing", f"""<header><h1>Housing</h1>
<p>{e(health)} · {e(mode)} · {today} new today · Claude ${ctx.store.spend_today():.2f} today</p>
<nav>{nav}</nav></header><main>{''.join(parts)}</main>""")


MANIFEST = json.dumps({
    "name": "Housing", "short_name": "Housing", "start_url": "/", "display": "standalone",
    "background_color": "#f6f5f2", "theme_color": "#2f6f4f",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
})
ICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' "
        "fill='#2f6f4f'/><path d='M14 32 32 16l18 16v18H38V38H26v12H14z' fill='#fff'/></svg>")


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
    class Handler(BaseHTTPRequestHandler):
        server_version = "housing"

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
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, where: str, headers=None) -> None:
            self.send_response(303)
            self.send_header("Location", where)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict[str, str]:
            length = min(int(self.headers.get("Content-Length") or 0), 10000)
            raw = self.rfile.read(length).decode(errors="replace")
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/manifest.json":
                return self._send(200, MANIFEST, "application/manifest+json")
            if path == "/icon.svg":
                return self._send(200, ICON, "image/svg+xml")
            if path == "/health":
                return self._send(200, "ok", "text/plain")
            session = self._session()
            if not auth.valid(session):
                return self._send(200, page("Log in", """<form class=login method=post action=/login>
<h1>Housing</h1><input type=password name=password placeholder=Password autofocus autocomplete=current-password>
<button class='btn primary'>Log in</button></form>"""))
            if path == "/":
                return self._send(200, dashboard(ctx, auth.csrf(session)))
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            path = urlsplit(self.path).path
            form = self._form()
            if path == "/login":
                if auth.password and hmac.compare_digest(form.get("password", ""), auth.password):
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                    return self._redirect("/", {"Set-Cookie": f"{COOKIE}={auth.new_session()}; Path=/; HttpOnly; "
                                                              f"SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"})
                time.sleep(1.5)  # slow down guessing
                return self._redirect("/")
            session = self._session()
            if not auth.valid(session) or not hmac.compare_digest(form.get("t", ""), auth.csrf(session)):
                return self._send(403, "forbidden", "text/plain")
            if path == "/act":
                listing = ctx.store.get(form.get("id", ""))
                if listing is None:
                    return self._send(404, "listing not found", "text/plain")
                try:
                    apply_action(ctx, listing, form.get("action", ""), form.get("value", "")[:60])
                except ValueError:
                    return self._send(400, "unknown action", "text/plain")
                return self._redirect("/#now")
            self._send(404, "not found", "text/plain")

    return Handler


def serve(ctx: Context, port: int = 8080, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Start the dashboard in a background thread. Returns the server (call .shutdown() to stop)."""
    if not ctx.cfg.secrets.dashboard_password:
        raise ValueError("DASHBOARD_PASSWORD is not set in .env")
    server = ThreadingHTTPServer((host, port), make_handler(ctx, Auth(ctx)))
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    log.info("dashboard on http://%s:%s", host, server.server_port)
    return server
