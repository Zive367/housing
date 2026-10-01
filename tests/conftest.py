from __future__ import annotations

from collections import defaultdict

import pytest

from housing_bot.config import Config, Person, PermitBand, PermitRule, Search, Secrets, Work
from housing_bot.store import Store


class FakeLLM:
    """Returns queued objects per schema type, records prompts. No network, no cost."""

    def __init__(self):
        self.queue: dict[type, list] = defaultdict(list)
        self.prompts: list[tuple[str, str]] = []

    def add(self, obj) -> None:
        self.queue[type(obj)].append(obj)

    def parse(self, schema, system, user, **kwargs):
        self.prompts.append((schema.__name__, user if isinstance(user, str) else str(user)))
        if not self.queue[schema]:
            raise AssertionError(f"FakeLLM has no queued {schema.__name__}")
        return self.queue[schema].pop(0)


class FakeNotifier:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, title, details="", link="", important=False):
        self.sent.append({"title": title, "details": details, "link": link, "important": important})
        return True


class FakeMailbox:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, to, subject, body, in_reply_to=None, from_name=""):
        self.sent.append({"to": to, "subject": subject, "body": body, "from_name": from_name})
        return f"<msg{len(self.sent)}@test>"


@pytest.fixture
def cfg() -> Config:
    return Config(
        # A public landmark (Den Haag Centraal), not anyone's real workplace.
        work=Work(address="Koningin Julianaplein 10, 2595 AA Den Haag", lat=52.08148371, lon=4.32340689),
        permits=[PermitRule(municipality="'s-Gravenhage", bands=[
            PermitBand(max_rent=932.93, income_max_single=51537, income_max_multi=56910),
            PermitBand(max_rent=1228.07, income_max_single=70149, income_max_multi=93531)])],
        secrets=Secrets(bot_email="bot@example.com", applicant_phone="+31612345678"),
        applicant=Person(first_name="Sam", last_name="Jansen", age="30", job_title="analyst", contract="permanent",
                         gross_monthly_income=4500),
        partner=Person(first_name="Alex", age="29", job_title="designer", gross_monthly_income=3500),
        search=Search(municipalities=["'s-Gravenhage", "Rijswijk", "Leidschendam-Voorburg", "Delft"]),
    )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")
