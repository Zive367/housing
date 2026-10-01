from email.message import EmailMessage

from housing_bot.fetch import normalize_phone, parse_html
from housing_bot.mail import parse_email

LISTING_HTML = """
<html><head><title>Appartement Prinsegracht 12, Den Haag</title>
<script type="application/ld+json">{"@type": "Residence", "name": "Prinsegracht 12"}</script></head>
<body><nav>Menu Login</nav>
<h1>Prinsegracht 12</h1>
<p>Huurprijs € 1.350 per maand excl. servicekosten. 55 m², gemeubileerd. Inschrijving mogelijk.</p>
<p>Bel ons: 070 - 345 67 89 of mail <a href="mailto:verhuur@goedmakelaar.nl">verhuur@goedmakelaar.nl</a></p>
<a href="tel:+31703456789">Bellen</a> <a href="mailto:noreply@platform.nl">x</a>
""" + "<p>Beschrijving van de woning met veel tekst.</p>" * 10 + "</body></html>"


def test_parse_html_extracts_contacts_and_structured_data():
    page = parse_html("https://goedmakelaar.nl/aanbod/123", LISTING_HTML)
    assert page.phones == ["+31703456789"]
    assert page.emails == ["verhuur@goedmakelaar.nl"]
    assert page.jsonld and page.jsonld[0]["name"] == "Prinsegracht 12"
    assert "Menu Login" not in page.text  # navigation stripped
    assert not page.blocked


def test_block_detection():
    page = parse_html("https://x.nl", "<html><body>Just a moment... checking your browser</body></html>", 403)
    assert page.blocked


def test_normalize_phone():
    assert normalize_phone("06 12345678") == "+31612345678"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("12345") is None


def test_parse_email_html_links_and_threading():
    msg = EmailMessage()
    msg["From"] = "Makelaar <info@goedmakelaar.nl>"
    msg["Subject"] = "Uitnodiging bezichtiging"
    msg["Message-ID"] = "<reply1@goedmakelaar.nl>"
    msg["In-Reply-To"] = "<msg1@test>"
    msg.set_content("Zie de link.")
    msg.add_alternative('<p>Kies een tijd: <a href="https://planner.example/slot/abc123">plan</a></p>', subtype="html")
    mail = parse_email(msg.as_bytes())
    assert mail.sender == "info@goedmakelaar.nl"
    assert mail.sender_domain == "goedmakelaar.nl"
    assert "https://planner.example/slot/abc123" in mail.links
    assert mail.references == ["<msg1@test>"]
