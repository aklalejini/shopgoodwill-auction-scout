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

from .scoring import score_listing
from .shopgoodwill import DataSourceError, ShopGoodwillClient, apply_detail, listing_from_search


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    for key in ("title", "price", "bids", "seller_id", "start_time", "end_time", "category"):
        if fresh.get(key) not in (None, ""):
            merged[key] = fresh[key]
    for field in ("discovered_by", "hunt_categories", "hunt_labels"):
        merged[field] = sorted(
            set(existing.get(field) or []) | set(fresh.get(field) or [])
        )
    merged["last_seen"] = fresh["last_seen"]
    merged["last_updated"] = fresh["last_updated"]
    return merged


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
    hunts = [hunt for hunt in config.get("hunts", []) if hunt.get("enabled", True)]
    profiles = config.get("scoring_profiles", {})
    if not hunts:
        raise ValueError("No enabled hunts are configured")

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

    fresh_records = deduplicate(discovered)
    active_by_id = {
        str(item["item_id"]): copy.deepcopy(item)
        for item in previous
        if not is_expired(item, now)
    }
    for fresh in fresh_records:
        item_id = str(fresh["item_id"])
        active_by_id[item_id] = merge_search_record(active_by_id.get(item_id), fresh)

    active = [item for item in active_by_id.values() if not is_expired(item, now)]
    for item in active:
        apply_hunt_scoring(item, hunts, profiles)

    pending = [item for item in active if item.get("detail_status") != "complete"]
    pending.sort(key=lambda item: (-int(item.get("score") or 0), item.get("end_time") or ""))
    detail_limit = (
        int(max_detail_requests)
        if max_detail_requests is not None
        else int(api_config.get("max_detail_requests_per_run", 160))
    )
    detailed = 0
    for item in pending[: max(0, detail_limit)]:
        try:
            apply_detail(item, source.detail(str(item["item_id"])))
            item["last_updated"] = timestamp
            detailed += 1
        except DataSourceError as exc:
            item["detail_status"] = "failed"
            item["detail_error"] = str(exc)
            print(f"warning: detail {item['item_id']}: {exc}", file=sys.stderr)

    for item in active:
        apply_hunt_scoring(item, hunts, profiles)
        item.pop("detail_error", None) if item.get("detail_status") == "complete" else None
    active.sort(key=lambda item: (-int(item.get("score") or 0), item.get("end_time") or ""))

    expired = [item for item in previous if is_expired(item, now)]
    archive = deduplicate(load_json(data_dir / "archive.json", []) + [
        {
            **item,
            "archived_at": timestamp,
            "final_observed_price": item.get("price"),
        }
        for item in expired
    ])
    archive.sort(key=lambda item: item.get("archived_at") or "", reverse=True)
    archive = archive[: int(config.get("archive", {}).get("maximum_records", 1000))]

    high_priority = [item for item in active if item.get("high_priority")]
    high_priority = high_priority[: int(config.get("high_priority", {}).get("maximum_records", 100))]

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
            "data_source": "ShopGoodwill public Buyer API used by the storefront",
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

    return {
        "active": len(active),
        "high_priority": len(high_priority),
        "archived": len(archive),
        "detail_requests": detailed,
        "search_failures": len(failures),
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
