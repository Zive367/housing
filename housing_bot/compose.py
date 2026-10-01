"""Write the application message: personal, short, and only facts from your profile."""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from .config import Config
from .llm import LLM, BudgetExceeded, LLMError
from .models import Listing

log = logging.getLogger(__name__)


class Composed(BaseModel):
    subject: str = Field(description="Email subject line, max 70 characters")
    message: str = Field(description="Plain-text message body")


SYSTEM = """You write the first message a tenant sends to a letting agent or landlord in the Netherlands, in reply
to one specific rental listing. Agents get dozens of replies within minutes; a short, concrete, trustworthy message
gets the viewing.

Hard rules:
- Use ONLY facts from the applicant profile you are given. Never invent jobs, employers, income, pets, references,
  dates or anything else. If a fact is missing, leave it out.
- Never state income amounts. Never offer or attach ID, payslips or other documents; at most say documents are
  available on request.
- Only say the income requirement is met if the profile says income_meets_requirement is true.
- Mention one or two concrete details of this home, from the listing facts, so it's clearly not a mass message.
- Say the applicant can move in as soon as possible and is looking for a long-term home. Ask for a viewing.
- Sign with the applicant's full name, phone number and email address.
- Plain text, no markdown, no emojis, no placeholders like [name]. Follow the requested style and language exactly."""


def meets_income(cfg: Config, listing: Listing) -> bool | None:
    d = listing.details
    bare = d.rent_eur if d and d.rent_eur is not None else listing.total_rent
    mine = cfg.applicant.gross_monthly_income
    if not bare or mine is None:
        return None
    total = mine + ((cfg.partner.gross_monthly_income or 0) if listing.apply_as == "couple" else 0)
    multiplier = (d.income_multiplier if d and d.income_multiplier else None) or cfg.income.default_multiplier
    return total >= multiplier * bare


def profile_facts(cfg: Config, listing: Listing) -> dict:
    a = cfg.applicant
    facts: dict = {
        "applicant": {k: v for k, v in {
            "full_name": f"{a.first_name} {a.last_name}".strip(), "age": a.age, "job_title": a.job_title,
            "employer": a.employer, "contract": a.contract, "nationality": a.nationality, "about": a.about,
        }.items() if v},
        "phone": cfg.secrets.applicant_phone,
        "email": cfg.secrets.contact_email,
        "household": "applicant alone" if listing.apply_as != "couple" else "applicant and partner (a couple)",
        "income_meets_requirement": meets_income(cfg, listing),
    }
    if listing.apply_as == "couple" and cfg.partner.present:
        p = cfg.partner
        facts["partner"] = {k: v for k, v in {"first_name": p.first_name, "age": p.age, "job_title": p.job_title,
                                              "contract": p.contract}.items() if v}
    return facts


def listing_facts(listing: Listing) -> dict:
    d = listing.details
    return {k: v for k, v in {
        "address": listing.address or (d.title if d else ""),
        "rent_eur": listing.total_rent,
        "size_m2": d.size_m2 if d else None,
        "furnished": d.furnished if d else None,
        "highlights": d.highlights if d else [],
        "agent_name": d.agent_name if d else None,
        "agency": d.agency_name if d else None,
        "listing_language": d.language if d else None,
    }.items() if v}


def compose(cfg: Config, llm: LLM | None, listing: Listing, variant: dict) -> Composed:
    if llm is not None:
        prompt = (f"Style: {variant['style']}\nLanguage: {'Dutch' if variant['language'] == 'nl' else 'English'}\n\n"
                  f"Applicant profile: {json.dumps(profile_facts(cfg, listing), ensure_ascii=False)}\n\n"
                  f"Listing facts: {json.dumps(listing_facts(listing), ensure_ascii=False)}")
        try:
            return llm.parse(Composed, SYSTEM, prompt, effort="low", max_tokens=4000)
        except (LLMError, BudgetExceeded) as e:
            log.warning("falling back to template message: %s", e)
    return template(cfg, listing, variant["language"])


def template(cfg: Config, listing: Listing, language: str) -> Composed:
    """Plain fallback when Claude is unavailable or over budget."""
    a = cfg.applicant
    name = f"{a.first_name} {a.last_name}".strip()
    where = listing.address or "this home"
    agent = listing.details.agent_name if listing.details and listing.details.agent_name else None
    couple = listing.apply_as == "couple" and cfg.partner.present
    contact = "\n".join(x for x in [name, cfg.secrets.applicant_phone, cfg.secrets.contact_email] if x)
    if language == "nl":
        contract = {"permanent": "een vast contract", "temporary": "een tijdelijk contract"}.get(a.contract, "")
        intro = f"Ik ben {a.first_name}" + (f" ({a.age})" if a.age else "")
        if a.job_title:
            intro += f", {a.job_title}" + (f" met {contract}" if contract else "")
        if couple:
            intro += f", en ik zoek samen met mijn partner {cfg.partner.first_name} een woning"
        body = (f"Beste {agent or 'heer/mevrouw'},\n\nGraag reageer ik op {where}. {intro}. "
                "Ik kan direct verhuizen en zoek een woning voor de lange termijn. "
                f"Is een bezichtiging mogelijk?\n\nMet vriendelijke groet,\n{contact}")
        return Composed(subject=f"Reactie op {where}"[:70], message=body)
    contract = {"permanent": "a permanent contract", "temporary": "a temporary contract"}.get(a.contract, "")
    intro = f"I'm {a.first_name}" + (f" ({a.age})" if a.age else "")
    if a.job_title:
        intro += f", working as {a.job_title}" + (f" on {contract}" if contract else "")
    if couple:
        intro += f", looking together with my partner {cfg.partner.first_name}"
    body = (f"Dear {agent or 'Sir/Madam'},\n\nI'm very interested in {where}. {intro}. "
            "I can move in right away and I'm looking for a long-term home. "
            f"Could we schedule a viewing?\n\nKind regards,\n{contact}")
    return Composed(subject=f"Viewing request: {where}"[:70], message=body)
