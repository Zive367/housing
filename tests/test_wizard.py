import shutil
from pathlib import Path

import yaml

from housing_bot import wizard
from housing_bot.config import Config

ROOT = Path(__file__).resolve().parent.parent


def test_wizard_writes_env_and_config(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.example.yaml")
    monkeypatch.setattr(wizard, "geocode_address", lambda client, address: (52.08148, 4.32340))
    answers = iter([
        "bot.housing@gmail.com",                                 # bot email
        "", "https://docs.google.com/spreadsheets/d/AbC123_x/edit", "me@example.com",
        "Sam", "Jansen", "06 12345678", "30", "analyst", "", "", "Dutch", "", "4500",
        "Alex", "29", "designer", "permanent", "3500",
        "Koningin Julianaplein 10, 2595 AA Den Haag", "", "1350", "", "40", "nl",
    ])
    wizard.run(tmp_path, ask=lambda prompt: next(answers), secret=lambda prompt: "abcd efgh ijkl mnop")

    env = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    assert env["BOT_EMAIL_APP_PASSWORD"] == "abcdefghijklmnop"
    assert env["GOOGLE_SHEET_ID"] == "AbC123_x"
    assert env["APPLICANT_PHONE"] == "+31612345678"
    assert env["APPLICANT_CONTRACT"] == "permanent" and env["PARTNER_GROSS_MONTHLY_INCOME"] == "3500"
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600

    cfg = Config.model_validate(yaml.safe_load((tmp_path / "config.yaml").read_text()))
    assert cfg.work.address.startswith("Koningin Julianaplein") and cfg.work.lat == 52.08148
    assert cfg.search.max_rent == 1500 and cfg.search.preferred_rent == 1350
    assert cfg.search.min_size_m2_couple == 40 and cfg.apply.languages == ["nl"]
    assert cfg.permits[0].bands[1].max_rent == 1228.07          # permit bands untouched
    assert (tmp_path / "secrets" / "sessions").is_dir()
