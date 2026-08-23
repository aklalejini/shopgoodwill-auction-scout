"""Dollar-denominated resale, shipping, margin, and max-bid estimates."""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Any

from .scoring import contains_phrase, extract_weight_lb, strip_boilerplate


DEFAULTS = {
    "minerals-geology": (18.0, 85.0, "medium", 0.35, 75, 14.0, 8.0),
    "sealed-vintage-media": (8.0, 35.0, "high", 0.45, 35, 6.0, 3.0),
    "vintage-electron-tubes": (15.0, 80.0, "medium", 0.35, 70, 10.0, 8.0),
    "vintage-pens": (15.0, 65.0, "medium", 0.38, 65, 8.0, 2.0),
    "estate-tobacco-pipes": (15.0, 70.0, "medium", 0.38, 55, 14.0, 3.0),
    "local-government-surplus": (0.0, 0.0, "none", 0.05, 365, 0.0, 0.0),
}

LIQUIDITY_PROBABILITY = {"high": 0.88, "medium": 0.68, "low": 0.48, "none": 0.18}
GENERIC_TITLE_PHRASES = (
    "box of", "assorted", "misc", "miscellaneous", "bulbs", "old", "bag of",
    "grab bag", "unknown", "as found", "estate lot", "mixed lot", "collection",
)
INFORMED_TITLE_PHRASES = (
    "nos", "tested", "rare", "collector", "collectible", "htf", "hard to find",
    "professionally restored", "authenticated", "patent era", "date code",
)


def load_valuation_rules(path: Path) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                keyword = str(row.get("keyword") or "").strip()
                hunt = str(row.get("hunt") or "").strip()
                if not keyword or not hunt:
                    continue
                try:
                    rules.append({
                        "keyword": keyword,
                        "hunt": hunt,
                        "est_low": float(row.get("est_low") or 0),
                        "est_high": float(row.get("est_high") or 0),
                        "liquidity": str(row.get("liquidity") or "medium").strip().lower(),
                        "confidence": max(0.0, min(1.0, float(row.get("confidence") or 0.5))),
                        "days_to_sell": max(1, int(float(row.get("days_to_sell") or 60))),
                        "notes": str(row.get("notes") or "").strip(),
                    })
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    return rules


