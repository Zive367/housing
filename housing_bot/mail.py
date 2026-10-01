"""Bot mailbox: poll IMAP for alert emails and agent replies, send mail over SMTP."""

from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

# Gmail puts some legit mail in spam; viewing invites must never be missed there.
FOLDERS = ("INBOX", "[Gmail]/Spam")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


@dataclass
class Email:
    folder: str
    uid: str
    message_id: str
    sender: str
    subject: str
    date: float
    text: str
    html: str = ""
    links: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)   # In-Reply-To + References message ids

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower() if "@" in self.sender else ""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_email(raw: bytes, folder: str = "INBOX", uid: str = "") -> Email:
    msg = email.message_from_bytes(raw, policy=default_policy)
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            continue
        if ctype == "text/plain":
            text_parts.append(content)
        elif ctype == "text/html":
            html_parts.append(content)
    html = "\n".join(html_parts)
    text = "\n".join(text_parts) or (html_to_text(html) if html else "")

    links: list[str] = []
    if html:
        soup = BeautifulSoup(html, "lxml")
        links = [a["href"].strip() for a in soup.find_all("a", href=True)]
    links += URL_RE.findall(text)
    seen: set[str] = set()
    links = [u for u in links if u.startswith("http") and not (u in seen or seen.add(u))]

    try:
        sent = parsedate_to_datetime(msg["Date"]).timestamp() if msg["Date"] else time.time()
    except (TypeError, ValueError):
        sent = time.time()
    message_id = (msg["Message-ID"] or "").strip() or f"<{folder}-{uid}@local>"
    references = re.findall(r"<[^>]+>", f"{msg['In-Reply-To'] or ''} {msg['References'] or ''}")
    return Email(folder=folder, uid=uid, message_id=message_id,
                 sender=parseaddr(msg["From"] or "")[1].lower(), subject=str(msg["Subject"] or ""),
                 date=sent, text=text, html=html, links=links, references=references)


class Mailbox:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    def gmail_query(self) -> str | None:
        """Gmail-side filter so only housing mail is ever downloaded: alerts from the housing sites and anything
        sent to the housing address. None (no filter) only when no separate housing address is configured."""
        s = self.cfg.secrets
        if "gmail" not in s.imap_host or not s.housing_address:
            return None
        sites = " OR ".join(self.cfg.alerts.sender_domains)
        return f"from:({sites}) OR to:{s.housing_address} OR deliveredto:{s.housing_address}"

    def fetch_new(self) -> list[Email]:
        """New messages since the last poll, across inbox and spam."""
        s = self.cfg.secrets
        found: list[Email] = []
        with imaplib.IMAP4_SSL(s.imap_host, timeout=60) as imap:
            imap.login(s.bot_email, s.bot_email_app_password)
            for folder in FOLDERS:
                try:
                    found += self._fetch_folder(imap, folder)
                except imaplib.IMAP4.error as e:
                    log.debug("skipping folder %s: %s", folder, e)
        return [m for m in found if m.sender != s.bot_email.lower()]

    def _fetch_folder(self, imap: imaplib.IMAP4_SSL, folder: str) -> list[Email]:
        status, _ = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            return []
        validity = (imap.response("UIDVALIDITY")[1][0] or b"").decode()
        key = f"imap:{folder}"
        prev_validity, _, prev_uid = (self.store.kv_get(key) or "").partition(":")
        fresh_start = prev_validity != validity or not prev_uid
        if fresh_start:
            # First run (or mailbox reset): only look at the last day, don't replay history.
            last = 0
            since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            criteria = ["SINCE", since]
        else:
            last = int(prev_uid)
            criteria = ["UID", f"{last + 1}:*"]
        query = self.gmail_query()
        if query:
            criteria += ["X-GM-RAW", '"' + query.replace('"', "") + '"']
        status, data = imap.uid("SEARCH", None, *criteria)
        uids = [int(u) for u in data[0].split()] if status == "OK" and data and data[0] else []
        uids = [u for u in uids if u > last]  # "N:*" always returns the newest UID, even if < N

        messages: list[Email] = []
        max_uid = last
        for uid in uids:
            status, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            max_uid = max(max_uid, uid)
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            mail = parse_email(parts[0][1], folder, str(uid))
            if not self.store.email_seen(mail.message_id):
                messages.append(mail)
        if fresh_start and not uids:
            status, data = imap.status(f'"{folder}"', "(UIDNEXT)")
            match = re.search(rb"UIDNEXT (\d+)", data[0] if status == "OK" and data else b"")
            max_uid = int(match.group(1)) - 1 if match else 0
        self.store.kv_set(key, f"{validity}:{max_uid}")
        return messages

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None,
             from_name: str = "") -> str:
        s = self.cfg.secrets
        msg = EmailMessage()
        msg["From"] = formataddr((from_name, s.bot_email)) if from_name else s.bot_email
        msg["To"] = to
        if s.housing_address and s.housing_address != s.bot_email:
            msg["Reply-To"] = s.housing_address
        msg["Subject"] = subject
        message_id = make_msgid(domain=s.bot_email.rsplit("@", 1)[-1] if "@" in s.bot_email else None)
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            smtp.login(s.bot_email, s.bot_email_app_password)
            smtp.send_message(msg)
        return message_id
