"""End-to-end flow with Claude, the network and the mailbox replaced by fakes."""

import httpx
import pytest

from housing_bot import pipeline
from housing_bot.compose import Composed
from housing_bot.fetch import parse_html
from housing_bot.geo import Place
from housing_bot.mail import Email
from housing_bot.models import ListingDetails, ReplyAnalysis, ScamVerdict, Status
from tests.conftest import FakeLLM, FakeMailbox, FakeNotifier
from tests.test_fetch_mail import LISTING_HTML

URL = "https://goedmakelaar.nl/aanbod/huur/den-haag/prinsegracht-12/123456"


def details(**overrides) -> ListingDetails:
    base = dict(is_rental_listing=True, still_available=True, street="Prinsegracht", house_number="12",
                city="Den Haag", rent_eur=1350, size_m2=55, furnished="furnished", registration_allowed="yes",
                landlord_type="agency", agency_name="Goed Makelaar", agent_name="Petra",
                agent_phones=["070 345 67 89"], agent_emails=["verhuur@goedmakelaar.nl"],
                application_method="email", language="nl", highlights=["balcony", "near Den Haag HS"])
    return ListingDetails(**(base | overrides))


@pytest.fixture
def ctx(cfg, store, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch", lambda url, client: parse_html(url, LISTING_HTML))
    monkeypatch.setattr(pipeline, "locate", lambda *a: Place(
        display="Prinsegracht 12, 2512GA 's-Gravenhage", kind="adres", street="Prinsegracht", number="12",
        postcode="2512GA", city="'s-Gravenhage", municipality="'s-Gravenhage", buurt="Kortenbos",
        buurt_code="BU0", lat=52.0755, lon=4.3065, verified=True))
    monkeypatch.setattr(pipeline, "neighbourhood_score", lambda *a: 6.5)
    # Extraction is covered in test_schemas; here the fake hands out ready ListingDetails.
    monkeypatch.setattr(pipeline, "extract", lambda llm, page, snippet: llm.parse(ListingDetails, "", ""))
    return pipeline.Context(cfg, store, FakeLLM(), FakeMailbox(), None, FakeNotifier(), httpx.Client())


def queue(ctx, scam_score=10, **detail_overrides):
    ctx.llm.add(details(**detail_overrides))
    ctx.llm.add(ScamVerdict(score=scam_score, reasons=["test"]))
    ctx.llm.add(Composed(subject="Reactie op Prinsegracht 12", message="Beste Petra, ..."))


def test_dry_run_writes_but_does_not_send(ctx):
    queue(ctx)
    listing = pipeline.process_candidate(ctx, URL, "pararius.nl")
    assert listing.status == Status.READY
    assert listing.message == "Beste Petra, ..."
    assert ctx.mailbox.sent == []
    assert listing.address_key == "2512GA|12" and listing.bike_minutes is not None
    assert listing.phone == "+31703456789" and listing.email == "verhuur@goedmakelaar.nl"
    assert pipeline.process_candidate(ctx, URL, "pararius.nl") is None   # never processed twice


def test_live_apply_then_viewing_reply(ctx):
    ctx.cfg.apply.dry_run = False
    queue(ctx)
    listing = pipeline.process_candidate(ctx, URL, "pararius.nl")
    assert listing.status == Status.APPLIED and listing.apply_method == "email"
    sent = ctx.mailbox.sent[0]
    assert sent["to"] == "verhuur@goedmakelaar.nl" and sent["body"] == "Beste Petra, ..."
    assert sent["from_name"] == "Sam Jansen"
    assert listing.sent_message_id == "<msg1@test>"

    ctx.llm.add(ReplyAnalysis(category="viewing_confirmed", summary="Viewing confirmed",
                              viewing_datetime="2026-10-07T18:30", needs_action=False))
    reply = Email(folder="INBOX", uid="9", message_id="<r1@x>", sender="verhuur@goedmakelaar.nl",
                  subject="Re: Reactie op Prinsegracht 12", date=0, text="Tot dinsdag!", references=["<msg1@test>"])
    pipeline.process_reply(ctx, reply)
    updated = ctx.store.get(listing.id)
    assert updated.status == Status.VIEWING_BOOKED and updated.viewing_at == "2026-10-07T18:30"
    assert updated.flagged and updated.outcome == "win"
    alert = ctx.notifier.sent[-1]
    assert alert["title"] == "🚩 VIEWING BOOKED" and alert["important"]
    assert ctx.store.variants()[0]["wins"] + ctx.store.variants()[1]["wins"] + ctx.store.variants()[2]["wins"] == 1


def test_likely_scam_is_never_applied(ctx):
    ctx.cfg.apply.dry_run = False
    ctx.llm.add(details())
    ctx.llm.add(ScamVerdict(score=85, reasons=["owner abroad"]))
    listing = pipeline.process_candidate(ctx, URL, "pararius.nl")
    assert listing.status == Status.LIKELY_SCAM and ctx.mailbox.sent == []


def test_over_budget_is_filtered_without_scam_check(ctx):
    ctx.llm.add(details(rent_eur=1750))
    listing = pipeline.process_candidate(ctx, URL, "pararius.nl")
    assert listing.status == Status.FILTERED and "above max" in listing.filter_reasons[0]


def test_same_home_via_second_site_is_duplicate(ctx):
    queue(ctx)
    pipeline.process_candidate(ctx, URL, "pararius.nl")
    ctx.llm.add(details())
    second = pipeline.process_candidate(ctx, "https://www.funda.nl/detail/huur/den-haag/appartement-prinsegracht-12/999",
                                        "funda.nl")
    assert second.status == Status.DUPLICATE


def test_phone_only_listing_needs_a_call(ctx):
    ctx.cfg.apply.dry_run = False
    queue(ctx, agent_emails=[], application_method="phone")
    listing = pipeline.process_candidate(ctx, URL, "pararius.nl")
    assert listing.status == Status.MANUAL_APPLY and listing.call_needed
    assert ctx.notifier.sent[-1]["important"]
