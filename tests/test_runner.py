import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from housing_bot import bandit, runner
from housing_bot.ingest import Candidate
from housing_bot.mail import Email
from housing_bot.models import Listing, Status
from housing_bot.pipeline import Context
from tests.conftest import FakeLLM, FakeMailbox, FakeNotifier


def make_ctx(cfg, store) -> Context:
    return Context(cfg, store, FakeLLM(), FakeMailbox(), None, FakeNotifier(), httpx.Client())


def test_no_reply_after_days_flags_a_call(cfg, store):
    ctx = make_ctx(cfg, store)
    bandit.ensure_defaults(store)
    old = Listing(id="o1", url="https://a.nl/1", status=Status.APPLIED, applied_at=time.time() - 5 * 86400,
                  phone="+31703456789", variant="en_concise", address="Spui 5")
    fresh = Listing(id="f1", url="https://a.nl/2", status=Status.APPLIED, applied_at=time.time() - 3600)
    store.save(old)
    store.save(fresh)
    runner.sweep_no_response(ctx)
    swept = store.get("o1")
    assert swept.status == Status.NO_RESPONSE and swept.call_needed and swept.outcome == "loss"
    assert store.get("f1").status == Status.APPLIED
    assert next(v for v in store.variants() if v["name"] == "en_concise")["losses"] == 1
    assert "Call: Spui 5" in runner.digest_text(ctx, 0)


def test_mail_routing(cfg, store, monkeypatch):
    cfg.alerts.sender_domains = ["stekkies.com"]
    ctx = make_ctx(cfg, store)
    seen = {"candidates": [], "replies": []}
    monkeypatch.setattr(runner, "extract_candidates",
                        lambda mail, client: [Candidate("https://pararius.nl/appartement-te-huur/den-haag/abcdef12/x",
                                                        "€ 1.300")])
    monkeypatch.setattr(runner, "process_candidate", lambda c, url, src, snip: seen["candidates"].append((url, src)))
    monkeypatch.setattr(runner, "process_reply", lambda c, mail: seen["replies"].append(mail.sender))

    def mail(sender, subject="Nieuwe woningen"):
        return Email(folder="INBOX", uid="1", message_id=f"<{sender}>", sender=sender, subject=subject, date=0, text="")

    with ThreadPoolExecutor(max_workers=2) as pool:
        runner.handle_mail(ctx, pool, mail("alerts@stekkies.com"))
        runner.handle_mail(ctx, pool, mail("info@goedmakelaar.nl", "Uitnodiging bezichtiging"))
        time.sleep(0.5)
    assert seen["candidates"] == [("https://pararius.nl/appartement-te-huur/den-haag/abcdef12/x", "stekkies.com")]
    assert seen["replies"] == ["info@goedmakelaar.nl"]
    assert store.email_seen("<alerts@stekkies.com>") and store.kv_get("last_alert")
