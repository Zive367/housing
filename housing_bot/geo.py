"""Address verification (official BAG register via PDOK), commute estimate and neighbourhood indicator (CBS)."""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx

from .store import Store

log = logging.getLogger(__name__)

PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
PDOK_FIELDS = ("weergavenaam,type,straatnaam,huisnummer,huis_nlt,postcode,woonplaatsnaam,gemeentenaam,"
               "buurtnaam,buurtcode,wijkcode,centroide_ll")
CBS_URL = "https://opendata.cbs.nl/ODataApi/odata/{table}/TypedDataSet"
CITY_ALIASES = {"den haag": "s-gravenhage", "the hague": "s-gravenhage", "voorburg": "leidschendam-voorburg",
                "leidschendam": "leidschendam-voorburg", "nootdorp": "pijnacker-nootdorp",
                "pijnacker": "pijnacker-nootdorp", "ypenburg": "s-gravenhage"}


@dataclass
class Place:
    display: str
    kind: str                 # "adres" (exact home) or "weg" (street only) or other PDOK type
    street: str = ""
    number: str = ""
    postcode: str = ""
    city: str = ""
    municipality: str = ""
    buurt: str = ""
    buurt_code: str = ""
    wijk_code: str = ""
    lat: float | None = None
    lon: float | None = None
    verified: bool | None = None   # True: exact address exists. False: given address doesn't exist. None: not checkable


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9 -]", "", text.replace("'", ""))
    return re.sub(r"\s+", " ", text).strip()


def same_city(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or CITY_ALIASES.get(a, a) == CITY_ALIASES.get(b, b))


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def leading_int(value: str | None) -> int | None:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def _point(wkt: str) -> tuple[float | None, float | None]:
    match = re.match(r"POINT\(([-\d.]+) ([-\d.]+)\)", wkt or "")
    return (float(match.group(2)), float(match.group(1))) if match else (None, None)


def _search(client: httpx.Client, query: str, kind: str, rows: int = 5) -> list[dict]:
    try:
        response = client.get(PDOK_URL, params={"q": query, "fq": f"type:{kind}", "rows": rows, "fl": PDOK_FIELDS},
                              timeout=15)
        response.raise_for_status()
        return response.json()["response"]["docs"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("PDOK lookup failed for %r: %s", query, e)
        return []


def _place(doc: dict, verified: bool | None) -> Place:
    lat, lon = _point(doc.get("centroide_ll", ""))
    return Place(display=doc.get("weergavenaam", ""), kind=doc.get("type", ""), street=doc.get("straatnaam", ""),
                 number=str(doc.get("huis_nlt", "")), postcode=doc.get("postcode", ""),
                 city=doc.get("woonplaatsnaam", ""), municipality=doc.get("gemeentenaam", ""),
                 buurt=doc.get("buurtnaam", ""), buurt_code=doc.get("buurtcode", ""),
                 wijk_code=doc.get("wijkcode", ""), lat=lat, lon=lon, verified=verified)


def locate(client: httpx.Client, street: str | None, number: str | None, postcode: str | None,
           city: str | None) -> Place | None:
    """Find the home in the national address register. PDOK search is fuzzy, so every hit is re-checked."""
    postcode = re.sub(r"\s", "", postcode or "").upper()
    num = leading_int(number)

    if num and (postcode or (street and city)):
        query = f"{postcode} {number}" if postcode else f"{street} {number} {city}"
        for doc in _search(client, query, "adres"):
            if doc.get("huisnummer") != num:
                continue
            if postcode and doc.get("postcode") != postcode:
                continue
            if street and similar(street, doc.get("straatnaam", "")) < 0.85:
                continue
            if not postcode and city and not (same_city(city, doc.get("woonplaatsnaam", ""))
                                               or same_city(city, doc.get("gemeentenaam", ""))):
                continue
            return _place(doc, verified=True)
        # A full address was given but it doesn't exist: strong scam signal. Still locate the street for distance.
        fallback = locate(client, street, None, None, city) if street and city else None
        if fallback:
            fallback.verified = False
            return fallback
        return Place(display=f"{street or ''} {number or ''} {postcode} {city or ''}".strip(), kind="none",
                     verified=False)

    if street and city:
        for doc in _search(client, f"{street} {city}", "weg"):
            if similar(street, doc.get("straatnaam", "")) >= 0.85 and (
                    same_city(city, doc.get("woonplaatsnaam", "")) or same_city(city, doc.get("gemeentenaam", ""))):
                return _place(doc, verified=None)
        return Place(display=f"{street}, {city}", kind="none", verified=False)  # street doesn't exist there
    if postcode:
        docs = _search(client, postcode, "postcode", rows=1)
        return _place(docs[0], verified=None) if docs else None
    if city:
        docs = _search(client, city, "woonplaats", rows=1)
        return _place(docs[0], verified=None) if docs else None
    return None


def geocode_address(client: httpx.Client, address: str) -> tuple[float, float] | None:
    docs = _search(client, address, "adres", rows=1)
    if not docs:
        return None
    lat, lon = _point(docs[0].get("centroide_ll", ""))
    return (lat, lon) if lat is not None else None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bike_minutes(km: float) -> int:
    """Straight-line km -> rough cycling time (25% detour, 16 km/h)."""
    return round(km * 1.25 / 16 * 60)


def _scale(value: float | None, worst: float, best: float) -> float | None:
    if value is None:
        return None
    return max(0.0, min(10.0, (value - worst) / (best - worst) * 10))


def neighbourhood_score(client: httpx.Client, store: Store, table: str, *codes: str) -> float | None:
    """Rough 0-10 indicator from CBS figures: home values, share of low incomes, social assistance rate.

    It's a socio-economic proxy, not a safety rating. Cached per area code.
    """
    for code in [c for c in codes if c]:
        cached = store.kv_get(f"cbs:{table}:{code}")
        if cached is not None:
            return json.loads(cached)
        try:
            response = client.get(CBS_URL.format(table=table),
                                  params={"$format": "json", "$filter": f"WijkenEnBuurten eq '{code}'"}, timeout=20)
            response.raise_for_status()
            rows = response.json().get("value", [])
        except (httpx.HTTPError, ValueError) as e:
            log.warning("CBS lookup failed for %s: %s", code, e)
            return None
        if not rows:
            continue
        row = rows[0]
        inhabitants = row.get("AantalInwoners_5") or 0
        assistance = row.get("PersonenPerSoortUitkeringBijstand_87")
        parts = [
            _scale(row.get("GemiddeldeWOZWaardeVanWoningen_39"), 150, 600),     # x EUR 1000
            _scale(row.get("k_40HuishoudensMetLaagsteInkomen_84"), 70, 20),     # % households in lowest 40%
            _scale(assistance / inhabitants * 100 if assistance is not None and inhabitants else None, 10, 1),
        ]
        known = [p for p in parts if p is not None]
        score = round(sum(known) / len(known), 1) if known else None
        store.kv_set(f"cbs:{table}:{code}", json.dumps(score))
        return score
    return None
