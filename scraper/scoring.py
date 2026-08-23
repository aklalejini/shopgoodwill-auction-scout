"""Editable, explainable opportunity scoring."""

from __future__ import annotations

import re
from typing import Any


def contains_phrase(text: str, phrase: str) -> bool:
    """Match phrases on word boundaries so e.g. 'lot' does not match 'pilot'."""
    pattern = r"(?<![a-z0-9])" + re.escape(phrase.casefold()) + r"(?![a-z0-9])"
    return re.search(pattern, text.casefold()) is not None


BOILERPLATE_MARKERS = (
    "return policy", "shipping", "bidding", "payments", "condition disclaimer",
    "mission statement", "pick up", "pickup", "item profile", "by bidding on",
)


def strip_boilerplate(text: str) -> str:
    """Remove seller policy copy before it can contaminate item-level signals."""
    value = str(text or "").strip()
    cutoff = len(value)
    for marker in BOILERPLATE_MARKERS:
        match = re.search(
            rf"(?:^|\n)\s*(?:[-=*#]+\s*)?{re.escape(marker)}\b",
            value,
            flags=re.IGNORECASE,
        )
        if match:
            cutoff = min(cutoff, match.start())
    return value[:cutoff].strip()


def _first_matching_tier(value: float, tiers: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for tier in tiers:
        if key == "maximum" and value <= float(tier[key]):
            return tier
        if key == "minimum" and value >= float(tier[key]):
            return tier
    return None


def extract_weight_lb(text: str) -> float | None:
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

    weight = extract_weight_lb(text)
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
    description = strip_boilerplate(str(listing.get("description") or ""))
    ocr_text = str(listing.get("ocr_text") or "")
    visual_text = str(listing.get("visual_text") or "")
    text = f"{title}\n{description}\n{ocr_text}\n{visual_text}"
    score = int(config.get("base_score", 0))
    reasons: list[str] = []
    matched_terms: list[str] = []
    matched_premium: list[str] = []
    matched_evidence: list[str] = []
    matched_opportunity_rules: list[str] = []
    risk_points = 0
    domain_signal = any(
        contains_phrase(text, phrase) for phrase in config.get("domain_keywords", [])
    )

    def evidence_location(phrase: str) -> str:
        if contains_phrase(title, phrase):
            return "title"
        if contains_phrase(description, phrase):
            return "description"
        if contains_phrase(ocr_text, phrase):
            return "image text"
        if contains_phrase(visual_text, phrase):
            return "image color analysis"
        return "listing"

    groups = (
        ("priority_keywords", "Opportunity term"),
        ("lower_priority_keywords", "Lower-priority term"),
        ("target_keywords", "Target keyword"),
        ("premium_keywords", "High-value signal"),
    )
    group_caps = config.get("keyword_group_caps", {})
    top_match_defaults = {
        "priority_keywords": 3,
        "target_keywords": 3,
        "premium_keywords": 2,
    }
    top_match_limits = {**top_match_defaults, **config.get("keyword_group_top_matches", {})}
    for group_name, label in groups:
        if (
            group_name == "priority_keywords"
            and config.get("require_domain_signal_for_priority_keywords", False)
            and not domain_signal
        ):
            continue
        matches: list[tuple[str, int, str]] = []
        for phrase, raw_points in config.get(group_name, {}).items():
            if not contains_phrase(text, phrase):
                continue
            points = int(raw_points)
            if points < 0:
                risk_points += points
            location = evidence_location(phrase)
            matches.append((phrase, points, location))
            if group_name == "target_keywords":
                matched_terms.append(phrase)
            elif group_name == "premium_keywords":
                matched_premium.append(phrase)

        positives = sorted(
            (match for match in matches if match[1] >= 0),
            key=lambda match: match[1],
            reverse=True,
        )
        negatives = [match for match in matches if match[1] < 0]
        top_limit = top_match_limits.get(group_name)
        selected_positives = positives[: int(top_limit)] if top_limit else positives
        selected = selected_positives + negatives
        group_points = sum(points for _, points, _ in selected)
        for phrase, points, location in selected:
            sign = "+" if points >= 0 else ""
            reasons.append(f"{label} '{phrase}' in {location} ({sign}{points})")
        if top_limit and len(positives) > int(top_limit):
            reasons.append(
                f"Only the top {int(top_limit)} {label.lower()} matches count; "
                f"{len(positives) - int(top_limit)} weaker overlap(s) ignored"
            )

        cap = group_caps.get(group_name)
        if cap is not None and group_points > int(cap):
            reduction = group_points - int(cap)
            group_points = int(cap)
            reasons.append(f"{label} bonuses capped to prevent keyword stacking (-{reduction})")
        score += group_points

    for rule in config.get("title_opportunity_rules", []):
        required = rule.get("requires_any", [])
        context = rule.get("requires_context", [])
        forbidden = rule.get("forbids_any", [])
        if required and not any(contains_phrase(title, str(phrase)) for phrase in required):
            continue
        if context and not any(contains_phrase(title, str(phrase)) for phrase in context):
            continue
        if any(contains_phrase(title, str(phrase)) for phrase in forbidden):
            continue
        points = int(rule.get("points", 0))
        score += points
        label = str(rule.get("label") or "Title-based opportunity")
        matched_opportunity_rules.append(label)
        reasons.append(f"{label} (+{points})")

    for phrase, raw_points in config.get("collector_evidence_keywords", {}).items():
        if not contains_phrase(text, phrase):
            continue
        points = int(raw_points)
        score += points
        matched_evidence.append(phrase)
        location = evidence_location(phrase)
        reasons.append(f"Collector evidence '{phrase}' in {location} (+{points})")

    category = str(listing.get("category") or "")
    for phrase, raw_points in config.get("category_penalties", {}).items():
        if not contains_phrase(category, phrase):
            continue
        points = int(raw_points)
        score += points
        if points < 0:
            risk_points += points
        reasons.append(f"Less relevant category '{phrase}' ({points})")

    trusted_source = False
    seller_id = str(listing.get("seller_id") or "")
    seller_bonus = int(config.get("seller_bonuses", {}).get(seller_id, 0))
    if seller_bonus and domain_signal:
        score += seller_bonus
        trusted_source = True
        reasons.append(
            f"Proven source for visually strong specimen lots (+{seller_bonus})"
        )

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

    bid_penalty = _first_matching_tier(
        bids, config.get("bid_penalties", []), "minimum"
    )
    if bid_penalty:
        points = int(bid_penalty["points"])
        score += points
        if points < 0:
            risk_points += points
        reasons.append(f"{bids} bids show the opportunity is already noticed ({points})")

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
        contains_phrase(text, phrase)
        for phrase in config.get("strong_collection_keywords", [])
    )
    priority_target_keywords = {
        str(phrase).casefold()
        for phrase in config.get("high_priority_target_keywords", [])
    }
    has_priority_target = bool(matched_terms) and (
        not priority_target_keywords
        or bool({phrase.casefold() for phrase in matched_terms} & priority_target_keywords)
    )
    high_priority_eligible = (
        photo_count >= int(config.get("high_priority_minimum_photos", 4))
        and (
            bool(config.get("allow_pickup_high_priority"))
            or not bool((listing.get("shipping") or {}).get("pickup_only"))
        )
        and (
            bool(matched_premium)
            or bool(matched_evidence)
            or has_priority_target
            or strong_collection_language
            or trusted_source
            or bool(matched_opportunity_rules)
            or (favorable_shipping and (extract_weight_lb(text) or 0) >= 4)
        )
        and risk_points >= int(config.get("high_priority_risk_floor", -24))
    )

    result = {
        "score": max(0, min(100, score)),
        "score_reasons": reasons or ["No scoring signals beyond the base score"],
        "matched_keywords": sorted(set(matched_terms)),
        "high_priority_eligible": high_priority_eligible,
    }
    matched_terms_field = str(config.get("matched_terms_field") or "")
    if matched_terms_field:
        result[matched_terms_field] = sorted(set(matched_terms))
    return result
