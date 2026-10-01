import httpx

from housing_bot.ingest import canonical_url, extract_candidates, is_alert, listing_id, looks_like_listing, \
    might_be_message
from housing_bot.mail import Email


def test_canonical_url_strips_tracking_and_language():
    a = canonical_url("https://www.funda.nl/en/detail/huur/den-haag/appartement-x-36/43748353/?utm_source=mail#top")
    b = canonical_url("https://funda.nl/detail/huur/den-haag/appartement-x-36/43748353")
    assert a == b == "https://funda.nl/detail/huur/den-haag/appartement-x-36/43748353"
    assert listing_id(a) == listing_id(b)
    assert canonical_url("https://agency.nl/object?id=12&utm_medium=x") == "https://agency.nl/object?id=12"


def test_looks_like_listing():
    assert looks_like_listing("https://www.pararius.nl/appartement-te-huur/den-haag/2b0f7c3e/laan-van-meerdervoort")
    assert looks_like_listing("https://www.funda.nl/detail/huur/den-haag/appartement-lyonnetstraat-36/43748353/")
    assert looks_like_listing("https://www.somemakelaar.nl/aanbod/huur/den-haag/prinsegracht-12/123456")
    assert not looks_like_listing("https://www.pararius.nl/account/instellingen")
    assert not looks_like_listing("https://www.funda.nl/unsubscribe?token=abc123456")
    assert not looks_like_listing("https://www.facebook.com/funda/12345")


def test_extract_candidates_from_alert_email():
    html = """
    <table>
      <tr><td><a href="https://www.pararius.nl/appartement-te-huur/den-haag/2b0f7c3e/prinsegracht?utm_source=alert">
        Prinsegracht</a> € 1.350 per maand · 55 m² · 2 kamers</td></tr>
      <tr><td><a href="https://www.pararius.nl/appartement-te-huur/den-haag/2b0f7c3e/prinsegracht">photo</a></td></tr>
      <tr><td><a href="https://www.pararius.nl/account/instellingen">Instellingen</a></td></tr>
      <tr><td><a href="https://www.pararius.nl/unsubscribe/abc">Afmelden</a></td></tr>
    </table>"""
    mail = Email(folder="INBOX", uid="1", message_id="<a@b>", sender="noreply@pararius.nl", subject="Nieuwe woningen",
                 date=0, text="", html=html,
                 links=["https://www.pararius.nl/appartement-te-huur/den-haag/2b0f7c3e/prinsegracht?utm_source=alert",
                        "https://www.pararius.nl/appartement-te-huur/den-haag/2b0f7c3e/prinsegracht",
                        "https://www.pararius.nl/account/instellingen", "https://www.pararius.nl/unsubscribe/abc"])
    with httpx.Client() as client:
        found = extract_candidates(mail, client)
    assert len(found) == 1
    assert "1.350" in found[0].snippet and "55 m²" in found[0].snippet
    assert is_alert(mail, ["pararius.nl"])
    assert not is_alert(mail, ["funda.nl"])
    assert not might_be_message(mail)
    mail.subject = "Je hebt een nieuw bericht van de makelaar"
    assert might_be_message(mail)
