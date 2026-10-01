"""Configuration: search criteria from config.yaml, secrets and personal data from .env."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("HOUSING_DATA_DIR", ROOT / "data"))
SECRETS_DIR = Path(os.environ.get("HOUSING_SECRETS_DIR", ROOT / "secrets"))


class Work(BaseModel):
    address: str
    lat: float | None = None
    lon: float | None = None


class Search(BaseModel):
    max_rent: float = 1500
    preferred_rent: float = 1400
    municipalities: list[str] = Field(default_factory=list)
    max_distance_km: float = 12
    min_size_m2_solo: float = 25
    min_size_m2_couple: float = 35
    prefer_furnished: bool = True
    require_registration: bool = True
    exclude_rooms: bool = True
    exclude_students_only: bool = True
    min_contract_months: int = 6
    min_neighbourhood_score: float = 0


class Income(BaseModel):
    default_multiplier: float = 3.5
    couple_preferred_size_m2: float = 45


class PermitBand(BaseModel):
    max_rent: float
    income_max_single: float
    income_max_multi: float


class PermitRule(BaseModel):
    municipality: str
    bands: list[PermitBand]


class Scam(BaseModel):
    block_score: int = 70
    warn_score: int = 40
    expected_price_per_m2: dict[str, float] = Field(default_factory=lambda: {"default": 20})
    trusted_domains: list[str] = Field(default_factory=list)


class Alerts(BaseModel):
    sender_domains: list[str] = Field(default_factory=list)


class Apply(BaseModel):
    dry_run: bool = True
    languages: list[str] = Field(default_factory=lambda: ["en", "nl"])
    max_per_hour: int = 20
    max_per_agency_per_day: int = 5
    browser_forms: bool = True
    manual_only_domains: list[str] = Field(default_factory=list)


class LLMConfig(BaseModel):
    model: str = "claude-opus-5-5"
    bulk_model: str = "claude-haiku-4-5"
    daily_budget_usd: float = 5.0


class Schedule(BaseModel):
    timezone: str = "Europe/Amsterdam"
    inbox_poll_seconds: int = 20
    digest_time: str = "08:00"
    weekly_learn_day: str = "sun"
    weekly_learn_time: str = "20:00"
    no_response_days: int = 4


class Neighbourhood(BaseModel):
    cbs_table: str = "85984NED"


class Person(BaseModel):
    first_name: str = ""
    last_name: str = ""
    age: str = ""
    job_title: str = ""
    employer: str = ""
    contract: str = ""
    nationality: str = ""
    about: str = ""
    gross_monthly_income: float | None = None

    @property
    def present(self) -> bool:
        return bool(self.first_name)


class Secrets(BaseModel):
    anthropic_api_key: str = ""
    bot_email: str = ""
    bot_email_app_password: str = ""
    # Address agents see and reply to, e.g. you+housing@gmail.com. The bot only reads mail sent to it
    # (plus alerts from the housing sites), so the rest of a personal inbox is never touched.
    housing_address: str = ""
    dashboard_password: str = ""
    dashboard_port: int = 8080
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    google_service_account_file: str = ""
    google_sheet_id: str = ""
    notify_email: str = ""
    applicant_phone: str = ""

    @property
    def contact_email(self) -> str:
        return self.housing_address or self.bot_email


class Config(BaseModel):
    work: Work
    search: Search = Field(default_factory=Search)
    income: Income = Field(default_factory=Income)
    permits: list[PermitRule] = Field(default_factory=list)
    scam: Scam = Field(default_factory=Scam)
    alerts: Alerts = Field(default_factory=Alerts)
    apply: Apply = Field(default_factory=Apply)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    schedule: Schedule = Field(default_factory=Schedule)
    neighbourhood: Neighbourhood = Field(default_factory=Neighbourhood)
    # Filled from the environment, never from config.yaml
    secrets: Secrets = Field(default_factory=Secrets)
    applicant: Person = Field(default_factory=Person)
    partner: Person = Field(default_factory=Person)


def _float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ".").replace("€", "").strip())
    except ValueError:
        return None


def _person(prefix: str) -> Person:
    env = os.environ
    return Person(
        first_name=env.get(f"{prefix}_FIRST_NAME", ""),
        last_name=env.get(f"{prefix}_LAST_NAME", ""),
        age=env.get(f"{prefix}_AGE", ""),
        job_title=env.get(f"{prefix}_JOB_TITLE", ""),
        employer=env.get(f"{prefix}_EMPLOYER", ""),
        contract=env.get(f"{prefix}_CONTRACT", ""),
        nationality=env.get(f"{prefix}_NATIONALITY", ""),
        about=env.get(f"{prefix}_ABOUT", ""),
        gross_monthly_income=_float(env.get(f"{prefix}_GROSS_MONTHLY_INCOME")),
    )


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path or os.environ.get("HOUSING_CONFIG", ROOT / "config.yaml"))
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy config.example.yaml to config.yaml and edit it.")
    raw = yaml.safe_load(path.read_text()) or {}
    cfg = Config.model_validate(raw)

    env = os.environ
    secret_fields = Secrets.model_fields
    cfg.secrets = Secrets(
        **{name: env[name.upper()] for name in secret_fields if env.get(name.upper())}
    )
    cfg.applicant = _person("APPLICANT")
    cfg.partner = _person("PARTNER")
    return cfg
