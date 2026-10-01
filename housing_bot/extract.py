"""Claude reads a listing page (any site, any layout) and returns structured facts."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .fetch import Page
from .llm import LLM
from .models import ListingDetails

SYSTEM = """You extract facts from Dutch rental listing pages for a tenant's search assistant.

Rules:
- Use only what the page says. Never guess an address, price or phone number.
- Unknown values: empty string for text, 0 for numbers, "unknown" for choices, [] for lists.
- Money is EUR per month unless the page says otherwise. If the page says "excl." service costs, set
  service_costs="excluded" and put the amount in service_costs_eur.
- Dutch terms: kale huur = bare rent; servicekosten = service costs; borg/waarborgsom = deposit;
  gemeubileerd = furnished; gestoffeerd = upholstered (floors/curtains, no furniture); kaal/oplevering kaal = unfurnished;
  inschrijven/inschrijving (BRP) mogelijk = registration allowed; "geen inschrijving" = registration not allowed;
  "niet geschikt voor woningdelers" = not for house-sharers (couples are fine); "max. 1 persoon" = max_occupants 1;
  inkomenseis "3x/4x de kale huur" = income_multiplier; onbepaalde tijd = indefinite; tijdelijk/bepaalde tijd = temporary;
  short stay / vakantieverhuur / max 6 maanden = short_stay; verhuurd / onder optie / onder voorbehoud = not available.
- application_method: how a tenant responds. Reaction/contact form or "bezichtiging aanvragen" button = web_form;
  only an email address = email; "bel ons" / phone only = phone; messaging inside a platform account = platform_message;
  a viewing-slot planner = viewing_planner; reserving by paying (booking platforms) = booking_payment.
- agent_phones / agent_emails: contacts of the landlord or letting agent for THIS home. Exclude the platform's own
  customer service numbers and addresses.
- highlights: 2-4 short concrete features useful to mention in a reply (e.g. "balcony facing south", "near HS station").
- red_flag_quotes: copy exact phrases that are typical of rental scams: owner abroad, pay or transfer before viewing,
  keys sent by post, payment via Airbnb/escrow/Western Union/crypto, contact only via WhatsApp, ID/passport requested
  before a viewing, emotional stories, extreme urgency. Leave empty if none."""

YesNo = Literal["yes", "no", "unknown"]


class Extracted(BaseModel):
    """What Claude fills in. Every field is required and nothing is nullable: the API caps optional and
    union-typed fields per request (24 / 16), so "unknown" is spelled as "", 0 or "unknown" instead."""

    is_rental_listing: bool = Field(description="True if the page advertises one specific home for rent")
    availability: Literal["available", "rented_or_under_option", "unknown"]
    title: str
    street: str
    house_number: str = Field(description="Number incl. addition, e.g. '12-B'")
    postcode: str = Field(description="Dutch postcode like '2511AB'")
    city: str
    neighbourhood: str
    rent_eur: float = Field(description="Monthly rent as advertised")
    service_costs: Literal["included", "excluded", "unknown"]
    service_costs_eur: float
    deposit_eur: float
    size_m2: float
    rooms: int
    bedrooms: int
    property_type: Literal["apartment", "studio", "house", "room", "other", "unknown"]
    furnished: Literal["furnished", "upholstered", "unfurnished", "unknown"]
    registration_allowed: YesNo = Field(description="Can the tenant register (inschrijven) at the address?")
    max_occupants: int
    couples_allowed: YesNo
    students_only: YesNo
    income_multiplier: float = Field(description="Required gross income as a multiple of rent, e.g. 3.5")
    available_from: str = Field(description="ISO date, or 'immediately'")
    contract_type: Literal["indefinite", "temporary", "short_stay", "unknown"]
    min_contract_months: int
    landlord_type: Literal["agency", "private", "institutional", "unknown"]
    agency_name: str
    agent_name: str
    agent_phones: list[str]
    agent_emails: list[str]
    application_method: Literal["web_form", "email", "phone", "platform_message", "viewing_planner",
                                "booking_payment", "unknown"]
    application_instructions: str = Field(description="How to apply, max 2 short sentences")
    language: Literal["nl", "en", "other"]
    highlights: list[str]
    red_flag_quotes: list[str]

    def to_details(self) -> ListingDetails:
        def text(value: str) -> str | None:
            return value.strip() or None

        def num(value: float) -> float | None:
            return value if value and value > 0 else None

        def count(value: int) -> int | None:
            return value if value and value > 0 else None

        yes_no = {"yes": True, "no": False, "unknown": None}
        return ListingDetails(
            is_rental_listing=self.is_rental_listing,
            still_available={"available": True, "rented_or_under_option": False}.get(self.availability),
            title=text(self.title), street=text(self.street), house_number=text(self.house_number),
            postcode=text(self.postcode), city=text(self.city), neighbourhood=text(self.neighbourhood),
            rent_eur=num(self.rent_eur), rent_includes_service_costs=yes_no[
                {"included": "yes", "excluded": "no"}.get(self.service_costs, "unknown")],
            service_costs_eur=num(self.service_costs_eur), deposit_eur=num(self.deposit_eur),
            size_m2=num(self.size_m2), rooms=count(self.rooms), bedrooms=count(self.bedrooms),
            property_type=None if self.property_type == "unknown" else self.property_type,
            furnished=self.furnished, registration_allowed=self.registration_allowed,
            max_occupants=count(self.max_occupants), couples_allowed=self.couples_allowed,
            students_only=yes_no[self.students_only], income_multiplier=num(self.income_multiplier),
            available_from=text(self.available_from), contract_type=self.contract_type,
            min_contract_months=count(self.min_contract_months), landlord_type=self.landlord_type,
            agency_name=text(self.agency_name), agent_name=text(self.agent_name),
            agent_phones=[p for p in self.agent_phones if p.strip()],
            agent_emails=[e.strip().lower() for e in self.agent_emails if "@" in e],
            application_method=self.application_method,
            application_instructions=text(self.application_instructions), language=self.language,
            highlights=self.highlights[:4], red_flag_quotes=self.red_flag_quotes[:8],
        )


def build_prompt(page: Page, alert_snippet: str = "") -> str:
    parts = [f"URL: {page.url}", f"Page title: {page.title}"]
    if page.jsonld:
        parts.append("Structured data (JSON-LD):\n" + json.dumps(page.jsonld, ensure_ascii=False)[:4000])
    if page.phones or page.emails:
        parts.append(f"Phone numbers found on page: {page.phones}\nEmail addresses found on page: {page.emails}")
    if alert_snippet:
        parts.append(f"Text from the alert email that linked here:\n{alert_snippet}")
    if page.blocked:
        parts.append("NOTE: the page could not be loaded (bot protection). Extract what you can from the alert text.")
    parts.append(f"Page text:\n{page.text}")
    return "\n\n".join(parts)


def extract(llm: LLM, page: Page, alert_snippet: str = "") -> ListingDetails:
    return llm.parse(Extracted, SYSTEM, build_prompt(page, alert_snippet), bulk=True, effort="low").to_details()
