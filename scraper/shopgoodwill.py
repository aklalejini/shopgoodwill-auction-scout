"""Small replaceable client for ShopGoodwill's publicly used Buyer API."""

from __future__ import annotations

import html
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests


PACIFIC = ZoneInfo("America/Los_Angeles")


class DataSourceError(RuntimeError):
    """Raised when a response cannot safely be interpreted."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def strip_html(value: Any) -> str:
    parser = _TextExtractor()
    parser.feed(html.unescape(str(value or "")))
    return "\n".join(parser.parts)


def normalize_image_url(server: str, path: str) -> str:
    clean_path = str(path or "").replace("\\", "/").lstrip("/")
    if clean_path.startswith(("http://", "https://")):
        return clean_path
    return urljoin(server.rstrip("/") + "/", clean_path)


def split_image_urls(server: str, paths: Any) -> list[str]:
    return [normalize_image_url(server, path) for path in str(paths or "").split(";") if path.strip()]


def parse_api_time(value: Any) -> str | None:
    """The Buyer API returns naive Pacific wall-clock timestamps."""
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PACIFIC)
    return parsed.isoformat()


def parse_search_response(payload: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("searchResults"), dict):
        raise DataSourceError("Search response did not contain searchResults")
    results = payload["searchResults"]
    items = results.get("items")
    if not isinstance(items, list):
        raise DataSourceError("Search response items were missing or malformed")
    valid_items = [item for item in items if isinstance(item, dict) and item.get("itemId")]
    return valid_items, int(results.get("itemCount") or len(valid_items))


class ShopGoodwillClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.base_url = str(config["base_url"]).rstrip("/")
        self.timeout = float(config.get("timeout_seconds", 25))
        self.delay = float(config.get("request_delay_seconds", 0.3))
        self.max_retries = int(config.get("max_retries", 2))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": str(config.get("user_agent")),
            "Accept": "application/json",
        })
        self._last_request_at = 0.0

    def _wait(self) -> None:
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._wait()
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                self._last_request_at = time.monotonic()
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(min(2**attempt, 4))
                        continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))
        raise DataSourceError(f"Request failed after limited retries: {last_error}")

    def search(self, term: str, page: int = 1) -> tuple[list[dict[str, Any]], int]:
        body = {
            "searchText": term,
            "page": page,
            "pageSize": min(int(self.config.get("page_size", 40)), 40),
            "sortColumn": 1,
            "sortDescending": True,
            "categoryId": 0,
            "categoryLevel": 0,
            "lowPrice": 0,
            "highPrice": 999999,
            "closedAuctionDays": 0,
            "searchBuyNowOnly": False,
            "searchPickupOnly": False,
            "searchNoPickupOnly": False,
            "searchOneCentShippingOnly": False,
            "searchDescriptions": bool(self.config.get("search_descriptions", False)),
            "searchClosedAuctions": False,
            "selectedCategoryIds": "",
            "useBuyerPrefs": False,
            "savedSearchId": 0,
            "partNumber": "",
        }
        response = self._request("POST", f"{self.base_url}/Search/ItemListing", json=body)
        try:
            return parse_search_response(response.json())
        except (ValueError, TypeError) as exc:
            raise DataSourceError(f"Search returned invalid JSON: {exc}") from exc

    def detail(self, item_id: str) -> dict[str, Any]:
        response = self._request(
            "GET", f"{self.base_url}/itemDetail/GetItemDetailModelByItemId/{item_id}"
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataSourceError(f"Detail {item_id} returned invalid JSON") from exc
        if not isinstance(payload, dict) or str(payload.get("itemId") or "") != str(item_id):
            raise DataSourceError(f"Detail {item_id} was missing its item ID")
        return payload


def listing_from_search(
    item: dict[str, Any], term: str, timestamp: str, hunt: dict[str, Any]
) -> dict[str, Any]:
    primary = normalize_image_url("", str(item.get("imageURL") or ""))
    item_id = str(item.get("itemId"))
    return {
        "item_id": item_id,
        "title": str(item.get("title") or "Untitled listing"),
        "price": float(item.get("currentPrice") or 0),
        "bids": int(item.get("numBids") or 0),
        "seller": "",
        "seller_id": item.get("sellerId"),
        "start_time": parse_api_time(item.get("startTime")),
        "end_time": parse_api_time(item.get("endTime")),
        "listing_url": f"https://shopgoodwill.com/item/{item_id}",
        "shipping": {
            "listed_price": float(item.get("shippingPrice") or 0),
            "pickup_only": False,
            "carrier": "",
        },
        "category": str(item.get("catFullName") or item.get("categoryName") or ""),
        "images": [primary] if primary else [],
        "thumbnails": [primary] if primary else [],
        "description": str(item.get("description") or ""),
        "discovered_by": [term],
        "hunt_categories": [str(hunt["id"])],
        "hunt_labels": [str(hunt["label"])],
        "detail_status": "pending",
        "last_seen": timestamp,
        "last_updated": timestamp,
    }


def apply_detail(listing: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    server = str(detail.get("imageServer") or "")
    full_images = split_image_urls(server, detail.get("imageUrlString"))
    thumbnails = split_image_urls(server, detail.get("thumbnailUrlString"))
    if full_images:
        listing["images"] = full_images
    if thumbnails:
        listing["thumbnails"] = thumbnails
    listing.update({
        "title": str(detail.get("title") or listing.get("title") or "Untitled listing"),
        "price": float(detail.get("currentPrice") or listing.get("price") or 0),
        "bids": int(detail.get("numberOfBids") or 0),
        "seller": str(detail.get("sellerCompanyName") or ""),
        "seller_id": detail.get("sellerId") or listing.get("seller_id"),
        "start_time": parse_api_time(detail.get("startTime")) or listing.get("start_time"),
        "end_time": parse_api_time(detail.get("endTime")) or listing.get("end_time"),
        "description": strip_html(detail.get("description")),
        "shipping": {
            "listed_price": float(detail.get("shippingPrice") or 0),
            "handling_price": float(detail.get("handlingPrice") or 0),
            "pickup_only": bool(detail.get("pickupOnly") or detail.get("storePickupOnly")),
            "carrier": str(detail.get("sellerShipperName") or ""),
            "policy": strip_html(detail.get("shippingPolicy")),
        },
        "detail_status": "complete",
    })
    return listing
