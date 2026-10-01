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
