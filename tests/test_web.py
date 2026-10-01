import re

import httpx
import pytest

from housing_bot import bandit, web
from housing_bot.models import Listing, ListingDetails, Status
from housing_bot.pipeline import Context
from tests.conftest import FakeLLM, FakeMailbox, FakeNotifier


@pytest.fixture
def site(cfg, store):
    cfg.secrets.dashboard_password = "correct horse battery"
    ctx = Context(cfg, store, FakeLLM(), FakeMailbox(), None, FakeNotifier(), httpx.Client())
    bandit.ensure_defaults(store)
    d = ListingDetails(is_rental_listing=True, size_m2=55, furnished="furnished")
    store.save(Listing(id="m1", url="https://www.pararius.nl/appartement-te-huur/den-haag/abc12345/x",
                       status=Status.MANUAL_APPLY, address="Prinsegracht 12, Den Haag", total_rent=1350,
                       bike_minutes=9, details=d, message="Beste verhuurder, <b>graag</b> een bezichtiging.",
                       variant="nl_concise", phone="+31703456789", call_needed=True, call_reason="Phone only"))
    store.save(Listing(id="v1", url="https://agency.nl/1", status=Status.VIEWING_INVITED, address="Spui 5",
                       viewing_link="https://planner.example/slot", applied_at=1, variant="en_warm",
                       last_reply="Kies een tijdslot"))
    store.save(Listing(id="x1", url="javascript:alert(1)", status=Status.APPLIED,
                       address="<script>alert('x')</script>", applied_at=1))
    store.save(Listing(id="f1", url="https://agency.nl/2", status=Status.FILTERED, address="Far away 1",
                       filter_reasons=["Too far from work (20 km)"]))
    server = web.serve(ctx, port=0, host="127.0.0.1")
    client = httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", follow_redirects=False)
    yield ctx, client
    server.shutdown()


def login(client) -> str:
    response = client.post("/login", data={"password": "correct horse battery"})
    assert response.status_code == 303 and "HttpOnly" in response.headers["set-cookie"]
    client.cookies.set(web.COOKIE, response.cookies[web.COOKIE])
    page = client.get("/").text
    return re.search(r"name=t value='([0-9a-f]+)'", page).group(1)


def test_login_required_and_wrong_password_rejected(site):
    _, client = site
    assert "type=password" in client.get("/").text
    assert web.COOKIE not in client.post("/login", data={"password": "nope"}).cookies
    assert client.post("/act", data={"id": "m1", "action": "applied"}).status_code == 403


def test_dashboard_puts_actionable_items_first_and_escapes_scraped_text(site):
    _, client = site
    login(client)
    page = client.get("/").text
    now = page.split("<section id=now>")[1].split("</section>")[0]
    assert now.index("Spui 5") < now.index("Prinsegracht 12")          # book viewing before apply yourself
    assert "Apply: copy message" in now and "Pick a viewing slot" in now and "tel:+31703456789" in now
    assert "&lt;b&gt;graag&lt;/b&gt;" in page and "<b>graag</b>" not in page
    assert "<script>alert('x')</script>" not in page and "javascript:alert" not in page
    assert "Too far from work" in page                                 # skipped section shows why


def test_actions_update_status_learning_and_require_csrf(site):
    ctx, client = site
    token = login(client)
    assert client.post("/act", data={"id": "m1", "action": "applied", "t": "bad"}).status_code == 403
    assert client.post("/act", data={"id": "m1", "action": "applied", "t": token}).status_code == 303
    applied = ctx.store.get("m1")
    assert applied.status == Status.APPLIED and applied.apply_method == "you (dashboard)" and applied.applied_at

    client.post("/act", data={"id": "v1", "action": "booked", "value": "di 7 okt 18:30", "t": token})
    booked = ctx.store.get("v1")
    assert booked.status == Status.VIEWING_BOOKED and booked.viewing_at == "di 7 okt 18:30" and booked.outcome == "win"

    client.post("/act", data={"id": "m1", "action": "called", "t": token})
    assert not ctx.store.get("m1").call_needed
    assert client.post("/act", data={"id": "m1", "action": "delete-everything", "t": token}).status_code == 400
