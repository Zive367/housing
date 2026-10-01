from housing_bot.decide import choose_household, decide, permit_check, rank, total_rent
from housing_bot.models import Listing, ListingDetails
from housing_bot.scam import rule_signals


def make(cfg, **details) -> Listing:
    d = ListingDetails(is_rental_listing=True, **details)
    listing = Listing(id="x", url="https://agency.nl/aanbod/1", domain="agency.nl", details=d,
                      municipality="'s-Gravenhage", distance_km=3.0, bike_minutes=14)
    listing.total_rent = total_rent(d)
    return listing


# --- scam rules ---------------------------------------------------------------------------------

def test_classic_scam_text_scores_high(cfg):
    listing = make(cfg, rent_eur=650, size_m2=70, agent_phones=["+447700900123"])
    text = ("I am currently in the UK for work so I cannot show the apartment. Pay the deposit via Airbnb payment "
            "and the keys will be sent by DHL. Western Union also possible.")
    signals = rule_signals(cfg, listing, text)
    score = sum(points for points, _ in signals)
    reasons = " ".join(r for _, r in signals)
    assert score >= 100
    assert "abroad" in reasons and "Keys sent by post" in reasons and "below market" in reasons
    assert "Foreign phone" in reasons


def test_normal_dutch_listing_has_no_signals(cfg):
    listing = make(cfg, rent_eur=1350, size_m2=55, agent_phones=["+31703456789"], landlord_type="agency")
    text = ("Huurprijs € 1.350 per maand. Waarborgsom 2 maanden. De huur dient vooraf te worden betaald voor de "
            "1e van de maand. Bezichtiging op afspraak. Betaling van borg en eerste maand voor de sleuteloverdracht.")
    assert rule_signals(cfg, listing, text) == []


def test_unknown_address_is_a_signal(cfg):
    listing = make(cfg, rent_eur=1300, size_m2=50, house_number="999")
    listing.address_verified = False
    assert any("does not exist" in r for _, r in rule_signals(cfg, listing, ""))


# --- criteria -----------------------------------------------------------------------------------

def test_decide_filters(cfg):
    assert decide(cfg, make(cfg, rent_eur=1350, size_m2=50)).qualifies
    reasons = decide(cfg, make(cfg, rent_eur=1600, size_m2=50, registration_allowed="no",
                               property_type="room", contract_type="short_stay")).reasons
    joined = " ".join(reasons)
    assert "above max" in joined and "Registration" in joined and "Room" in joined and "Short stay" in joined
    outside = make(cfg, rent_eur=1200, size_m2=50)
    outside.municipality = "Amsterdam"
    assert "Outside search area" in decide(cfg, outside).reasons[0]


def test_service_costs_count_towards_budget(cfg):
    listing = make(cfg, rent_eur=1420, rent_includes_service_costs=False, service_costs_eur=120, size_m2=60)
    assert listing.total_rent == 1540
    assert not decide(cfg, listing).qualifies


def test_household_choice(cfg):
    small = ListingDetails(is_rental_listing=True, rent_eur=1200, size_m2=30)
    assert choose_household(cfg, small, 1200)[0] == "solo"            # too small for two
    one = ListingDetails(is_rental_listing=True, rent_eur=1200, size_m2=60, max_occupants=1)
    assert choose_household(cfg, one, 1200)[0] == "solo"
    big = ListingDetails(is_rental_listing=True, rent_eur=1400, size_m2=65)
    assert choose_household(cfg, big, 1400)[0] == "couple"            # 4500 < 3.5 x 1400 = 4900, combined is enough
    medium = ListingDetails(is_rental_listing=True, rent_eur=1200, size_m2=40)
    assert choose_household(cfg, medium, 1200)[0] == "solo"           # 4500 >= 4200 alone, compact home


def test_den_haag_permit(cfg):
    note, blocks = permit_check(cfg, "'s-Gravenhage", 1100, "solo")
    assert "permit needed" in note and not blocks                     # 4500 x 12.96 = 58,320 < 70,149
    note, blocks = permit_check(cfg, "'s-Gravenhage", 1100, "couple")
    assert blocks                                                     # 8000 x 12.96 = 103,680 > 93,531
    assert permit_check(cfg, "'s-Gravenhage", 1300, "solo") == ("", False)   # free sector
    assert permit_check(cfg, "Rijswijk", 1100, "solo") == ("", False)


def test_permit_cap_switches_household(cfg):
    listing = make(cfg, rent_eur=1100, size_m2=70)                   # big: would apply as couple, but cap blocks
    decision = decide(cfg, listing)
    assert decision.qualifies and decision.apply_as == "solo"


def test_rank_prefers_cheap_close_furnished(cfg):
    good = make(cfg, rent_eur=1250, size_m2=50, furnished="furnished")
    bad = make(cfg, rent_eur=1500, size_m2=50, furnished="unfurnished")
    bad.bike_minutes = 35
    assert rank(cfg, good) > rank(cfg, bad)
