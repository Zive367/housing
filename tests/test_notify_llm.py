from housing_bot.llm import LLM
from housing_bot.notify import Notifier
from tests.conftest import FakeMailbox


def test_notifications_are_emails_with_flag_for_important(cfg):
    cfg.secrets.notify_email = "me@example.com"
    box = FakeMailbox()
    notifier = Notifier(cfg, box)
    assert notifier.send("VIEWING BOOKED", "Spui 5 · Tue 18:30", "https://x.nl", important=True)
    assert notifier.send("Daily summary", "3 new")
    assert box.sent[0]["to"] == "me@example.com"
    assert box.sent[0]["subject"] == "[Housing bot] 🚩 VIEWING BOOKED"
    assert box.sent[0]["body"] == "Spui 5 · Tue 18:30\n\nhttps://x.nl"
    assert box.sent[1]["subject"] == "[Housing bot] Daily summary"


def test_without_notify_email_nothing_is_sent(cfg):
    box = FakeMailbox()
    assert not Notifier(cfg, box).send("x")
    assert box.sent == []


def test_page_reading_uses_haiku_without_opus_only_params(cfg, store):
    llm = LLM(cfg, store, client=object())
    assert llm.model_for(bulk=True) == "claude-haiku-4-5"
    assert llm._base_kwargs("claude-haiku-4-5", "low") == {"model": "claude-haiku-4-5"}
    opus = llm._base_kwargs(llm.model_for(bulk=False), "low")
    assert opus["fallbacks"] == "default" and opus["output_config"] == {"effort": "low"}


def test_gmail_only_downloads_housing_mail(cfg, store):
    from housing_bot.mail import Mailbox

    cfg.alerts.sender_domains = ["funda.nl", "pararius.nl"]
    cfg.secrets.bot_email = "sam@gmail.com"
    box = Mailbox(cfg, store)
    assert box.gmail_query() is None                          # no housing address: nothing to filter on
    cfg.secrets.housing_address = "sam+housing@gmail.com"
    assert box.gmail_query() == ("from:(funda.nl OR pararius.nl) OR to:sam+housing@gmail.com "
                                 "OR deliveredto:sam+housing@gmail.com")
    assert cfg.secrets.contact_email == "sam+housing@gmail.com"


def test_applications_ask_for_replies_on_housing_address(cfg, store, monkeypatch):
    import smtplib

    from housing_bot.mail import Mailbox

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def starttls(self, **k): sent.append("starttls")
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    cfg.secrets.bot_email, cfg.secrets.housing_address = "sam@gmail.com", "sam+housing@gmail.com"
    Mailbox(cfg, store).send("agent@makelaar.nl", "Reactie", "Beste...", from_name="Sam Jansen")
    assert sent[0] == "starttls"                               # default port 587 (Hetzner blocks 465)
    assert sent[1]["From"] == "Sam Jansen <sam@gmail.com>" and sent[1]["Reply-To"] == "sam+housing@gmail.com"
    sent.clear()
    cfg.secrets.smtp_port = 465
    Mailbox(cfg, store).send("agent@makelaar.nl", "Reactie", "Beste...")
    assert "starttls" not in sent and len(sent) == 1
