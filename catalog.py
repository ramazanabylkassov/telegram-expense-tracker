"""The shared category list + spend dictionary, cached in memory and refreshed from BigQuery.

Used on every mapping in two ways:
  1. The known variants are given to the model as examples ("starbucks -> Eating out").
  2. After the model answers, an exact dictionary hit overrides its category guess,
     because a variant users have confirmed beats a fresh guess.
Every save teaches the dictionary (MERGE into dim_spend_variants), so it improves for all users.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import config
from storage import Warehouse, normalise

log = logging.getLogger(__name__)


@dataclass
class Category:
    id: int
    name: str  # English; used by the model, the dictionary and fact rows
    description: str
    name_ru: str = ""

    def label(self, lang: str) -> str:
        """The name to show a user."""
        return (self.name_ru or self.name) if lang == "ru" else self.name


class Catalog:
    def __init__(self, warehouse: Warehouse):
        self.wh = warehouse
        self.categories: list[Category] = []
        # (variant, type) -> {category_id: score}
        self.variants: dict[tuple[str, str], dict[int, float]] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    # ---- loading ------------------------------------------------------------ #

    def load(self):
        cats = self.wh.load_categories()
        if not cats:
            raise RuntimeError("dim_categories has no active categories")
        self.categories = [
            Category(c["category_id"], c["name"], c["description"], c.get("name_ru") or "") for c in cats
        ]
        active = {c.id for c in self.categories}
        variants: dict[tuple[str, str], dict[int, float]] = defaultdict(dict)
        for v in self.wh.load_variants():
            if v["category_id"] not in active:
                continue
            # A seeded variant counts as one confirmation; learned ones count what users confirmed.
            score = max(v["confirmations"], 1 if v["source"] == "seed" else 0)
            if score > 0:
                variants[(v["variant"], v["variant_type"])][v["category_id"]] = score
        self.variants = dict(variants)
        self._loaded_at = time.monotonic()
        log.info("Catalog: %d categories, %d spend variants", len(self.categories), len(self.variants))

    async def refresh_if_stale(self, force: bool = False):
        if not force and time.monotonic() - self._loaded_at < config.CATALOG_REFRESH_SECONDS:
            return
        async with self._lock:
            if not force and time.monotonic() - self._loaded_at < config.CATALOG_REFRESH_SECONDS:
                return
            try:
                await asyncio.to_thread(self.load)
            except Exception:
                if not self.categories:
                    raise
                log.exception("Catalog refresh failed; keeping the cached copy")

    # ---- lookups ------------------------------------------------------------ #

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.categories]

    def by_name(self, name: str) -> Category | None:
        return next((c for c in self.categories if c.name == name), None)

    def by_id(self, category_id: int) -> Category | None:
        return next((c for c in self.categories if c.id == category_id), None)

    def fallback(self) -> Category:
        return self.by_name("Other") or self.categories[-1]

    def _best(self, key: tuple[str, str]) -> int | None:
        scores = self.variants.get(key)
        if not scores:
            return None
        return max(scores.items(), key=lambda kv: kv[1])[0]

    def match(self, merchant: str | None, description: str | None) -> Category | None:
        """Dictionary lookup: merchant first (most specific), then the item."""
        m, d = normalise(merchant), normalise(description)
        for key in ((m, "merchant"), (d, "item")):
            if key[0]:
                cid = self._best(key)
                if cid is not None:
                    return self.by_id(cid)
        # Known merchant inside a longer merchant name, e.g. "wolt" in "wolt kazakhstan".
        # (Only the merchant field: "small" the grocery chain must not match "small gift".)
        haystack = f" {m} "
        hits = [
            (max(scores.values()), variant, self._best((variant, vtype)))
            for (variant, vtype), scores in self.variants.items()
            if scores and vtype == "merchant" and len(variant) >= 3 and f" {variant} " in haystack
        ]
        if hits:
            _, _, cid = max(hits)
            return self.by_id(cid)
        return None

    # ---- prompt material ---------------------------------------------------- #

    def category_guide(self) -> str:
        return "\n".join(f"- {c.name}: {c.description}" for c in self.categories)

    def examples(self, limit: int = config.PROMPT_VARIANTS_LIMIT) -> str:
        """Most-confirmed variants, as 'variant -> Category' lines for the prompt."""
        ranked = sorted(
            ((max(s.values()), v, t, self._best((v, t))) for (v, t), s in self.variants.items() if s),
            reverse=True,
        )[:limit]
        lines = []
        for _, variant, vtype, cid in ranked:
            cat = self.by_id(cid)
            if cat:
                lines.append(f"{variant} ({vtype}) -> {cat.name}")
        return "\n".join(lines)

    # ---- learning ----------------------------------------------------------- #

    @staticmethod
    def keys_for(merchant: str | None, description: str | None) -> list[tuple[str, str]]:
        keys = []
        if normalise(merchant):
            keys.append((normalise(merchant), "merchant"))
        if normalise(description):
            keys.append((normalise(description), "item"))
        return keys

    async def learn(self, keys: list[tuple[str, str]], category: Category, corrected: bool):
        if not keys:
            return
        # Update the in-memory copy right away so the next message benefits…
        for key in keys:
            scores = self.variants.setdefault(key, {})
            scores[category.id] = scores.get(category.id, 0) + 1
        # …and persist for everyone.
        await asyncio.to_thread(self.wh.learn_variants, keys, category.id, corrected)


    async def unlearn(self, keys: list[tuple[str, str]], category_id: int, corrected: bool):
        for key in keys:
            scores = self.variants.get(tuple(key))
            if scores and category_id in scores:
                scores[category_id] = max(scores[category_id] - 1, 0)
                if scores[category_id] == 0:
                    del scores[category_id]
                if not scores:
                    del self.variants[tuple(key)]
        await asyncio.to_thread(self.wh.unlearn_variants, [tuple(k) for k in keys], category_id, corrected)
