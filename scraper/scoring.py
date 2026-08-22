"""Editable, explainable opportunity scoring."""

from __future__ import annotations

import re
from typing import Any


def _contains(text: str, phrase: str) -> bool:
    """Match phrases on word boundaries so e.g. 'lot' does not match 'pilot'."""
    pattern = r"(?<![a-z0-9])" + re.escape(phrase.casefold()) + r"(?![a-z0-9])"
    return re.search(pattern, text.casefold()) is not None


def _first_matching_tier(value: float, tiers: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for tier in tiers:
        if key == "maximum" and value <= float(tier[key]):
            return tier
        if key == "minimum" and value >= float(tier[key]):
            return tier
    return None


def score_listing(listing: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return score, human-readable reasons, and matched mineral terms."""
    title = str(listing.get("title") or "")
    description = str(listing.get("description") or "")
    text = f"{title}\n{description}"
    score = int(config.get("base_score", 0))
    reasons: list[str] = []
    matched_minerals: list[str] = []
    domain_signal = any(_contains(text, phrase) for phrase in config.get("domain_keywords", []))

    groups = (
        ("priority_keywords", "Opportunity term"),
        ("lower_priority_keywords", "Lower-priority term"),
        ("target_keywords", "Target keyword"),
        ("premium_keywords", "High-value signal"),
    )
    for group_name, label in groups:
        if (
            group_name == "priority_keywords"
            and config.get("require_domain_signal_for_priority_keywords", False)
            and not domain_signal
        ):
            continue
        for phrase, raw_points in config.get(group_name, {}).items():
            if not _contains(text, phrase):
                continue
            points = int(raw_points)
            score += points
            location = "title" if _contains(title, phrase) else "description"
            sign = "+" if points >= 0 else ""
            reasons.append(f"{label} '{phrase}' in {location} ({sign}{points})")
            if group_name == "target_keywords":
                matched_minerals.append(phrase)

    category = str(listing.get("category") or "")
    for phrase, raw_points in config.get("category_penalties", {}).items():
        if not _contains(category, phrase):
            continue
        points = int(raw_points)
        score += points
        reasons.append(f"Less relevant category '{phrase}' ({points})")

    price = float(listing.get("price") or 0)
    price_tier = _first_matching_tier(price, config.get("price_bonuses", []), "maximum")
    if price_tier:
        points = int(price_tier["points"])
        score += points
        reasons.append(f"Price at or below ${price_tier['maximum']} (+{points})")

    bids = int(listing.get("bids") or 0)
    bid_tier = _first_matching_tier(bids, config.get("bid_bonuses", []), "maximum")
    if bid_tier:
        points = int(bid_tier["points"])
        score += points
        reasons.append(f"{bids} bid{'s' if bids != 1 else ''} (+{points})")

    photo_count = len(listing.get("images") or [])
    photo_tier = _first_matching_tier(photo_count, config.get("photo_bonuses", []), "minimum")
    if photo_tier:
        points = int(photo_tier["points"])
        score += points
        reasons.append(f"{photo_count} listing photos (+{points})")

    return {
        "score": max(0, min(100, score)),
        "score_reasons": reasons or ["No scoring signals beyond the base score"],
        "matched_keywords": sorted(set(matched_minerals)),
        "matched_minerals": sorted(set(matched_minerals)),
    }
