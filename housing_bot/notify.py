"""Notifications by email to your personal address. Turn on phone notifications for that inbox."""

from __future__ import annotations

import logging
import smtplib

from .config import Config
from .mail import Mailbox

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Config, mailbox: Mailbox | None):
        self.cfg = cfg
        self.mailbox = mailbox

    def send(self, title: str, details: str = "", link: str = "", important: bool = False) -> bool:
        """Important events get a 🚩 in the subject, so a Gmail filter can star them / mark them important."""
        to = self.cfg.secrets.notify_email
        if not (to and self.mailbox):
            log.warning("notification not sent (NOTIFY_EMAIL or mailbox missing): %s", title)
            return False
        subject = f"[Housing bot] {'🚩 ' if important else ''}{title}"
        try:
            self.mailbox.send(to, subject, "\n\n".join(x for x in [details, link] if x))
            return True
        except (smtplib.SMTPException, OSError) as e:
            log.warning("email notification failed: %s", e)
            return False
