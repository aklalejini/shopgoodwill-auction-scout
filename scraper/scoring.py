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


def _weight_in_pounds(text: str) -> float | None:
    """Extract the largest plainly stated pound weight from listing copy."""
    matches = re.findall(r"\b(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds)\b", text, re.IGNORECASE)
    return max((float(value) for value in matches), default=None)


def _shipping_adjustment(
    listing: dict[str, Any], text: str, config: dict[str, Any]
) -> tuple[int, list[str], bool]:
    """Score delivered-cost certainty and return whether shipping is unusually favorable."""
    rules = config.get("shipping_rules", {})
    shipping = listing.get("shipping") or {}
    if not shipping or not rules:
        return 0, [], False

    points = 0
    reasons: list[str] = []
    favorable = False
    listed_price = float(shipping.get("listed_price") or 0)
    handling_price = float(shipping.get("handling_price") or 0)

    if shipping.get("pickup_only"):
        adjustment = int(rules.get("pickup_only_points", -25))
        points += adjustment
        reasons.append(f"Pickup-only limits the opportunity ({adjustment})")
        return points, reasons, False

    penny_maximum = float(rules.get("penny_flat_maximum", 0.01))
    if 0 < listed_price <= penny_maximum:
        adjustment = int(rules.get("penny_flat_points", 0))
        points += adjustment
        favorable = True
        reasons.append(f"Flat shipping is only ${listed_price:.2f} (+{adjustment})")
    elif listed_price > 0:
        known_tier = _first_matching_tier(
            listed_price, rules.get("known_price_bonuses", []), "maximum"
        )
        if known_tier:
            adjustment = int(known_tier["points"])
            points += adjustment
            reasons.append(
                f"Known shipping cost at or below ${known_tier['maximum']} (+{adjustment})"
            )
    elif listing.get("detail_status") == "complete":
        adjustment = int(rules.get("calculated_shipping_points", 0))
        points += adjustment
        reasons.append(f"Shipping is calculated and not included in the price ({adjustment})")

    handling_threshold = float(rules.get("handling_penalty_minimum", 999999))
    if handling_price >= handling_threshold:
        adjustment = int(rules.get("handling_penalty_points", 0))
        points += adjustment
        reasons.append(f"${handling_price:.2f} handling charge ({adjustment})")

    weight = _weight_in_pounds(text)
    heavy_threshold = float(rules.get("heavy_weight_minimum", 999999))
    if weight is not None and weight >= heavy_threshold:
        if favorable:
            adjustment = int(rules.get("heavy_with_flat_shipping_points", 0))
            points += adjustment
            reasons.append(
                f"{weight:g} lb lot has unusually favorable flat shipping (+{adjustment})"
            )
        elif listed_price == 0 and listing.get("detail_status") == "complete":
            adjustment = int(rules.get("heavy_with_calculated_shipping_points", 0))
            points += adjustment
            reasons.append(
                f"{weight:g} lb lot has unresolved shipping cost ({adjustment})"
            )

    return points, reasons, favorable


def score_listing(listing: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return score, human-readable reasons, and matched mineral terms."""
    title = str(listing.get("title") or "")
    description = str(listing.get("description") or "")
    text = f"{title}\n{description}"
    score = int(config.get("base_score", 0))
    reasons: list[str] = []
    matched_minerals: list[str] = []
    matched_premium: list[str] = []
    matched_evidence: list[str] = []
    risk_points = 0
    domain_signal = any(_contains(text, phrase) for phrase in config.get("domain_keywords", []))

    groups = (
        ("priority_keywords", "Opportunity term"),
        ("lower_priority_keywords", "Lower-priority term"),
        ("target_keywords", "Target keyword"),
        ("premium_keywords", "High-value signal"),
    )
    group_caps = config.get("keyword_group_caps", {})
    for group_name, label in groups:
        if (
            group_name == "priority_keywords"
            and config.get("require_domain_signal_for_priority_keywords", False)
            and not domain_signal
        ):
            continue
        group_points = 0
        for phrase, raw_points in config.get(group_name, {}).items():
            if not _contains(text, phrase):
                continue
            points = int(raw_points)
            group_points += points
            if points < 0:
                risk_points += points
            location = "title" if _contains(title, phrase) else "description"
            sign = "+" if points >= 0 else ""
            reasons.append(f"{label} '{phrase}' in {location} ({sign}{points})")
            if group_name == "target_keywords":
                matched_minerals.append(phrase)
            elif group_name == "premium_keywords":
                matched_premium.append(phrase)

        cap = group_caps.get(group_name)
        if cap is not None and group_points > int(cap):
            reduction = group_points - int(cap)
            group_points = int(cap)
            reasons.append(f"{label} bonuses capped to prevent keyword stacking (-{reduction})")
        score += group_points

    for phrase, raw_points in config.get("collector_evidence_keywords", {}).items():
        if not _contains(text, phrase):
            continue
        points = int(raw_points)
        score += points
        matched_evidence.append(phrase)
        location = "title" if _contains(title, phrase) else "description"
        reasons.append(f"Collector evidence '{phrase}' in {location} (+{points})")

    category = str(listing.get("category") or "")
    for phrase, raw_points in config.get("category_penalties", {}).items():
        if not _contains(category, phrase):
            continue
        points = int(raw_points)
        score += points
        if points < 0:
            risk_points += points
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

    photo_penalty = _first_matching_tier(
        photo_count, config.get("photo_penalties", []), "maximum"
    )
    if photo_penalty:
        points = int(photo_penalty["points"])
        score += points
        risk_points += points
        reasons.append(f"Only {photo_count} listing photo{'s' if photo_count != 1 else ''} ({points})")

    shipping_points, shipping_reasons, favorable_shipping = _shipping_adjustment(
        listing, text, config
    )
    score += shipping_points
    reasons.extend(shipping_reasons)
    if shipping_points < 0:
        risk_points += shipping_points

    strong_collection_language = any(
        _contains(text, phrase) for phrase in config.get("strong_collection_keywords", [])
    )
    high_priority_eligible = (
        photo_count >= int(config.get("high_priority_minimum_photos", 4))
        and not bool((listing.get("shipping") or {}).get("pickup_only"))
        and (
            bool(matched_premium)
            or bool(matched_evidence)
            or bool(matched_minerals)
            or strong_collection_language
            or (favorable_shipping and (_weight_in_pounds(text) or 0) >= 4)
        )
        and risk_points >= int(config.get("high_priority_risk_floor", -24))
    )

    return {
        "score": max(0, min(100, score)),
        "score_reasons": reasons or ["No scoring signals beyond the base score"],
        "matched_keywords": sorted(set(matched_minerals)),
        "matched_minerals": sorted(set(matched_minerals)),
        "high_priority_eligible": high_priority_eligible,
    }
