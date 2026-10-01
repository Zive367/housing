"""Your criteria: does a listing qualify, apply alone or as a couple, permit checks, and ranking."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .geo import same_city
from .models import Listing, ListingDetails

# Approximate taxable annual income from gross monthly salary: 12 months + 8% holiday allowance.
ANNUAL_FACTOR = 12.96


@dataclass
class Decision:
    qualifies: bool
    reasons: list[str] = field(default_factory=list)   # why not, when it doesn't qualify
    apply_as: str = "solo"
    apply_reason: str = ""
    permit_note: str = ""


def total_rent(d: ListingDetails) -> float | None:
    if d.rent_eur is None:
        return None
    if d.rent_includes_service_costs is False and d.service_costs_eur:
        return d.rent_eur + d.service_costs_eur
    return d.rent_eur


def couple_blocker(cfg: Config, d: ListingDetails) -> str:
    """Why this home can't be rented as a couple ("" if it can)."""
    if not cfg.partner.present:
        return "No partner configured"
    if d.max_occupants == 1:
        return "Listing allows 1 person"
    if d.couples_allowed == "no":
        return "Listing excludes couples"
    if d.size_m2 is not None and d.size_m2 < cfg.search.min_size_m2_couple:
        return f"Too small for two ({d.size_m2:.0f} m²)"
    return ""


def choose_household(cfg: Config, d: ListingDetails, rent: float | None) -> tuple[str, str]:
    """Apply alone or together? Picks whichever application an agent is most likely to accept."""
    blocker = couple_blocker(cfg, d)
    if blocker:
        return "solo", blocker

    big = d.size_m2 is not None and d.size_m2 >= cfg.income.couple_preferred_size_m2
    mine = cfg.applicant.gross_monthly_income
    theirs = cfg.partner.gross_monthly_income or 0
    multiplier = d.income_multiplier or cfg.income.default_multiplier
    bare = d.rent_eur if d.rent_eur is not None else rent
    if mine is None or not bare:
        return ("couple", "Spacious home; incomes not configured") if big else ("solo", "Incomes not configured")

    need = multiplier * bare
    solo_ok = mine >= need
    couple_ok = mine + theirs >= need
    if solo_ok and not big:
        return "solo", f"Your income alone meets {multiplier:g}x rent"
    if couple_ok and (big or not solo_ok):
        why = "Combined income needed" if not solo_ok else "Spacious home, two incomes is the stronger profile"
        return "couple", f"{why} ({multiplier:g}x rent = €{need:,.0f}/mo gross)"
    if solo_ok:
        return "solo", f"Your income alone meets {multiplier:g}x rent"
    return ("couple" if theirs else "solo",
            f"Below the usual {multiplier:g}x rent requirement (€{need:,.0f}/mo gross); applying anyway")


def permit_check(cfg: Config, municipality: str, bare_rent: float | None, household: str) -> tuple[str, bool]:
    """Returns (note, blocks). Den Haag requires a housing permit for rents up to the middle-rent cap."""
    if bare_rent is None:
        return "", False
    for rule in cfg.permits:
        if not same_city(rule.municipality, municipality):
            continue
        for band in sorted(rule.bands, key=lambda b: b.max_rent):
            if bare_rent <= band.max_rent:
                monthly = (cfg.applicant.gross_monthly_income or 0) + (
                    (cfg.partner.gross_monthly_income or 0) if household == "couple" else 0)
                cap = band.income_max_single if household == "solo" else band.income_max_multi
                note = f"Housing permit needed (rent ≤ €{band.max_rent:,.2f}); income cap €{cap:,.0f}/yr"
                if monthly and monthly * ANNUAL_FACTOR > cap:
                    return note + ": your income is likely above it", True
                return note, False
    return "", False


def decide(cfg: Config, listing: Listing) -> Decision:
    d = listing.details
    s = cfg.search
    reasons: list[str] = []
    rent = listing.total_rent

    if rent is not None and rent > s.max_rent:
        reasons.append(f"Rent €{rent:,.0f} above max €{s.max_rent:,.0f}")
    if listing.municipality and not any(same_city(listing.municipality, m) for m in s.municipalities):
        reasons.append(f"Outside search area ({listing.municipality})")
    if listing.distance_km is not None and listing.distance_km > s.max_distance_km:
        reasons.append(f"Too far from work ({listing.distance_km:.1f} km)")
    if s.exclude_rooms and d.property_type == "room":
        reasons.append("Room in a shared house")
    if s.exclude_students_only and d.students_only:
        reasons.append("Students only")
    if s.require_registration and d.registration_allowed == "no":
        reasons.append("Registration at the address not allowed")
    if d.contract_type == "short_stay":
        reasons.append("Short stay only")
    if d.size_m2 is not None and d.size_m2 < s.min_size_m2_solo:
        reasons.append(f"Too small ({d.size_m2:.0f} m²)")
    if (s.min_neighbourhood_score and listing.neighbourhood_score is not None
            and listing.neighbourhood_score < s.min_neighbourhood_score):
        reasons.append(f"Neighbourhood indicator {listing.neighbourhood_score}/10")

    household, why = choose_household(cfg, d, rent)
    bare = d.rent_eur if d.rent_eur is not None else rent
    note, blocks = permit_check(cfg, listing.municipality, bare, household)
    if blocks:
        other = "couple" if household == "solo" else "solo"
        alt_note, alt_blocks = permit_check(cfg, listing.municipality, bare, other)
        if not alt_blocks and (other == "solo" or not couple_blocker(cfg, d)):
            household, why, note = other, f"Applying {other}: income cap of the housing permit", alt_note
        else:
            reasons.append(note)

    return Decision(qualifies=not reasons, reasons=reasons, apply_as=household, apply_reason=why, permit_note=note)


def rank(cfg: Config, listing: Listing) -> float:
    """Higher is better. Used to sort the sheet and the daily digest."""
    d = listing.details
    score = 50.0
    if listing.total_rent:
        score += max(-15, min(15, (cfg.search.preferred_rent - listing.total_rent) / 20))
    if listing.bike_minutes is not None:
        score -= min(30, listing.bike_minutes * 0.8)
    if d and cfg.search.prefer_furnished:
        score += {"furnished": 8, "upholstered": 3}.get(d.furnished, 0)
    if listing.neighbourhood_score is not None:
        score += (listing.neighbourhood_score - 5) * 2
    if d and d.size_m2:
        score += min(d.size_m2, 80) / 8
    if listing.scam_score:
        score -= listing.scam_score / 5
    return round(score, 1)
