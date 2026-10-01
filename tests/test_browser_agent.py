"""The form agent against a local fake agency page, in a real headless Chromium.

The scripted 'Claude' deliberately tries two forbidden things (typing into an IBAN field, clicking "Betaal nu")
to prove the code-level guards stop them.
"""

import ast
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from housing_bot import browser_agent
from housing_bot.browser_agent import Action, Step
from housing_bot.models import Listing

PAGE = """<!doctype html><html><body>
<h1>Prinsegracht 12</h1><p>Mooi appartement.</p>
<button id="open" onclick="document.getElementById('f').style.display='block'">Reageer op deze woning</button>
<form id="f" style="display:none" onsubmit="send(event)">
  <label for="n">Naam</label><input id="n" name="naam" required>
  <label for="e">E-mail</label><input id="e" name="email" type="email" required>
  <label for="t">Telefoon</label><input id="t" name="telefoon">
  <label for="i">IBAN</label><input id="i" name="iban">
  <label for="m">Bericht</label><textarea id="m" name="bericht"></textarea>
  <label><input type="checkbox" id="c" required> Ik ga akkoord met de privacyverklaring</label>
  <button type="button" onclick="window.paid=true; fetch('/pay', {method:'POST'})">Betaal nu</button>
  <input type="button" value="Ga verder met iDEAL" onclick="fetch('/pay2', {method:'POST'})">
  <button type="submit">Verstuur</button>
</form>
<script>
function send(ev) {
  ev.preventDefault();
  const data = {naam: n.value, email: e.value, telefoon: t.value, iban: i.value, bericht: m.value, akkoord: c.checked};
  fetch('/submit', {method: 'POST', body: JSON.stringify(data)}).then(() => {
    document.body.innerHTML = '<h2>Bedankt voor je reactie!</h2><p>We nemen snel contact op.</p>';
  });
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    received: list = []

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Handler.received.append((self.path, body.decode()))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class ScriptedLLM:
    """Plays Claude's role with a fixed policy based on what the page shows."""

    def __init__(self):
        self.turns = 0

    def parse(self, schema, system, user, **kwargs):
        assert schema is Step
        self.turns += 1
        elements = ast.literal_eval(user.split("Interactive elements:\n", 1)[1])

        def find(word):
            return next(e["id"] for e in elements if word.lower() in (e["text"] + " " + e["label"]).lower())

        if "Bedankt" in user:
            return Step(status="submitted", reason="confirmation shown", evidence="Bedankt voor je reactie!")
        if not any(e["tag"] == "textarea" for e in elements):
            return Step(actions=[Action(kind="click", element_id=find("Reageer"))], status="continue",
                        reason="open the form")
        return Step(status="continue", reason="fill and send", actions=[
            Action(kind="type", element_id=find("Naam"), value_key="full_name"),
            Action(kind="type", element_id=find("E-mail"), value_key="email"),
            Action(kind="type", element_id=find("Telefoon"), value_key="phone"),
            Action(kind="type", element_id=find("IBAN"), value_key="full_name"),      # must be refused
            Action(kind="type", element_id=find("Bericht"), value_key="message"),
            Action(kind="check", element_id=find("akkoord")),
            Action(kind="click", element_id=find("Betaal nu")),                       # must be refused
            Action(kind="click", element_id=find("iDEAL")),                           # must be refused
            Action(kind="click", element_id=find("Verstuur")),
        ])


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    Handler.received = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/aanbod/123"
    httpd.shutdown()


def test_form_agent_fills_form_and_respects_guards(cfg, server, tmp_path, monkeypatch):
    monkeypatch.setattr(browser_agent, "DATA_DIR", tmp_path)
    listing = Listing(id="t1", url=server, address="Prinsegracht 12", apply_as="solo")
    llm = ScriptedLLM()
    result = browser_agent.send_via_form(cfg, llm, listing, "Beste verhuurder, graag een bezichtiging.")

    assert result.outcome == "submitted", result
    paths = [p for p, _ in Handler.received]
    assert "/pay" not in paths and "/pay2" not in paths      # payment clicks refused
    submitted = json.loads(next(body for p, body in Handler.received if p == "/submit"))
    assert submitted["naam"] == "Sam Jansen"
    assert submitted["email"] == "bot@example.com"
    assert submitted["telefoon"] == "+31612345678"
    assert submitted["bericht"] == "Beste verhuurder, graag een bezichtiging."
    assert submitted["iban"] == ""                           # sensitive field refused
    assert submitted["akkoord"] is True
    assert (tmp_path / "screens" / "t1.png").exists()
