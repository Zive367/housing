"""Message-style experiments: Thompson sampling picks the style that gets the most viewings."""

from __future__ import annotations

import random

from .store import Store

DEFAULT_VARIANTS = [
    ("en_concise", "en",
     "Short and businesslike, 4-6 sentences: who we are, why this home, move-in as soon as possible, "
     "long-term tenancy, ask for a viewing."),
    ("nl_concise", "nl",
     "Short and businesslike in Dutch, 4-6 sentences: who we are, why this home, move-in as soon as possible, "
     "long-term tenancy, ask for a viewing."),
    ("en_warm", "en",
     "Warm and personal, under 130 words: open with 1-2 concrete things about this home that appeal to us, "
     "say who we are, that we can move in right away and want to stay long-term, ask for a viewing."),
]


def ensure_defaults(store: Store) -> None:
    if not store.variants(active_only=False):
        for name, language, style in DEFAULT_VARIANTS:
            store.add_variant(name, language, style)


def choose(store: Store, languages: list[str], rng: random.Random | None = None) -> dict:
    """Sample each style's viewing rate from Beta(1+wins, 1+losses) and take the best draw."""
    rng = rng or random.Random()
    ensure_defaults(store)
    pool = [v for v in store.variants() if v["language"] in languages] or store.variants()
    return max(pool, key=lambda v: rng.betavariate(1 + v["wins"], 1 + v["losses"]))
