"""Notifications to your phone: WhatsApp (official Cloud API or CallMeBot), with email as backup."""

from __future__ import annotations

import logging
import re
import smtplib

import httpx

from .config import Config
from .mail import Mailbox

log = logging.getLogger(__name__)


def _param(text: str, limit: int) -> str:
    """WhatsApp template parameters can't contain newlines, tabs or 4+ spaces in a row, and can't be empty."""
    text = re.sub(r"[\r\n\t]+", " · ", text or "")
    text = re.sub(r" {2,}", " ", text).strip()
    return (text[: limit - 1] + "…" if len(text) > limit else text) or "-"


class Notifier:
    def __init__(self, cfg: Config, mailbox: Mailbox | None, client: httpx.Client | None = None):
        self.cfg = cfg
        self.mailbox = mailbox
        self.http = client or httpx.Client(timeout=20)

    def send(self, title: str, details: str = "", link: str = "", important: bool = False) -> bool:
        """Send to the configured channel. Important events are also emailed, so a WhatsApp hiccup can't hide them."""
        channel = self.cfg.secrets.notify_channel
        delivered = False
        try:
            if channel == "whatsapp_cloud":
                delivered = self._whatsapp_cloud(title, details, link)
            elif channel == "callmebot":
                delivered = self._callmebot(title, details, link)
        except httpx.HTTPError as e:
            log.warning("WhatsApp notification failed: %s", e)
        if channel == "email" or important or not delivered:
            delivered = self._email(title, details, link) or delivered
        return delivered

    def _whatsapp_cloud(self, title: str, details: str, link: str) -> bool:
        s = self.cfg.secrets
        if not (s.whatsapp_token and s.whatsapp_phone_number_id and s.whatsapp_to):
            return False
        response = self.http.post(
            f"https://graph.facebook.com/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {s.whatsapp_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": s.whatsapp_to,
                "type": "template",
                "template": {
                    "name": s.whatsapp_template,
                    "language": {"code": s.whatsapp_template_lang},
                    "components": [{"type": "body", "parameters": [
                        {"type": "text", "text": _param(title, 60)},
                        {"type": "text", "text": _param(details, 700)},
                        {"type": "text", "text": _param(link, 300)},
                    ]}],
                },
            },
        )
        if response.status_code >= 400:
            log.warning("WhatsApp API %s: %s", response.status_code, response.text[:300])
            return False
        return True

    def _callmebot(self, title: str, details: str, link: str) -> bool:
        s = self.cfg.secrets
        if not (s.callmebot_phone and s.callmebot_apikey):
            return False
        text = "\n".join(x for x in [f"*{title}*", details, link] if x)
        response = self.http.get("https://api.callmebot.com/whatsapp.php",
                                 params={"phone": s.callmebot_phone, "text": text, "apikey": s.callmebot_apikey})
        return response.status_code < 400

    def _email(self, title: str, details: str, link: str) -> bool:
        to = self.cfg.secrets.notify_email
        if not (to and self.mailbox):
            return False
        try:
            self.mailbox.send(to, f"[Housing bot] {title}", "\n\n".join(x for x in [details, link] if x))
            return True
        except (smtplib.SMTPException, OSError) as e:
            log.warning("email notification failed: %s", e)
            return False
