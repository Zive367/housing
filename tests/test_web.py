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


def test_call_now_on_every_card_with_a_number(site):
    _, client = site
    login(client)
    page = client.get("/").text
    assert "href='tel:+31703456789'" in page and "Call now" in page
    assert "Find number" in page                                       # no number known: search for it


def test_settings_page_saves_to_file_and_applies_immediately(site, tmp_path, monkeypatch):
    import json

    from housing_bot import config as config_module
    ctx, client = site
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)
    token = login(client)
    page = client.get("/settings").text
    assert "Alex" in page and "Sam" in page and "BOT_EMAIL_APP_PASSWORD" not in page

    form = {"t": token, "applicant.first_name": "Sam", "applicant.last_name": "Jansen",
            "secrets.applicant_phone": "06 23456789", "applicant.age": "30", "applicant.job_title": "analyst",
            "applicant.employer": "", "applicant.contract": "permanent", "applicant.nationality": "",
            "applicant.about": "quiet", "applicant.gross_monthly_income": "4600",
            "partner.first_name": "Alex", "partner.age": "29", "partner.job_title": "UX designer",
            "partner.contract": "permanent", "partner.gross_monthly_income": "3800",
            "search.max_rent": "1550", "search.preferred_rent": "1400", "search.min_size_m2_solo": "25",
            "search.min_size_m2_couple": "40", "search.max_distance_km": "12",
            "search.municipalities": ["'s-Gravenhage", "Delft"], "search.prefer_furnished": ["off", "on"],
            "search.require_registration": ["off", "on"], "search.exclude_rooms": "off",
            "search.min_neighbourhood_score": "", "apply.languages": ["nl"], "apply.dry_run": ["off", "on"],
            "llm.daily_budget_usd": "5"}
    response = client.post("/settings", data=form)
    assert response.status_code == 303
    cfg = ctx.cfg
    assert cfg.partner.job_title == "UX designer" and cfg.search.max_rent == 1550
    assert cfg.secrets.applicant_phone == "+31623456789" and cfg.search.municipalities == ["'s-Gravenhage", "Delft"]
    assert cfg.search.exclude_rooms is False and cfg.apply.dry_run is True and cfg.apply.languages == ["nl"]
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["partner.gross_monthly_income"] == 3800

    bad = dict(form, **{"search.max_rent": "lots", "secrets.applicant_phone": "123"})
    response = client.post("/settings", data=bad)
    assert response.status_code == 400 and "not a number" in response.text and "not a valid phone" in response.text
    assert cfg.search.max_rent == 1550                                  # nothing changed on error
    assert client.post("/settings", data=dict(form, t="bad")).status_code == 403


def test_saved_settings_override_config_at_startup(cfg, tmp_path, monkeypatch):
    import json

    from housing_bot import config as config_module, settings
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"search.max_rent": 1600, "partner.first_name": "Somi",
                                                        "secrets.anthropic_api_key": "stolen"}))
    settings.load(cfg)
    assert cfg.search.max_rent == 1600 and cfg.partner.first_name == "Somi"
    assert cfg.secrets.anthropic_api_key == ""                          # secrets can't be set from the file


def test_password_guessing_is_locked_out_and_reported(site, monkeypatch):
    ctx, client = site
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    for _ in range(5):
        assert client.post("/login", data={"password": "guess"}).status_code == 303
    locked = client.post("/login", data={"password": "correct horse battery"})
    assert locked.status_code == 429 and "Too many wrong passwords" in locked.text
    other = client.post("/login", data={"password": "correct horse battery"},
                        headers={"X-Forwarded-For": "203.0.113.9"})
    assert other.status_code == 303 and web.COOKIE in other.cookies   # other people aren't locked out
    for n in range(5):
        client.post("/login", data={"password": "x"}, headers={"X-Forwarded-For": f"198.51.100.{n}"})
    assert any("guessing" in m["title"] for m in ctx.notifier.sent)   # 10 failures -> email


def test_public_mode_needs_long_password_and_secure_cookie(cfg, store):
    cfg.secrets.dashboard_public, cfg.secrets.dashboard_password = True, "short-pass"
    ctx = Context(cfg, store, FakeLLM(), FakeMailbox(), None, FakeNotifier(), httpx.Client())
    with pytest.raises(ValueError):
        web.serve(ctx, port=0, host="127.0.0.1")
    cfg.secrets.dashboard_password = "a much longer passphrase"
    server = web.serve(ctx, port=0, host="127.0.0.1")
    try:
        r = httpx.post(f"http://127.0.0.1:{server.server_port}/login", data={"password": "a much longer passphrase"})
        assert "Secure" in r.headers["set-cookie"] and "max-age" in r.headers["strict-transport-security"]
    finally:
        server.shutdown()
