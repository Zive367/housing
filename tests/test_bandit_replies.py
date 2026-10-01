import random

from housing_bot import bandit
from housing_bot.mail import Email
from housing_bot.models import Listing, ReplyAnalysis, Status
from housing_bot.replies import match_listing, next_status


def test_bandit_learns_the_winning_style(store):
    bandit.ensure_defaults(store)
    for _ in range(30):
        store.record_outcome("en_warm", won=True)
        store.record_outcome("en_concise", won=False)
        store.record_outcome("nl_concise", won=False)
    rng = random.Random(1)
    picks = [bandit.choose(store, ["en", "nl"], rng)["name"] for _ in range(200)]
    assert picks.count("en_warm") > 180


def test_bandit_respects_languages(store):
    rng = random.Random(2)
    assert {bandit.choose(store, ["nl"], rng)["language"] for _ in range(20)} == {"nl"}


def test_status_precedence():
    assert next_status(Status.APPLIED, "viewing_confirmed") == Status.VIEWING_BOOKED
    assert next_status(Status.VIEWING_BOOKED, "auto_acknowledgement") is None
    assert next_status(Status.VIEWING_BOOKED, "question") is None
    assert next_status(Status.VIEWING_BOOKED, "viewing_cancelled") == Status.NEEDS_REPLY
    assert next_status(Status.VIEWING_BOOKED, "rejection") == Status.REJECTED
    assert next_status(Status.OFFER, "rejection") is None


def _mail(**kw) -> Email:
    base = dict(folder="INBOX", uid="1", message_id="<r@x>", sender="info@goedmakelaar.nl", subject="Re: woning",
                date=0, text="")
    return Email(**(base | kw))


def test_match_reply_by_thread_address_and_sender(store):
    a = Listing(id="a1", url="https://goedmakelaar.nl/aanbod/1", address="Prinsegracht 12, 2512GA 's-Gravenhage",
                address_key="2512GA|12", sent_message_id="<msg1@test>", applied_at=1, email="info@goedmakelaar.nl")
    b = Listing(id="b2", url="https://ander.nl/2", address="Spui 5, 2511BL 's-Gravenhage", address_key="2511BL|5",
                applied_at=2, email="verhuur@ander.nl")
    store.save(a)
    store.save(b)
    empty = ReplyAnalysis(category="question", summary="?", needs_action=True)
    assert match_listing(store, _mail(references=["<msg1@test>"]), empty).id == "a1"
    by_address = ReplyAnalysis(category="viewing_invitation", summary="kom kijken",
                               property_address="Spui 5, 2511 BL Den Haag", needs_action=True)
    assert match_listing(store, _mail(sender="someone@other.nl"), by_address).id == "b2"
    assert match_listing(store, _mail(sender="verhuur@ander.nl"), empty).id == "b2"
    assert match_listing(store, _mail(sender="random@gmail.com"), empty) is None