def load_outcomes(path: Path) -> dict[str, dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {
                str(row.get("item_id") or "").strip(): dict(row)
                for row in csv.DictReader(handle)
                if str(row.get("item_id") or "").strip()
            }
    except OSError:
        return {}


def _lot_count(text: str) -> int:
    patterns = (
        r"\blot of\s+(\d{1,3})\b",
        r"\b(\d{1,3})\s*(?:pc|pcs|piece|pieces|tube|tubes|pen|pens|pipe|pipes|tape|tapes|reel|reels)\b",
    )
    values = [int(match) for pattern in patterns for match in re.findall(pattern, text, re.I)]
    return max(values, default=1)


def _shipping_estimate(listing: dict[str, Any], hunt_id: str, raw_text: str) -> tuple[float, float | None, str]:
    shipping = listing.get("shipping") or {}
    handling = float(shipping.get("handling_price") or 0)
    if shipping.get("pickup_only"):
        return 0.0, extract_weight_lb(raw_text), "Pickup only; shipping estimate unavailable"
    listed = float(shipping.get("listed_price") or 0)
    if listed > 0:
        return round(listed + handling, 2), extract_weight_lb(raw_text), "Seller-provided shipping plus handling"

    default_weight = DEFAULTS.get(hunt_id, DEFAULTS["minerals-geology"])[6]
    weight = extract_weight_lb(raw_text) or default_weight
    # Conservative ground estimate to north Mississippi. It intentionally models
    # the nonlinear cost of heavy parcels without pretending to be a carrier quote.
    ground = 9.5 + 2.4 * weight + 0.03 * weight * weight
    return round(ground + handling, 2), round(weight, 2), "Calculated ground estimate from weight/category"


def _naivete(title: str, description: str, photo_count: int, title_matches: list[dict[str, Any]]) -> tuple[int, list[str]]:
    points = 35
    reasons: list[str] = []
    generic = [phrase for phrase in GENERIC_TITLE_PHRASES if contains_phrase(title, phrase)]
    if generic:
        bonus = min(24, 8 * len(generic))
        points += bonus
        reasons.append(f"Generic seller wording ({', '.join(generic[:3])}) (+{bonus})")
    if not title_matches:
        points += 16
        reasons.append("No valuation identifier in title (+16)")
    else:
        penalty = min(24, 8 + 4 * len(title_matches))
        points -= penalty
        reasons.append(f"Seller named {len(title_matches)} value identifier(s) (-{penalty})")
    if not re.search(r"\b\d{1,3}[a-z]{1,4}\d{0,3}\b", title, re.I):
        points += 6
        reasons.append("No obvious model/type number in title (+6)")
    informed = [phrase for phrase in INFORMED_TITLE_PHRASES if contains_phrase(title, phrase)]
    if informed:
        penalty = min(30, 10 * len(informed))
        points -= penalty
        reasons.append(f"Informed-market wording ({', '.join(informed[:3])}) (-{penalty})")
    if len(title.split()) <= 8:
        points += 5
        reasons.append("Short title (+5)")
    if len(description) < 180:
        points += 8
        reasons.append("Short item description (+8)")
    if photo_count >= 8 and len(title.split()) <= 10:
        points += 15
        reasons.append("Many photos with a short title (+15)")
    return max(0, min(100, points)), reasons


def estimate_listing(
    listing: dict[str, Any],
    rules: list[dict[str, Any]],
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    economics = economics or {}
    hunt_id = str((listing.get("primary_hunt") or {}).get("id") or "minerals-geology")
    title = str(listing.get("title") or "")
    raw_description = str(listing.get("description") or "")
    description = strip_boilerplate(raw_description)
    ocr_text = str(listing.get("ocr_text") or "")
    text = f"{title}\n{description}\n{ocr_text}"
    hunt_rules = [rule for rule in rules if rule["hunt"] == hunt_id]
    matches: list[dict[str, Any]] = []
    for rule in hunt_rules:
        if not contains_phrase(text, rule["keyword"]):
            continue
        matched = dict(rule)
        if contains_phrase(title, rule["keyword"]):
            matched["source"] = "title"
            matched["source_factor"] = 1.0
        elif contains_phrase(ocr_text, rule["keyword"]):
            matched["source"] = "image OCR"
            matched["source_factor"] = 0.85
        else:
            matched["source"] = "description"
            matched["source_factor"] = 0.55
        matches.append(matched)
    # A specific matched phrase supersedes its generic substring (for example,
    # "rough tourmaline" should not also receive full "tourmaline" value).
    matches = [
        rule for rule in matches
        if not any(
            rule["keyword"].casefold() != other["keyword"].casefold()
            and rule["keyword"].casefold() in other["keyword"].casefold()
            for other in matches
        )
    ]
    title_matches = [rule for rule in hunt_rules if contains_phrase(title, rule["keyword"])]
    matches.sort(
        key=lambda rule: ((rule["est_low"] + rule["est_high"]) / 2) * rule["confidence"],
        reverse=True,
    )

    default_low, default_high, default_liquidity, default_confidence, default_days, labor_cost, _ = DEFAULTS.get(
        hunt_id, DEFAULTS["minerals-geology"]
    )
    if matches:
        weights = (1.0, 0.45, 0.25)
        selected = matches[: len(weights)]
        est_low = sum(rule["est_low"] * rule["confidence"] * rule["source_factor"] * weights[index] for index, rule in enumerate(selected))
        est_high = sum(rule["est_high"] * rule["confidence"] * rule["source_factor"] * weights[index] for index, rule in enumerate(selected))
        confidence = max(rule["confidence"] * rule["source_factor"] for rule in selected)
        liquidity = min(
            selected,
            key=lambda rule: LIQUIDITY_PROBABILITY.get(rule["liquidity"], 0.5),
        )["liquidity"]
        days_to_sell = round(sum(rule["days_to_sell"] for rule in selected) / len(selected))
    else:
        score_factor = 0.82 + min(100, float(listing.get("score") or 0)) / 300
        est_low, est_high = default_low * score_factor, default_high * score_factor
        confidence, liquidity, days_to_sell = default_confidence, default_liquidity, default_days
        selected = []

    count = _lot_count(text)
    has_direct_identifier = any(rule.get("source") in ("title", "image OCR") for rule in selected)
    lot_multiplier = (
        min(1.8, 1 + 0.2 * math.log2(max(1, count)))
        if not selected or has_direct_identifier
        else 1.0
    )
    est_low *= lot_multiplier
    est_high *= lot_multiplier
    est_low = round(max(0, est_low), 2)
    est_high = round(max(est_low, est_high), 2)
    resale_midpoint = round((est_low + est_high) / 2, 2)
    velocity_factor = max(0.58, 1 - min(days_to_sell, 365) / 850)
    # Confidence already discounts the resale range above. Applying it again to
    # sell probability would double-penalize uncertain identifications.
    sell_probability = round(
        max(0.08, min(0.95, LIQUIDITY_PROBABILITY.get(liquidity, 0.55) * velocity_factor)),
        3,
    )
    estimated_shipping, parsed_weight, shipping_basis = _shipping_estimate(listing, hunt_id, raw_description)
    fee_rate = float(economics.get("fee_rate", 0.15))
    target_multiple = float(economics.get("target_multiple", 2.5))
    current_price = float(listing.get("price") or 0)
    expected_margin = (
        resale_midpoint * sell_probability
        - current_price
        - estimated_shipping
        - resale_midpoint * fee_rate
        - labor_cost
    )
    max_bid = (
        resale_midpoint * sell_probability * (1 - fee_rate) / target_multiple
        - estimated_shipping
        - labor_cost
    )
    naivete_score, naivete_reasons = _naivete(title, description, len(listing.get("images") or []), title_matches)
    valuation_reasons = [
        f"{rule['keyword']} from {rule['source']}: ${rule['est_low']:g}–${rule['est_high']:g}, {rule['liquidity']} liquidity"
        for rule in selected
    ] or [f"Category fallback used at {confidence:.0%} confidence"]
    valuation_reasons.append(shipping_basis)

    return {
        "clean_description": description,
        "valuation_matches": [rule["keyword"] for rule in selected],
        "valuation_reasons": valuation_reasons,
        "estimated_resale_low": est_low,
        "estimated_resale_high": est_high,
        "estimated_resale": resale_midpoint,
        "valuation_confidence": round(confidence, 3),
        "liquidity": liquidity,
        "estimated_days_to_sell": days_to_sell,
        "sell_probability": sell_probability,
        "estimated_shipping": estimated_shipping,
        "estimated_weight_lb": parsed_weight,
        "estimated_labor_cost": labor_cost,
        "fee_rate": fee_rate,
        "target_multiple": target_multiple,
        "expected_margin": round(expected_margin, 2),
        "max_bid": round(max(0, max_bid), 2),
        "naivete_score": naivete_score,
        "naivete_reasons": naivete_reasons,
    }
