"""Settings you (and your partner) can change from the dashboard.

Saved to data/settings.json, which is applied on top of config.yaml and .env at start-up and immediately when
saved. Secrets (API key, passwords) are deliberately not editable here.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .fetch import normalize_phone

MUNICIPALITIES = ["'s-Gravenhage", "Rijswijk", "Leidschendam-Voorburg", "Delft", "Wassenaar",
                  "Pijnacker-Nootdorp", "Zoetermeer", "Westland"]
MUNICIPALITY_LABELS = {"'s-Gravenhage": "Den Haag", "Leidschendam-Voorburg": "Voorburg / Leidschendam"}


@dataclass
class Field:
    key: str            # "section.attribute" on Config, e.g. "applicant.first_name"
    label: str
    kind: str = "text"  # text | number | bool | choice | multi | textarea | phone
    group: str = ""
    options: list[str] = field(default_factory=list)
    lo: float | None = None
    hi: float | None = None
    hint: str = ""


FIELDS = [
    Field("applicant.first_name", "First name", group="you"),
    Field("applicant.last_name", "Last name", group="you"),
    Field("secrets.applicant_phone", "Phone agents can call", "phone", "you"),
    Field("applicant.age", "Age", group="you"),
    Field("applicant.job_title", "Job title", group="you"),
    Field("applicant.employer", "Employer (empty = not mentioned)", group="you"),
    Field("applicant.contract", "Contract", "choice", "you", ["permanent", "temporary", "self-employed"]),
    Field("applicant.nationality", "Nationality", group="you"),
    Field("applicant.about", "About you (one line)", "textarea", "you"),
    Field("applicant.gross_monthly_income", "Gross monthly income €", "number", "you", lo=0, hi=100000,
          hint="Only used to decide alone/together. Never sent to anyone."),
    Field("partner.first_name", "First name (empty = always apply alone)", group="partner"),
    Field("partner.age", "Age", group="partner"),
    Field("partner.job_title", "Job title", group="partner"),
    Field("partner.contract", "Contract", "choice", "partner", ["", "permanent", "temporary", "self-employed"]),
    Field("partner.gross_monthly_income", "Gross monthly income €", "number", "partner", lo=0, hi=100000,
          hint="Only used to decide alone/together. Never sent to anyone."),
    Field("search.max_rent", "Maximum rent € (incl. service costs)", "number", "search", lo=300, hi=5000),
    Field("search.preferred_rent", "Preferred rent € (ranks higher)", "number", "search", lo=300, hi=5000),
    Field("search.min_size_m2_solo", "Minimum m² living alone", "number", "search", lo=0, hi=300),
    Field("search.min_size_m2_couple", "Minimum m² living together", "number", "search", lo=0, hi=300),
    Field("search.max_distance_km", "Max distance to work (km)", "number", "search", lo=1, hi=60),
    Field("search.municipalities", "Areas", "multi", "search", MUNICIPALITIES),
    Field("search.prefer_furnished", "Prefer furnished", "bool", "search"),
    Field("search.require_registration", "Must be able to register at the address", "bool", "search"),
    Field("search.exclude_rooms", "Skip rooms in shared houses", "bool", "search"),
    Field("search.min_neighbourhood_score", "Minimum area score (0 = off)", "number", "search", lo=0, hi=10),
    Field("apply.languages", "Message languages", "multi", "bot", ["nl", "en"]),
    Field("apply.dry_run", "Practice mode (write messages, send nothing)", "bool", "bot"),
    Field("llm.daily_budget_usd", "Daily Claude budget $", "number", "bot", lo=0.5, hi=50),
]
GROUPS = {"you": "You", "partner": "Partner", "search": "What we're looking for", "bot": "Bot"}


def path() -> Path:
    from .config import DATA_DIR
    return DATA_DIR / "settings.json"


def get(cfg: Config, key: str):
    section, attr = key.split(".")
    return getattr(getattr(cfg, section), attr)


def _set(cfg: Config, key: str, value) -> None:
    section, attr = key.split(".")
    setattr(getattr(cfg, section), attr, value)


def parse(form: dict[str, list[str]], partner_label: str = "Partner") -> tuple[dict, list[str]]:
    """Form values -> typed overrides, plus human-readable errors."""
    values: dict = {}
    errors: list[str] = []
    for f in FIELDS:
        raw = [v.strip() for v in form.get(f.key, [])]
        label = f"{GROUPS[f.group] if f.group != 'partner' else partner_label}: {f.label}"
        if f.kind == "bool":
            values[f.key] = bool(raw and raw[-1] in ("on", "true", "1"))
        elif f.kind == "multi":
            chosen = [v for v in raw if v in f.options]
            if not chosen:
                errors.append(f"{label}: pick at least one")
            values[f.key] = chosen
        elif f.kind == "number":
            text = (raw[0] if raw else "").replace(",", ".").replace("€", "")
            if not text:
                if f.key.endswith("income"):
                    values[f.key] = None
                elif f.key.endswith("neighbourhood_score"):
                    values[f.key] = 0
                else:
                    errors.append(f"{label}: required")
                continue
            try:
                number = float(text)
            except ValueError:
                errors.append(f"{label}: not a number")
                continue
            if (f.lo is not None and number < f.lo) or (f.hi is not None and number > f.hi):
                errors.append(f"{label}: must be between {f.lo:g} and {f.hi:g}")
                continue
            values[f.key] = number
        elif f.kind == "phone":
            text = raw[0] if raw else ""
            phone = normalize_phone(text) if text else ""
            if text and not phone:
                errors.append(f"{label}: not a valid phone number")
                continue
            values[f.key] = phone
        elif f.kind == "choice":
            text = raw[0] if raw else ""
            if text not in f.options:
                errors.append(f"{label}: invalid choice")
                continue
            values[f.key] = text
        else:
            values[f.key] = (raw[0] if raw else "")[:300]
    return values, errors


def apply(cfg: Config, values: dict) -> None:
    for key, value in values.items():
        if any(f.key == key for f in FIELDS):
            _set(cfg, key, value)


def load(cfg: Config) -> None:
    """Apply saved dashboard settings on top of config.yaml / .env."""
    p = path()
    if p.exists():
        try:
            apply(cfg, json.loads(p.read_text()))
        except (ValueError, OSError):
            pass  # a broken file must never stop the bot; the dashboard will overwrite it on the next save


def save(cfg: Config, values: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".settings")
    with os.fdopen(fd, "w") as fh:
        json.dump(values, fh, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)   # atomic: a crash mid-save never leaves half a file
    apply(cfg, values)
