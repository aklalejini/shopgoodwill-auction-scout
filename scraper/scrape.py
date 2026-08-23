"""Refresh active listings, high-priority listings, and the compact archive."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .ctbids import CTBidsClient, apply_ctbids_detail, ctbids_listing
from .government import (
    GSAAuctionsClient,
    GovDealsClient,
    apply_govdeals_detail,
    apply_gsa_detail,
    govdeals_listing,
    gsa_listing,
)
from .ocr import process_ocr
from .scoring import contains_phrase, score_listing
from .shopgoodwill import DataSourceError, ShopGoodwillClient, apply_detail, listing_from_search
from .valuation import load_outcomes


PROJECT_ROOT = Path(__file__).resolve().parents[1]

PUBLIC_INTERNAL_FIELDS = {
    "valuation_matches", "valuation_reasons", "estimated_resale_low",
    "estimated_resale_high", "estimated_resale", "valuation_confidence",
    "sell_probability", "estimated_days_to_sell", "estimated_shipping",
    "estimated_weight_lb", "estimated_labor_cost", "liquidity",
    "expected_margin", "max_bid", "manual_valuation", "naivete_score",
    "naivete_reasons", "fee_rate", "target_multiple", "clean_description",
    "potentially_undervalued", "financially_actionable", "ocr_text",
    "visual_text", "matched_minerals", "potential_shipping_savings",
    "score_components",
}


def clean_public_record(value: Any) -> None:
    """Recursively remove internal, obsolete, and unverified publication fields."""
    if isinstance(value, dict):
        for field in PUBLIC_INTERNAL_FIELDS:
            value.pop(field, None)
        for child in value.values():
            clean_public_record(child)
    elif isinstance(value, list):
        for child in value:
            clean_public_record(child)


def derive_high_priority(
    active: list[dict[str, Any]], maximum_records: int
) -> list[dict[str, Any]]:
    """Return high-priority records directly from the canonical active objects."""
    return sorted(
        (item for item in active if item.get("high_priority")),
        key=lambda item: -int(item.get("score") or 0),
    )[:maximum_records]


def apply_seller_clusters(items: list[dict[str, Any]], close_window_hours: int = 72) -> None:
    """Annotate same-seller groups whose auctions close within a practical window."""
    by_seller: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        item.pop("seller_cluster", None)
        seller_id = str(item.get("seller_id") or "")
        if seller_id:
            by_seller.setdefault(seller_id, []).append(item)

    for seller_id, seller_items in by_seller.items():
        seller_items.sort(key=lambda item: item.get("end_time") or "")
        clusters: list[list[dict[str, Any]]] = []
        cluster_start: datetime | None = None
        for item in seller_items:
            try:
                end = datetime.fromisoformat(str(item.get("end_time") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if not clusters:
                clusters.append([item])
                cluster_start = end
                continue
            # Compare against the first auction in the group. Comparing against
            # the prior auction lets daily listings chain into one huge cluster.
            if cluster_start and abs((end - cluster_start).total_seconds()) <= close_window_hours * 3600:
                clusters[-1].append(item)
            else:
                clusters.append([item])
                cluster_start = end

        for index, cluster in enumerate(clusters, start=1):
            if len(cluster) < 2:
                continue
            policies = " ".join(
                str((item.get("shipping") or {}).get("policy") or "") for item in cluster
            ).casefold()
            combining_unavailable = any(
                phrase in policies
                for phrase in (
                    "combined shipping temporarily unavailable",
                    "cannot be combined",
                    "combining is unavailable",
                )
            )
            payload = {
                "id": f"{seller_id}:{index}",
                "count": len(cluster),
                "item_ids": [str(item.get("item_id")) for item in cluster],
                "combined_shipping_unavailable": combining_unavailable,
                "close_window_hours": close_window_hours,
            }
            for item in cluster:
                item["seller_cluster"] = payload


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def deduplicate(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        item_id = str(record.get("item_id") or "")
        if not item_id:
            continue
        if item_id not in by_id:
            by_id[item_id] = copy.deepcopy(record)
            for field in ("discovered_by", "hunt_categories", "hunt_labels"):
                by_id[item_id][field] = sorted(set(record.get(field) or []))
            continue
        current = by_id[item_id]
        for field in ("discovered_by", "hunt_categories", "hunt_labels"):
            current[field] = sorted(
                set(current.get(field) or []) | set(record.get(field) or [])
            )
        for key, value in record.items():
            if key not in ("discovered_by", "hunt_categories", "hunt_labels") and value not in (None, "", [], {}):
                current[key] = value
    return list(by_id.values())


def is_expired(record: dict[str, Any], now: datetime) -> bool:
    value = record.get("end_time")
    if not value:
        return False
    try:
        end = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


def merge_search_record(existing: dict[str, Any] | None, fresh: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return fresh
    merged = copy.deepcopy(existing)
    for key in (
        "title", "price", "buy_now_price", "has_buy_now", "bids", "seller_id",
        "start_time", "end_time", "category", "source", "source_label",
        "source_native_id", "source_account_id", "source_sale_id", "listing_url", "location",
        "local_search",
    ):
        if fresh.get(key) not in (None, ""):
            merged[key] = fresh[key]
    for field in ("discovered_by", "hunt_categories", "hunt_labels"):
        merged[field] = sorted(
            set(existing.get(field) or []) | set(fresh.get(field) or [])
        )
    merged["last_seen"] = fresh["last_seen"]
    merged["last_updated"] = fresh["last_updated"]
    return merged


def matches_hunt_domain(item: dict[str, Any], profile: dict[str, Any]) -> bool:
    """Require convincing hunt evidence while rejecting obvious product collisions."""
    if profile.get("accept_all"):
        return True
    relevance = profile.get("relevance", {})
    title = str(item.get("title") or "")
    category = "\n".join(
        str(item.get(field) or "")
        for field in ("category", "catFullName", "categoryName")
    )
    # Search discovery is title-based. Restrict inclusion to the title so an
    # unrelated or boilerplate description cannot rescue a misleading item.
    text = title

    # Product nouns in the title are reliable signals. This prevents Fossil watches,
    # Crystal dolls, mineral-wash clothing, and quartz jewelry from entering the feed.
    required_any = relevance.get("required_any_keywords", [])
    has_required_condition = any(
        contains_phrase(title, str(phrase)) for phrase in required_any
    )
    condition_optional = relevance.get("condition_optional_keywords", [])
    has_condition_optional_model = any(
        contains_phrase(title, str(phrase)) for phrase in condition_optional
    )
    if required_any and not has_required_condition and not has_condition_optional_model:
        return False
    required_signals = relevance.get("required_signal_keywords", [])
    if required_signals and not any(
        contains_phrase(title, str(phrase)) for phrase in required_signals
    ):
        return False
    if any(
        contains_phrase(title, str(phrase))
        for phrase in relevance.get("excluded_product_keywords", [])
    ):
        return False
    if any(
        contains_phrase(category, str(phrase))
        for phrase in relevance.get("strict_excluded_category_keywords", [])
    ):
        return False
    strong_keywords = relevance.get("strong_keywords") or profile.get(
        "domain_keywords", []
    )
    has_strong_title_signal = any(
        contains_phrase(title, str(phrase)) for phrase in strong_keywords
    )
    if any(
        contains_phrase(category, str(phrase))
        for phrase in relevance.get("excluded_category_keywords", [])
    ) and not has_strong_title_signal:
        return False

    if has_strong_title_signal:
        return True

    ambiguous_matches = {
        str(phrase).casefold()
        for phrase in relevance.get("ambiguous_keywords", [])
        if contains_phrase(text, str(phrase))
    }
    has_context = any(
        contains_phrase(text, str(phrase))
        for phrase in relevance.get("supporting_keywords", [])
    )
    minimum_ambiguous = int(relevance.get("minimum_ambiguous_matches", 2))
    return len(ambiguous_matches) >= minimum_ambiguous and has_context


def matches_any_hunt(
    item: dict[str, Any], hunts: list[dict[str, Any]], profiles: dict[str, Any]
) -> bool:
    """Keep an item when it is relevant to at least one hunt that discovered it."""
    item_hunts = set(item.get("hunt_categories") or [])
    candidates = [hunt for hunt in hunts if hunt["id"] in item_hunts]
    if not candidates:
        candidates = hunts[:1]
    return any(
        matches_hunt_domain(item, profiles[str(hunt["scoring_profile"])])
        for hunt in candidates
    )


def apply_hunt_scoring(
    item: dict[str, Any], hunts: list[dict[str, Any]], profiles: dict[str, Any]
) -> None:
    """Score a listing within each hunt that discovered it, keeping the strongest result."""
    hunt_ids = set(item.get("hunt_categories") or [])
    candidates = [hunt for hunt in hunts if hunt["id"] in hunt_ids]
    if not candidates and hunts:
        candidates = [hunts[0]]
        item["hunt_categories"] = [str(hunts[0]["id"])]
        item["hunt_labels"] = [str(hunts[0]["label"])]

    scored: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for hunt in candidates:
        profile = profiles[str(hunt["scoring_profile"])]
        scored.append((hunt, profile, score_listing(item, profile)))
    if not scored:
        raise ValueError("At least one enabled hunt and scoring profile is required")

    best_hunt, best_profile, best_result = max(scored, key=lambda entry: entry[2]["score"])
    item.update(best_result)
    item["primary_hunt"] = {"id": best_hunt["id"], "label": best_hunt["label"]}
    item["hunt_scores"] = [
        {
            "id": hunt["id"],
            "label": hunt["label"],
            "score": result["score"],
            "score_reasons": result["score_reasons"],
            "matched_keywords": result["matched_keywords"],
        }
        for hunt, _, result in scored
    ]
    item["potentially_undervalued"] = int(item["score"]) >= int(
        best_profile.get("undervalued_threshold", 30)
    )
    item["high_priority"] = int(item["score"]) >= int(
        best_profile.get("high_priority_threshold", 35)
    ) and bool(item.get("high_priority_eligible", True))


def refresh(
    config: dict[str, Any],
    data_dir: Path,
    docs_data_dir: Path,
    max_detail_requests: int | None = None,
    client: ShopGoodwillClient | None = None,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    previous = deduplicate(load_json(data_dir / "listings.json", []))
    discovered: list[dict[str, Any]] = []
    failures: list[str] = []
    api_config = config["api"]
    source = client or ShopGoodwillClient(api_config)
    source_config = config.get("sources", {})
    local_config = config.get("local_search", {})
    zip_code = str(local_config.get("zip_code") or "38635")
    radius_miles = int(local_config.get("radius_miles") or 50)
    gsa_client = GSAAuctionsClient(source_config.get("gsa_auctions", {}))
    govdeals_client = GovDealsClient(source_config.get("govdeals", {}))
    ctbids_client = CTBidsClient(source_config.get("ctbids", {}))
    ctbids_expected_scopes: set[str] = set()
    ctbids_successful_scopes: set[str] = set()
    hunts = [hunt for hunt in config.get("hunts", []) if hunt.get("enabled", True)]
    profiles = config.get("scoring_profiles", {})
    outcomes = load_outcomes(PROJECT_ROOT / "data" / "outcomes.csv")
    ocr_cache = load_json(PROJECT_ROOT / "data" / "ocr_cache.json", {})
    if not hunts:
        raise ValueError("No enabled hunts are configured")

    local_hunt = next(
        (hunt for hunt in hunts if hunt["id"] == "local-government-surplus"), None
    )
    estate_hunt = next(
        (hunt for hunt in hunts if hunt["id"] == "local-estate-auctions"), None
    )

    for hunt in hunts:
        for term in hunt.get("search_terms", []):
            for page in range(1, int(api_config.get("pages_per_term", 1)) + 1):
                try:
                    items, _ = source.search(str(term), page)
                except DataSourceError as exc:
                    failures.append(f"{hunt['label']} / {term} (page {page}): {exc}")
                    print(f"warning: {failures[-1]}", file=sys.stderr)
                    break
                for item in items:
                    discovered.append(listing_from_search(item, str(term), timestamp, hunt))
                if len(items) < int(api_config.get("page_size", 40)):
                    break

        profile = profiles[str(hunt["scoring_profile"])]
        for sweep in hunt.get("seller_sweeps", []):
            seller_id = str(sweep["seller_id"])
            source_label = str(sweep.get("label") or f"seller:{seller_id}")
            pages = int(sweep.get("pages", 1))
            for page in range(1, pages + 1):
                try:
                    items, _ = source.search(
                        "", page, sort_descending=False, seller_ids=[seller_id]
                    )
                except DataSourceError as exc:
                    failures.append(
                        f"{hunt['label']} / {source_label} (page {page}): {exc}"
                    )
                    print(f"warning: {failures[-1]}", file=sys.stderr)
                    break
                for item in items:
                    if matches_hunt_domain(item, profile):
                        discovered.append(
                            listing_from_search(item, source_label, timestamp, hunt)
                        )
                if len(items) < int(api_config.get("page_size", 40)):
                    break

    if local_hunt and source_config.get("gsa_auctions", {}).get("enabled", True):
        try:
            items, _ = gsa_client.search(zip_code, radius_miles)
            discovered.extend(
                gsa_listing(item, timestamp, local_hunt, zip_code, radius_miles)
                for item in items
            )
        except DataSourceError as exc:
            failures.append(f"GSA Auctions / {zip_code} + {radius_miles} mi: {exc}")
            print(f"warning: {failures[-1]}", file=sys.stderr)

    if local_hunt and source_config.get("govdeals", {}).get("enabled", True):
        try:
            items, _ = govdeals_client.search(zip_code, radius_miles)
            discovered.extend(
                govdeals_listing(item, timestamp, local_hunt, zip_code, radius_miles)
                for item in items
            )
        except DataSourceError as exc:
            failures.append(f"GovDeals / {zip_code} + {radius_miles} mi: {exc}")
            print(f"warning: {failures[-1]}", file=sys.stderr)

    if estate_hunt and source_config.get("ctbids", {}).get("enabled", True):
        ctbids_expected_scopes.add("nearby")
        try:
            items, _ = ctbids_client.search(zip_code, radius_miles)
            discovered.extend(
                ctbids_listing(item, timestamp, estate_hunt, zip_code, radius_miles)
                for item in items
            )
            ctbids_successful_scopes.add("nearby")
        except DataSourceError as exc:
            failures.append(f"CTBids / {zip_code} + {radius_miles} mi: {exc}")
            print(f"warning: {failures[-1]}", file=sys.stderr)
        if source_config.get("ctbids", {}).get("include_nationwide_shippable", True):
            ctbids_expected_scopes.add("shippable")
            try:
                items, _ = ctbids_client.search(shippable_only=True)
                discovered.extend(
                    ctbids_listing(
                        item, timestamp, estate_hunt, zip_code, radius_miles,
                        scope="shippable",
                    )
                    for item in items
                )
                ctbids_successful_scopes.add("shippable")
            except DataSourceError as exc:
                failures.append(f"CTBids / nationwide shippable: {exc}")
                print(f"warning: {failures[-1]}", file=sys.stderr)

    fresh_records = deduplicate(discovered)
    ctbids_refresh_complete = bool(ctbids_expected_scopes) and (
        ctbids_expected_scopes <= ctbids_successful_scopes
    )
    active_by_id = {
        str(item["item_id"]): copy.deepcopy(item)
        for item in previous
        if not is_expired(item, now)
        and not (
            ctbids_refresh_complete
            and str(item.get("source") or "") == "ctbids"
        )
    }
    for fresh in fresh_records:
        item_id = str(fresh["item_id"])
        active_by_id[item_id] = merge_search_record(active_by_id.get(item_id), fresh)

    active_candidates = [
        item for item in active_by_id.values() if not is_expired(item, now)
    ]
    active = [
        item for item in active_candidates if matches_any_hunt(item, hunts, profiles)
    ]
    relevance_filtered = len(active_candidates) - len(active)
    for item in active:
        apply_hunt_scoring(item, hunts, profiles)

    pending = [
        item for item in active
        if item.get("detail_status") != "complete" or "has_buy_now" not in item
    ]
    pending.sort(key=lambda item: (-int(item.get("score") or 0), item.get("end_time") or ""))
    detail_limit = (
        int(max_detail_requests)
        if max_detail_requests is not None
        else int(api_config.get("max_detail_requests_per_run", 160))
    )
    detailed = 0
    source_detail_counts = {
        "shopgoodwill": 0, "gsa-auctions": 0, "govdeals": 0, "ctbids": 0
    }
    detail_limits = {
        "shopgoodwill": max(0, detail_limit),
        "gsa-auctions": int(source_config.get("gsa_auctions", {}).get("max_detail_requests_per_run", 25)),
        "govdeals": int(source_config.get("govdeals", {}).get("max_detail_requests_per_run", 24)),
        "ctbids": int(source_config.get("ctbids", {}).get("max_detail_requests_per_run", 20)),
    }
    for item in pending:
        source_id = str(item.get("source") or "shopgoodwill")
        if source_detail_counts.get(source_id, 0) >= detail_limits.get(source_id, 0):
            continue
        source_detail_counts[source_id] = source_detail_counts.get(source_id, 0) + 1
        try:
            if source_id == "govdeals":
                apply_govdeals_detail(
                    item,
                    govdeals_client.detail(
                        str(item.get("source_native_id") or ""),
                        str(item.get("source_account_id") or ""),
                    ),
                )
            elif source_id == "gsa-auctions":
                apply_gsa_detail(
                    item,
                    gsa_client.detail(str(item.get("source_native_id") or "")),
                    gsa_client,
                )
            elif source_id == "ctbids":
                apply_ctbids_detail(
                    item,
                    ctbids_client.detail(
                        str(item.get("source_native_id") or ""),
                        str(item.get("source_sale_id") or ""),
                    ),
                )
            else:
                apply_detail(item, source.detail(str(item.get("source_native_id") or item["item_id"])))
            item["last_updated"] = timestamp
            detailed += 1
        except DataSourceError as exc:
            item["detail_status"] = "failed"
            item["detail_error"] = str(exc)
            print(f"warning: detail {item['item_id']}: {exc}", file=sys.stderr)

    local_hunt_ids = {"local-government-surplus", "local-estate-auctions"}
    standard_hunts = [hunt for hunt in hunts if hunt["id"] not in local_hunt_ids]
    for item in active:
        item.setdefault("source", "shopgoodwill")
        item.setdefault("source_label", "ShopGoodwill")
        item.setdefault("source_native_id", str(item.get("item_id") or ""))
        if str(item.get("source") or "shopgoodwill") in {"gsa-auctions", "govdeals"}:
            matched = [
                hunt for hunt in standard_hunts
                if matches_hunt_domain(item, profiles[str(hunt["scoring_profile"])])
            ]
            if matched:
                item["hunt_categories"] = [
                    str(local_hunt["id"]), *[str(hunt["id"]) for hunt in matched]
                ]
                item["hunt_labels"] = [
                    str(local_hunt["label"]), *[str(hunt["label"]) for hunt in matched]
                ]
        elif str(item.get("source") or "shopgoodwill") == "ctbids":
            matched = [
                hunt for hunt in standard_hunts
                if matches_hunt_domain(item, profiles[str(hunt["scoring_profile"])])
            ]
            if matched and estate_hunt:
                item["hunt_categories"] = [
                    str(estate_hunt["id"]), *[str(hunt["id"]) for hunt in matched]
                ]
                item["hunt_labels"] = [
                    str(estate_hunt["label"]), *[str(hunt["label"]) for hunt in matched]
                ]
        apply_hunt_scoring(item, hunts, profiles)
        item.pop("detail_error", None) if item.get("detail_status") == "complete" else None

    ocr_stats = process_ocr(active, ocr_cache, config.get("ocr", {}))
    economics = config.get("economics", {})
    for item in active:
        # OCR may expose identifiers that were absent from listing copy.
        apply_hunt_scoring(item, hunts, profiles)
        score_high_priority = bool(item.get("high_priority"))
        item["score_high_priority"] = score_high_priority
        item["potentially_high_value"] = score_high_priority
        item["high_priority"] = score_high_priority
        item.pop("potentially_undervalued", None)
        item.pop("financially_actionable", None)
        outcome = outcomes.get(str(item.get("item_id") or ""))
        if outcome:
            item["outcome"] = outcome
        else:
            item.pop("outcome", None)
        clean_public_record(item)

    apply_seller_clusters(active, int(economics.get("cluster_close_window_hours", 72)))
    active.sort(
        key=lambda item: (
            -int(item.get("score") or 0),
            item.get("end_time") or "",
        )
    )

    expired = [item for item in previous if is_expired(item, now)]
    archive = deduplicate(load_json(data_dir / "archive.json", []) + [
        {
            **item,
            "archived_at": timestamp,
            "final_observed_price": item.get("price"),
        }
        for item in expired
    ])
    for item in archive:
        clean_public_record(item)
    archive.sort(key=lambda item: item.get("archived_at") or "", reverse=True)
    archive = archive[: int(config.get("archive", {}).get("maximum_records", 1000))]

    high_priority = derive_high_priority(
        active, int(config.get("high_priority", {}).get("maximum_records", 100))
    )

    for target in (data_dir, docs_data_dir):
        write_json_atomic(target / "listings.json", active)
        write_json_atomic(target / "high_priority.json", high_priority)
        write_json_atomic(target / "archive.json", archive)
        write_json_atomic(target / "status.json", {
            "generated_at": timestamp,
            "active_count": len(active),
            "high_priority_count": len(high_priority),
            "archived_count": len(archive),
            "search_failures": failures,
            "detail_requests_completed": detailed,
            "relevance_filtered_count": relevance_filtered,
            "potentially_high_value_count": len(high_priority),
            "ocr": ocr_stats,
            "data_source": "ShopGoodwill plus official GSA, GovDeals, and CTBids public listings",
            "local_search": {"zip_code": zip_code, "radius_miles": radius_miles},
            "sources": [
                {
                    "id": source_id,
                    "label": {"shopgoodwill": "ShopGoodwill", "gsa-auctions": "GSA Auctions", "govdeals": "GovDeals", "ctbids": "CTBids"}[source_id],
                    "active_count": sum(1 for item in active if str(item.get("source") or "shopgoodwill") == source_id),
                    "detail_requests_attempted": source_detail_counts.get(source_id, 0),
                }
                for source_id in ("shopgoodwill", "gsa-auctions", "govdeals", "ctbids")
            ],
            "hunts": [
                {
                    "id": hunt["id"],
                    "label": hunt["label"],
                    "active_count": sum(
                        1 for item in active if hunt["id"] in item.get("hunt_categories", [])
                    ),
                }
                for hunt in hunts
            ],
        })
    write_json_atomic(PROJECT_ROOT / "data" / "ocr_cache.json", ocr_cache)

    return {
        "active": len(active),
        "high_priority": len(high_priority),
        "archived": len(archive),
        "detail_requests": detailed,
        "relevance_filtered": relevance_filtered,
        "search_failures": len(failures),
        "ocr_listings": int(ocr_stats.get("listings_processed") or 0),
        "high_potential": len(high_priority),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "scraper" / "config.json")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--docs-data-dir", type=Path, default=PROJECT_ROOT / "docs" / "data")
    parser.add_argument("--max-detail-requests", type=int, default=None)
    args = parser.parse_args()
    config = load_json(args.config, {})
    if not config:
        parser.error(f"Could not load config: {args.config}")
    stats = refresh(config, args.data_dir, args.docs_data_dir, args.max_detail_requests)
    print(json.dumps(stats, indent=2))
    return 1 if stats["search_failures"] and stats["active"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
