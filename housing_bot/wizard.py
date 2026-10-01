"""`python -m housing_bot init`: asks the setup questions and writes .env and config.yaml for you."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from getpass import getpass
from pathlib import Path

import httpx

from .fetch import normalize_phone
from .geo import geocode_address

Ask = Callable[[str], str]


def _ask(ask: Ask, label: str, default: str = "", required: bool = False, check=None) -> str:
    while True:
        shown = f" [{default}]" if default else ""
        value = ask(f"{label}{shown}: ").strip() or default
        if required and not value:
            print("  required")
            continue
        if value and check:
            error = check(value)
            if error:
                print(f"  {error}")
                continue
        return value


def _email(value: str) -> str:
    return "" if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", value, re.I) else "not an email address"


def _money(value: str) -> str:
    try:
        float(value.replace(",", "."))
        return ""
    except ValueError:
        return "enter a number, e.g. 4200"


def _phone(value: str) -> str:
    return "" if normalize_phone(value) else "not a valid phone number"


def _set_line(text: str, key: str, value: str) -> str:
    """Replace the first `key: ...` line, keeping its indentation and trailing comment."""
    pattern = re.compile(rf"^(\s*{re.escape(key)}:\s*)([^#\n]*?)(\s*#.*)?$", re.M)
    if not pattern.search(text):
        raise KeyError(key)
    return pattern.sub(lambda m: f"{m.group(1)}{value}{m.group(3) or ''}", text, count=1)


def run(root: Path, ask: Ask = input, secret: Ask = getpass, client: httpx.Client | None = None) -> None:
    env_path, config_path = root / ".env", root / "config.yaml"
    if any(p.exists() and p.stat().st_size for p in (env_path, config_path)):  # empty placeholders don't count
        if ask(f"{env_path.name} / {config_path.name} already exist. Overwrite? [y/N]: ").strip().lower() != "y":
            print("Nothing changed.")
            return
    client = client or httpx.Client(timeout=20)
    print("\nAnswers stay on this machine, in .env and config.yaml (both never committed).\n")

    print("== Mailbox ==")
    gmail = _ask(ask, "Gmail address the bot uses", required=True, check=_email)
    user, _, domain = gmail.partition("@")
    env = {
        "BOT_EMAIL": gmail,
        "BOT_EMAIL_APP_PASSWORD": secret("Gmail app password (16 letters, input hidden): ").replace(" ", ""),
        "HOUSING_ADDRESS": _ask(ask, "Address agents see and reply to (the bot only reads mail to this address "
                                     "and from the housing sites)", f"{user}+housing@{domain}", check=_email),
        "IMAP_HOST": "imap.gmail.com", "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
    }
    print("\n== Claude, Google Sheet, notifications ==")
    env["ANTHROPIC_API_KEY"] = secret("Anthropic API key (input hidden): ").strip()
    env["GOOGLE_SERVICE_ACCOUNT_FILE"] = _ask(ask, "Service account key file",
                                              "secrets/google-service-account.json")
    if not (root / env["GOOGLE_SERVICE_ACCOUNT_FILE"]).exists():
        print("  (file not there yet: copy it in before starting the bot)")
    sheet = _ask(ask, "Google Sheet link or ID", required=True)
    match = re.search(r"/d/([A-Za-z0-9_-]+)", sheet)
    env["GOOGLE_SHEET_ID"] = match.group(1) if match else sheet
    env["NOTIFY_EMAIL"] = _ask(ask, "Email address for the bot's alerts to you", gmail, check=_email)

    print("\n== You ==")
    env["APPLICANT_FIRST_NAME"] = _ask(ask, "First name", required=True)
    env["APPLICANT_LAST_NAME"] = _ask(ask, "Last name", required=True)
    env["APPLICANT_PHONE"] = normalize_phone(_ask(ask, "Mobile number", required=True, check=_phone)) or ""
    env["APPLICANT_AGE"] = _ask(ask, "Age")
    env["APPLICANT_JOB_TITLE"] = _ask(ask, "Job title")
    env["APPLICANT_EMPLOYER"] = _ask(ask, "Employer to mention in messages (empty = don't mention)")
    env["APPLICANT_CONTRACT"] = _ask(ask, "Contract: permanent / temporary / self-employed", "permanent")
    env["APPLICANT_NATIONALITY"] = _ask(ask, "Nationality")
    env["APPLICANT_ABOUT"] = _ask(ask, "One line about you", "non-smoker, no pets, quiet and tidy")
    env["APPLICANT_GROSS_MONTHLY_INCOME"] = _ask(
        ask, "Gross monthly income in EUR (internal only, never sent)", check=_money)

    print("\n== Partner (leave first name empty to always apply alone) ==")
    env["PARTNER_FIRST_NAME"] = _ask(ask, "Partner's first name")
    for key, label in [("PARTNER_AGE", "Partner's age"), ("PARTNER_JOB_TITLE", "Partner's job title"),
                       ("PARTNER_CONTRACT", "Partner's contract"),
                       ("PARTNER_GROSS_MONTHLY_INCOME", "Partner's gross monthly income (internal only)")]:
        env[key] = _ask(ask, label, check=_money if "INCOME" in key else None) if env["PARTNER_FIRST_NAME"] else ""

    print("\n== Search ==")
    while True:
        work = _ask(ask, "Work address (street + number + city)", required=True)
        found = geocode_address(client, work)
        if found:
            print(f"  found: {found[0]:.5f}, {found[1]:.5f}")
            break
        print("  not found in the Dutch address register, try 'Street 12, 1234 AB City'")
    max_rent = _ask(ask, "Maximum rent incl. service costs", "1500", check=_money)
    preferred = _ask(ask, "Preferred rent (ranks higher)", "1400", check=_money)
    solo = _ask(ask, "Minimum m² living alone", "25", check=_money)
    couple = _ask(ask, "Minimum m² living together", "35", check=_money)
    languages = _ask(ask, "Message languages: 'nl,en', 'nl' or 'en'", "nl,en")

    config = (root / "config.example.yaml").read_text()
    config = _set_line(config, "address", f'"{work}"')
    config = config.replace(f'  address: "{work}"', f'  address: "{work}"\n  lat: {found[0]}\n  lon: {found[1]}', 1)
    for key, value in [("max_rent", max_rent), ("preferred_rent", preferred), ("min_size_m2_solo", solo),
                       ("min_size_m2_couple", couple)]:
        config = _set_line(config, key, value)
    langs = [x.strip() for x in languages.split(",") if x.strip() in ("nl", "en")] or ["nl", "en"]
    config = _set_line(config, "languages", f"[{', '.join(langs)}]")

    lines = ["# Written by `python -m housing_bot init`. Never commit this file."]
    lines += [f"{k}={v}" for k, v in env.items()]
    env_path.write_text("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)
    config_path.write_text(config.replace(
        "# Copy to `config.yaml` (gitignored) and adjust.", "# Written by `python -m housing_bot init`.", 1))
    (root / "secrets" / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    print(f"\nWrote {env_path} and {config_path}. Dry-run mode is on: nothing is sent until you set "
          "apply.dry_run: false in config.yaml.\nNext: python -m housing_bot check")
