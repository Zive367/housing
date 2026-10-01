"""Weekly self-review: what actually gets viewings, and a new message style to test against the best one."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .llm import BudgetExceeded, LLMError
from .models import POSITIVE, Listing, Status

log = logging.getLogger(__name__)
MIN_OUTCOMES_TO_EVOLVE = 20


class NewStyle(BaseModel):
    name: str = Field(description="short snake_case name, max 20 characters")
    language: Literal["en", "nl"]
    style: str = Field(description="Writing instructions for the message, max 60 words")
    rationale: str = Field(description="One sentence: why this could beat the current best")


SYSTEM = """You improve the first message a tenant sends to letting agents in the Den Haag rental market, where
dozens of people reply within minutes. You get statistics per message style and example messages that did and
didn't lead to a viewing. Propose ONE new style to test against the current best: a concrete change (length,
opening line, language, what to emphasise), not a vague one. The style must stay honest: no invented facts, no
income amounts, no documents offered up front."""


def _speed_bucket(listing: Listing) -> str:
    s = listing.time_to_apply_s or 0
    return "< 2 min" if s < 120 else "2-10 min" if s < 600 else "10-60 min" if s < 3600 else "> 1 hour"


def _won(listing: Listing) -> bool:
    return listing.outcome == "win" or listing.status in POSITIVE


def breakdown(applied: list[Listing], title: str, key: Callable[[Listing], str]) -> list[list]:
    groups: dict[str, list[Listing]] = defaultdict(list)
    for listing in applied:
        groups[key(listing) or "(unknown)"].append(listing)
    rows = [[title, "applied", "viewing / positive reply", "rate %"]]
    for name, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        wins = sum(_won(x) for x in items)
        rows.append([name, len(items), wins, round(100 * wins / len(items), 1)])
    return rows + [[]]


def stats_rows(listings: list[Listing]) -> list[list]:
    applied = [x for x in listings if x.applied_at]
    blocked = sum(1 for x in listings if x.status == Status.LIKELY_SCAM)
    rows: list[list] = [["Updated", time.strftime("%Y-%m-%d %H:%M")],
                        ["Listings seen", len(listings)], ["Applications sent", len(applied)],
                        ["Likely scams blocked", blocked], []]
    for title, key in [("Message style", lambda x: x.variant), ("Source", lambda x: x.source),
                       ("Method", lambda x: x.apply_method), ("Applied as", lambda x: x.apply_as),
                       ("Speed", _speed_bucket), ("Area", lambda x: x.municipality)]:
        rows += breakdown(applied, title, key)
    rows += breakdown(applied, "Agency (top 15)", lambda x: x.agency)[:16]
    return rows


def evolve_styles(ctx) -> str:
    store = ctx.store
    listings = [x for x in store.all() if x.applied_at and x.outcome]
    if len(listings) < MIN_OUTCOMES_TO_EVOLVE or ctx.llm is None:
        return f"Not enough outcomes yet to change message styles ({len(listings)}/{MIN_OUTCOMES_TO_EVOLVE})."

    variants = store.variants()

    def mean(v: dict) -> float:
        return (1 + v["wins"]) / (2 + v["wins"] + v["losses"])

    ranked = sorted(variants, key=mean)
    worst, best = ranked[0], ranked[-1]
    notes = []
    if len(variants) > 2 and worst["wins"] + worst["losses"] >= 10 and mean(best) - mean(worst) >= 0.1:
        store.retire_variant(worst["name"])
        notes.append(f"Retired style '{worst['name']}' ({mean(worst):.0%} vs best {mean(best):.0%}).")

    wins = [x.message for x in listings if x.outcome == "win"][:3]
    losses = [x.message for x in listings if x.outcome == "loss"][:3]
    prompt = (f"Styles (name, language, instructions, viewings, no viewing):\n"
              f"{json.dumps([[v['name'], v['language'], v['style'], v['wins'], v['losses']] for v in variants])}\n\n"
              f"Messages that got a viewing:\n{json.dumps(wins, ensure_ascii=False)}\n\n"
              f"Messages that didn't:\n{json.dumps(losses, ensure_ascii=False)}\n\n"
              f"Allowed languages: {ctx.cfg.apply.languages}")
    try:
        new = ctx.llm.parse(NewStyle, SYSTEM, prompt, effort="medium", max_tokens=6000)
    except (LLMError, BudgetExceeded) as e:
        return " ".join(notes + [f"Couldn't generate a new style: {e}"])
    if new.language not in ctx.cfg.apply.languages:
        return " ".join(notes + [f"Proposed style skipped (language {new.language} not enabled)."])
    name = f"{date.today():%m%d}_{new.name}"[:28]
    store.add_variant(name, new.language, new.style)
    notes.append(f"Testing new style '{name}': {new.rationale}")
    return " ".join(notes)


def weekly(ctx) -> str:
    listings = ctx.store.all()
    if ctx.sheet:
        try:
            ctx.sheet.write_stats(stats_rows(listings))
        except Exception as e:  # stats are nice-to-have; never crash the bot over them
            log.warning("could not write stats: %s", e)
    applied = [x for x in listings if x.applied_at]
    wins = sum(_won(x) for x in applied)
    summary = (f"So far: {len(applied)} applications, {wins} viewing invites or positive replies "
               f"({(100 * wins / len(applied)) if applied else 0:.0f}%). ") + evolve_styles(ctx)
    ctx.notifier.send("Weekly review", summary)
    return summary
