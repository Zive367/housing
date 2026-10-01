"""Every schema sent to Claude must stay within the API's structured-output limits, or every call fails with a 400."""

import pytest
from anthropic.lib._parse._transform import transform_schema
from pydantic import TypeAdapter

from housing_bot.browser_agent import Step
from housing_bot.compose import Composed
from housing_bot.extract import Extracted
from housing_bot.learn import NewStyle
from housing_bot.models import ReplyAnalysis, ScamVerdict

MAX_OPTIONAL = 24      # per request, across all strict schemas
MAX_UNION = 16


def complexity(schema: dict) -> tuple[int, int]:
    optional = unions = 0

    def walk(node):
        nonlocal optional, unions
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                required = set(node.get("required", []))
                optional += sum(1 for name in props if name not in required)
                unions += sum(1 for p in props.values()
                              if isinstance(p, dict) and ("anyOf" in p or isinstance(p.get("type"), list)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return optional, unions


@pytest.mark.parametrize("model", [Extracted, ReplyAnalysis, ScamVerdict, Step, Composed, NewStyle])
def test_schema_within_api_limits(model):
    optional, unions = complexity(transform_schema(TypeAdapter(model).json_schema()))
    assert optional <= MAX_OPTIONAL, f"{model.__name__}: {optional} optional fields"
    assert unions <= MAX_UNION, f"{model.__name__}: {unions} union-typed fields"


def test_extracted_converts_unknowns_to_none():
    blank = {name: None for name in Extracted.model_fields}
    blank.update(is_rental_listing=True, availability="unknown", title="", street="Spui", house_number="",
                 postcode="", city="Den Haag", neighbourhood="", rent_eur=1295, service_costs="excluded",
                 service_costs_eur=60, deposit_eur=0, size_m2=0, rooms=0, bedrooms=0, property_type="unknown",
                 furnished="furnished", registration_allowed="yes", max_occupants=0, couples_allowed="unknown",
                 students_only="no", income_multiplier=0, available_from="", contract_type="indefinite",
                 min_contract_months=0, landlord_type="agency", agency_name="X", agent_name="",
                 agent_phones=[""], agent_emails=["Verhuur@X.nl", "n/a"], application_method="web_form",
                 application_instructions="", language="nl", highlights=["a"], red_flag_quotes=[])
    d = Extracted(**blank).to_details()
    assert d.house_number is None and d.size_m2 is None and d.max_occupants is None
    assert d.still_available is None and d.students_only is False and d.property_type is None
    assert d.rent_eur == 1295 and d.rent_includes_service_costs is False and d.service_costs_eur == 60
    assert d.agent_emails == ["verhuur@x.nl"] and d.agent_phones == []
