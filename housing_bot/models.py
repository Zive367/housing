"""Data shapes shared across the pipeline."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Status(StrEnum):
    NEW = "NEW"
    FILTERED = "FILTERED"              # fails your criteria (budget, area, size, registration, ...)
    DUPLICATE = "DUPLICATE"            # same home already seen via another source
    UNAVAILABLE = "UNAVAILABLE"        # already rented / under option / not a listing
    LIKELY_SCAM = "LIKELY_SCAM"        # scam score >= block threshold: never applied
    READY = "READY"                    # message written but dry-run mode is on
    APPLIED = "APPLIED"
    MANUAL_APPLY = "MANUAL_APPLY"      # bot couldn't apply itself: you need to (link + message sent to you)
    FAILED = "FAILED"
    VIEWING_INVITED = "VIEWING_INVITED"  # agent offered slots / a booking link: act fast
    VIEWING_BOOKED = "VIEWING_BOOKED"
    DOCS_REQUESTED = "DOCS_REQUESTED"  # agent wants documents: you send them yourself
    NEEDS_REPLY = "NEEDS_REPLY"        # agent asked a question
    REJECTED = "REJECTED"
    NO_RESPONSE = "NO_RESPONSE"
    OFFER = "OFFER"
    CLOSED = "CLOSED"


# Statuses that end the automated part of a listing's life.
TERMINAL = {Status.FILTERED, Status.DUPLICATE, Status.UNAVAILABLE, Status.LIKELY_SCAM,
            Status.REJECTED, Status.CLOSED}
# Statuses that count as "the application got a positive response" for learning.
POSITIVE = {Status.VIEWING_INVITED, Status.VIEWING_BOOKED, Status.OFFER, Status.DOCS_REQUESTED}


class ListingDetails(BaseModel):
    """Facts about one listing (filled from Claude's extraction, see extract.Extracted). Unknown = None."""

    is_rental_listing: bool = Field(description="True if the page advertises one specific home for rent")
    still_available: bool | None = Field(None, description="False if marked rented/verhuurd/under option/onder optie")
    title: str | None = None
    street: str | None = None
    house_number: str | None = Field(None, description="Number incl. addition, e.g. '12-B'")
    postcode: str | None = Field(None, description="Dutch postcode like '2511AB'")
    city: str | None = None
    neighbourhood: str | None = None
    rent_eur: float | None = Field(None, description="Monthly rent as advertised")
    rent_includes_service_costs: bool | None = None
    service_costs_eur: float | None = Field(None, description="Monthly service costs if listed separately")
    deposit_eur: float | None = None
    size_m2: float | None = None
    rooms: int | None = None
    bedrooms: int | None = None
    property_type: Literal["apartment", "studio", "house", "room", "other"] | None = None
    furnished: Literal["furnished", "upholstered", "unfurnished", "unknown"] = Field(
        "unknown", description="gemeubileerd=furnished, gestoffeerd=upholstered, kaal=unfurnished")
    registration_allowed: Literal["yes", "no", "unknown"] = Field(
        "unknown", description="Can the tenant register (inschrijven) at the address?")
    max_occupants: int | None = None
    couples_allowed: Literal["yes", "no", "unknown"] = "unknown"
    students_only: bool | None = None
    income_multiplier: float | None = Field(None, description="Required gross income as a multiple of rent, e.g. 3.5")
    available_from: str | None = Field(None, description="ISO date, or 'immediately'")
    contract_type: Literal["indefinite", "temporary", "short_stay", "unknown"] = "unknown"
    min_contract_months: int | None = None
    landlord_type: Literal["agency", "private", "institutional", "unknown"] = "unknown"
    agency_name: str | None = None
    agent_name: str | None = None
    agent_phones: list[str] = Field(default_factory=list)
    agent_emails: list[str] = Field(default_factory=list)
    application_method: Literal["web_form", "email", "phone", "platform_message", "viewing_planner",
                                "booking_payment", "unknown"] = "unknown"
    application_instructions: str | None = Field(None, description="How to apply, max 2 short sentences")
    language: Literal["nl", "en", "other"] = "nl"
    highlights: list[str] = Field(default_factory=list, description="2-4 short concrete features of this home")
    red_flag_quotes: list[str] = Field(
        default_factory=list, description="Verbatim phrases typical of rental scams, if any")


class Listing(BaseModel):
    id: str
    url: str
    source: str = ""                    # where we heard about it (alert sender domain or "manual")
    domain: str = ""
    status: Status = Status.NEW
    first_seen: float = Field(default_factory=time.time)
    updated: float = Field(default_factory=time.time)
    alert_text: str = ""                # snippet from the alert email, used if the page is blocked
    page_blocked: bool = False
    details: ListingDetails | None = None

    # enrichment
    address: str = ""
    address_key: str = ""               # "2511AB|12" for matching replies and de-duplication
    municipality: str = ""
    buurt: str = ""
    buurt_code: str = ""
    lat: float | None = None
    lon: float | None = None
    address_verified: bool | None = None
    distance_km: float | None = None
    bike_minutes: int | None = None
    walk_minutes: int | None = None
    neighbourhood_score: float | None = None

    # verification / decision
    total_rent: float | None = None
    scam_score: int | None = None
    scam_reasons: list[str] = Field(default_factory=list)
    filter_reasons: list[str] = Field(default_factory=list)
    rank: float | None = None
    apply_as: Literal["solo", "couple", ""] = ""
    apply_reason: str = ""
    permit_note: str = ""
    duplicate_of: str = ""

    # contact
    agency: str = ""
    agent: str = ""
    phone: str = ""
    email: str = ""
    call_needed: bool = False
    call_reason: str = ""

    # application
    variant: str = ""
    message: str = ""
    apply_method: str = ""
    sent_message_id: str = ""           # Message-ID of our application email, to match replies
    applied_at: float | None = None
    time_to_apply_s: float | None = None
    apply_error: str = ""

    # follow-up
    outcome: Literal["", "win", "loss"] = ""   # counted once for the message-style experiment
    viewing_at: str = ""
    viewing_link: str = ""
    last_reply: str = ""
    notes: str = ""

    def touch(self) -> None:
        self.updated = time.time()

    @property
    def flagged(self) -> bool:
        if self.status in {Status.VIEWING_BOOKED, Status.VIEWING_INVITED, Status.DOCS_REQUESTED,
                           Status.NEEDS_REPLY, Status.OFFER}:
            return True
        return self.call_needed and self.status not in TERMINAL


class ScamVerdict(BaseModel):
    score: int = Field(description="0 = clearly legitimate, 100 = clearly a scam")
    reasons: list[str] = Field(default_factory=list, description="Short, specific reasons")


class ReplyAnalysis(BaseModel):
    """What Claude extracts from an incoming email that isn't a listing alert."""

    category: Literal["viewing_invitation", "viewing_confirmed", "viewing_cancelled", "rejection",
                      "documents_requested", "question", "platform_notification", "auto_acknowledgement",
                      "new_listing_alert", "unrelated"]
    summary: str = Field(description="One sentence, in English")
    property_address: str | None = Field(None, description="Street + number and/or postcode the email is about")
    listing_url: str | None = None
    viewing_datetime: str | None = Field(None, description="ISO 8601 local time if a viewing time is fixed")
    booking_link: str | None = Field(None, description="Link to pick or confirm a viewing slot, if any")
    agent_name: str | None = None
    agent_phone: str | None = None
    needs_action: bool = Field(description="True if you must do something (book, reply, send documents, call)")
