"""Public-source adapters for nearby GSA Auctions and GovDeals inventory."""

from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from .shopgoodwill import DataSourceError, strip_html


GSA_AUCTION_API = "https://www.ppms.gov/gw/auction/ppms/api/v1"
GSA_SALES_API = "https://www.ppms.gov/gw/sales/ppms/api/v1"
GSA_STORAGE_API = "https://www.ppms.gov/gw/common/ppms/api/v1"
GOVDEALS_SEO = "https://prod-seo.govdeals.com"


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _first_int(value: Any) -> int:
    match = re.search(r"\d[\d,]*", str(value or ""))
    return _int(match.group(0)) if match else 0


def _utc_time(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip().strip("()")
    raw = re.sub(r"\s+UTC$", "", raw, flags=re.I)
    for pattern in ("%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def _clean_image_url(value: str) -> str:
    """Keep stable source URL and cache buster while removing resize variants."""
    raw = html.unescape(str(value or ""))
    if not raw.startswith("http"):
        return ""
    parts = urlsplit(raw)
    query = [(key, val) for key, val in parse_qsl(parts.query) if key == "cb"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\ufffd", "–").strip()


class _PublicClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.timeout = float(config.get("timeout_seconds", 35))
        self.delay = float(config.get("request_delay_seconds", 0.35))
        self.max_retries = int(config.get("max_retries", 2))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": str(config.get("user_agent") or "AuctionScout/1.0"),
            "Accept": "application/json, text/plain, */*",
        })
        self._last_request_at = 0.0

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            remaining = self.delay - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                self._last_request_at = time.monotonic()
                if (response.status_code == 429 or response.status_code >= 500) and attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))
        raise DataSourceError(f"Request failed after limited retries: {last_error}")


class GSAAuctionsClient(_PublicClient):
    source_id = "gsa-auctions"
    source_label = "GSA Auctions"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.session.headers.update({
            "Origin": "https://gsaauctions.gov",
            "Referer": "https://gsaauctions.gov/",
            "Content-Type": "application/json",
        })

    def search(self, zip_code: str, radius_miles: int) -> tuple[list[dict[str, Any]], int]:
        size = min(100, max(1, int(self.config.get("page_size", 50))))
        page = 1
        found: list[dict[str, Any]] = []
        total = 0
        while page <= int(self.config.get("maximum_pages", 5)):
            sort = "AUCTION_END_DATE_TIME_ASC,ASC"
            body = {
                "categoryCodeList": [], "unCheckedCategoryList": [],
                "auctionSearchTypeAdvanced": "ALL_WORDS", "advancedSearchText": "",
                "zipCode": zip_code, "radius": str(radius_miles), "auctionType": "",
                "minPrice": "", "maxPrice": "", "saleNumber": "", "bidDeposit": None,
                "states": [], "auctionEndDateFrom": "", "auctionEndDateTo": "",
                "auctionStatus": "active", "params": {"page": page, "size": size, "sort": sort},
            }
            response = self._request(
                "POST", f"{GSA_AUCTION_API}/auctions",
                params={"page": page, "size": size, "sort": sort}, json=body,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise DataSourceError("GSA search returned invalid JSON") from exc
            items = payload.get("auctionDTOList") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise DataSourceError("GSA search response was missing auctionDTOList")
            total = _int(payload.get("totalElements"))
            found.extend(item for item in items if isinstance(item, dict) and item.get("auctionId"))
            if not items or page >= _int(payload.get("totalPages")):
                break
            page += 1
        return found, total

    def detail(self, auction_id: str) -> dict[str, Any]:
        auction_response = self._request("GET", f"{GSA_AUCTION_API}/auctions/getAuction/{auction_id}")
        try:
            auction = auction_response.json()
        except ValueError as exc:
            raise DataSourceError(f"GSA detail {auction_id} returned invalid JSON") from exc
        lot_id = auction.get("lotId") or (auction.get("auctionDetails") or {}).get("lotId")
        if not lot_id:
            raise DataSourceError(f"GSA detail {auction_id} did not include a lot ID")
        sales_response = self._request("GET", f"{GSA_SALES_API}/sales/preview/auctions/{lot_id}")
        try:
            sales = sales_response.json()
        except ValueError as exc:
            raise DataSourceError(f"GSA sales detail {lot_id} returned invalid JSON") from exc
        return {"auction": auction, "sales": sales}

    def resolve_images(self, auction_id: str, sales: dict[str, Any]) -> list[str]:
        entries = ((sales.get("imagesAndDocs") or {}).get("image") or [])
        request_body = [
            {"id": str(auction_id), "uri": str(entry.get("uri") or ""),
             "fileName": str(entry.get("name") or entry.get("fileName") or "image")}
            for entry in entries if isinstance(entry, dict) and entry.get("uri")
        ]
        if not request_body:
            return []
        try:
            payload = self._request(
                "POST", f"{GSA_STORAGE_API}/storage/presigned-urls", json=request_body
            ).json()
        except (DataSourceError, ValueError):
            return []
        values = payload if isinstance(payload, list) else payload.get("presignedUrlDTOList", [])
        return [str(entry.get("presignedUrl")) for entry in values if isinstance(entry, dict) and entry.get("presignedUrl")]


def gsa_listing(item: dict[str, Any], timestamp: str, hunt: dict[str, Any], zip_code: str, radius: int) -> dict[str, Any]:
    auction_id = str(item.get("auctionId"))
    location = item.get("location") or {}
    return {
        "item_id": f"gsa-{auction_id}", "source": "gsa-auctions", "source_label": "GSA Auctions",
        "source_native_id": auction_id, "title": str(item.get("lotName") or "Untitled GSA lot"),
        "price": _float(item.get("currentBid") or item.get("minBid")), "buy_now_price": 0.0,
        "has_buy_now": str(item.get("saleMethod") or "").casefold() == "buy now",
        "bids": _int(item.get("numberOfBidders")), "seller": "U.S. General Services Administration",
        "seller_id": str(item.get("salesNumber") or "gsa"), "start_time": _utc_time(item.get("startDate")),
        "end_time": _utc_time(item.get("endDate")),
        "listing_url": f"https://gsaauctions.gov/auctions/preview/{auction_id}",
        "shipping": {"listed_price": 0.0, "pickup_only": True, "carrier": "Local pickup"},
        "category": str(item.get("categoryCode") or "Government surplus"), "images": [], "thumbnails": [],
        "description": "", "location": ", ".join(filter(None, [str(location.get("city") or ""), str(location.get("state") or ""), str(location.get("zipCode") or "")])),
        "local_search": {"zip_code": zip_code, "radius_miles": radius},
        "discovered_by": [f"GSA within {radius} mi of {zip_code}"],
        "hunt_categories": [str(hunt["id"])], "hunt_labels": [str(hunt["label"])],
        "detail_status": "pending", "last_seen": timestamp, "last_updated": timestamp,
    }


def apply_gsa_detail(listing: dict[str, Any], detail: dict[str, Any], client: GSAAuctionsClient) -> dict[str, Any]:
    auction = detail.get("auction") or {}
    sales = detail.get("sales") or {}
    desc = sales.get("auctionDescriptionDTO") or {}
    bid = sales.get("biddingDetailsDTO") or {}
    images = client.resolve_images(str(listing.get("source_native_id") or ""), sales)
    if images:
        listing["images"] = images
        listing["thumbnails"] = images
    listing.update({
        "title": str(desc.get("propertyName") or desc.get("lotName") or listing.get("title")),
        "description": strip_html(desc.get("itemDescription") or sales.get("salesDescription")),
        "price": _float(auction.get("currentBid") or bid.get("currentBid") or listing.get("price")),
        "bids": _int(auction.get("numberOfBidders") or bid.get("numberOfBidders")),
        "detail_status": "complete",
    })
    return listing


class GovDealsClient(_PublicClient):
    source_id = "govdeals"
    source_label = "GovDeals"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.session.headers.update({"Accept": "text/html", "User-Agent": "AuctionScout/1.0 (compatible; public auction research)"})

    def search(self, zip_code: str, radius_miles: int) -> tuple[list[dict[str, Any]], int]:
        response = self._request(
            "GET", f"{GOVDEALS_SEO}/en/search/filters",
            params={"zipcode": zip_code, "miles": radius_miles, "source": "location-search",
                    "ps": int(self.config.get("page_size", 120)), "sf": "auctionclose", "so": "asc"},
        )
        return parse_govdeals_search(response.text)

    def detail(self, asset_id: str, account_id: str) -> dict[str, Any]:
        response = self._request("GET", f"{GOVDEALS_SEO}/en/asset/{asset_id}/{account_id}")
        return parse_govdeals_detail(response.text, asset_id, account_id)


def parse_govdeals_search(document: str) -> tuple[list[dict[str, Any]], int]:
    soup = BeautifulSoup(document, "html.parser")
    cards: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for node in soup.select("div[id^='asset-']"):
        match = re.fullmatch(r"asset-(\d+)-(\d+)", str(node.get("id") or ""))
        if not match:
            continue
        account_id, asset_id = match.groups()
        if (asset_id, account_id) in seen:
            continue
        seen.add((asset_id, account_id))
        title_link = node.select_one("a.link-click")
        price_node = node.select_one("[name='pAssetCurrentBid']")
        location_node = node.select_one("[name='pAssetLocation']")
        timer = node.select_one("app-ux-timer")
        title = _clean_text((title_link or {}).get("title") or (title_link.get_text(" ", strip=True) if title_link else "Untitled GovDeals lot"))
        price = _float((price_node or {}).get("title") or (price_node.get_text(" ", strip=True) if price_node else 0))
        timer_text = timer.get_text(" ", strip=True) if timer else ""
        end_match = re.search(r"\(([^()]+(?:UTC|GMT))\)", timer_text, re.I)
        cards.append({
            "asset_id": asset_id, "account_id": account_id, "title": title, "price": price,
            "location": _clean_text((location_node or {}).get("title") or (location_node.get_text(" ", strip=True) if location_node else "")),
            "end_time": _utc_time(end_match.group(1)) if end_match else None,
        })
    if not cards and "No Results Found" not in soup.get_text(" ", strip=True):
        raise DataSourceError("GovDeals search page contained no recognizable listing cards")
    summary = soup.find(string=re.compile(r"\d+\s+Results", re.I))
    match = re.search(r"(\d[\d,]*)\s+Results", str(summary or ""), re.I)
    return cards, _int(match.group(1)) if match else len(cards)


def govdeals_listing(item: dict[str, Any], timestamp: str, hunt: dict[str, Any], zip_code: str, radius: int) -> dict[str, Any]:
    asset_id, account_id = str(item["asset_id"]), str(item["account_id"])
    return {
        "item_id": f"govdeals-{account_id}-{asset_id}", "source": "govdeals", "source_label": "GovDeals",
        "source_native_id": asset_id, "source_account_id": account_id, "title": str(item.get("title") or "Untitled GovDeals lot"),
        "price": _float(item.get("price")), "buy_now_price": 0.0, "has_buy_now": False, "bids": 0,
        "seller": "", "seller_id": account_id, "start_time": None, "end_time": item.get("end_time"),
        "listing_url": f"https://www.govdeals.com/en/asset/{asset_id}/{account_id}",
        "shipping": {"listed_price": 0.0, "pickup_only": True, "carrier": "Local pickup"},
        "category": "Government surplus", "images": [], "thumbnails": [], "description": "",
        "location": str(item.get("location") or ""), "local_search": {"zip_code": zip_code, "radius_miles": radius},
        "discovered_by": [f"GovDeals within {radius} mi of {zip_code}"],
        "hunt_categories": [str(hunt["id"])], "hunt_labels": [str(hunt["label"])],
        "detail_status": "pending", "last_seen": timestamp, "last_updated": timestamp,
    }


def _section_value(section: Tag | None, label: str) -> str:
    if not section:
        return ""
    for heading in section.find_all(["h5", "dt", "th", "td"]):
        if heading.get_text(" ", strip=True).rstrip(":").casefold() != label.casefold():
            continue
        row = heading.find_parent(class_=re.compile(r"row|description-body"))
        if row:
            columns = row.find_all(class_=re.compile(r"col-6"), recursive=False)
            if len(columns) >= 2:
                first_value = columns[1].find(["p", "a"])
                return _clean_text(first_value.get_text(" ", strip=True) if first_value else columns[1].get_text(" ", strip=True))
        sibling = heading.find_next_sibling()
        return sibling.get_text(" ", strip=True) if sibling else ""
    return ""


def parse_govdeals_detail(document: str, asset_id: str, account_id: str) -> dict[str, Any]:
    soup = BeautifulSoup(document, "html.parser")
    title_node = soup.select_one("h1.product-title")
    if not title_node:
        raise DataSourceError(f"GovDeals detail {asset_id}/{account_id} was missing its title")
    current = soup.select_one("#currentBid")
    bid_count = soup.select_one("#bid_count_link")
    timer = soup.select_one("#onlineAuctionBidBox app-ux-timer")
    timer_text = timer.get_text(" ", strip=True) if timer else ""
    end_match = re.search(r"\(([^()]+(?:UTC|GMT))\)", timer_text, re.I)
    description_node = soup.select_one(".description-table .long-description")
    seller_section = soup.select_one("#seller_information")
    bid_box = soup.select_one("#onlineAuctionBidBox")
    bid_box_text = bid_box.get_text(" ", strip=True) if bid_box else ""
    has_buy_now = bool(re.search(r"\bBuy\s*(?:It\s*)?Now\b", bid_box_text, re.I))
    price = _float((current or {}).get("title") or (current.get_text(" ", strip=True) if current else 0))
    image_values: set[str] = set()
    for node in soup.select("meta[property='og:image'], img[src*='/assets/photos/'], link[href*='/assets/photos/']"):
        value = node.get("content") or node.get("src") or node.get("href") or ""
        clean = _clean_image_url(str(value))
        if clean:
            image_values.add(clean)
    description = description_node.get_text("\n", strip=True) if description_node else ""
    delivery_text = " ".join(
        node.parent.get_text(" ", strip=True)
        for node in soup.find_all(string=re.compile(r"Shipping Available|pickup only|buyer arranged shipping", re.I))
        if isinstance(node.parent, Tag)
    )
    shipping_available = bool(re.search(r"Shipping Available", delivery_text, re.I)) and not bool(re.search(r"not allowed|pickup only", delivery_text, re.I))
    attributes = {
        row.select_one(".td-att-label").get_text(" ", strip=True): row.select_one(".td-att-value").get_text(" ", strip=True)
        for row in soup.select("table[id^='table-id-'] tr")
        if row.select_one(".td-att-label") and row.select_one(".td-att-value")
    }
    return {
        "title": _clean_text(title_node.get_text(" ", strip=True)), "price": price,
        "buy_now_price": price if has_buy_now else 0.0, "has_buy_now": has_buy_now,
        "bids": _first_int((bid_count or {}).get("title")), "end_time": _utc_time(end_match.group(1)) if end_match else None,
        "seller": _section_value(seller_section, "Seller"), "item_location": _section_value(seller_section, "Item Location"),
        "description": _clean_text(description), "images": sorted(image_values), "attributes": attributes,
        "shipping": {"listed_price": 0.0, "pickup_only": not shipping_available,
                     "carrier": "See listing" if shipping_available else "Local pickup", "policy": delivery_text},
    }


def apply_govdeals_detail(listing: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    images = list(detail.get("images") or [])
    listing.update({
        "title": str(detail.get("title") or listing.get("title")), "price": _float(detail.get("price") or listing.get("price")),
        "buy_now_price": _float(detail.get("buy_now_price")), "has_buy_now": bool(detail.get("has_buy_now")),
        "bids": _int(detail.get("bids")), "end_time": detail.get("end_time") or listing.get("end_time"),
        "seller": str(detail.get("seller") or listing.get("seller") or ""),
        "description": str(detail.get("description") or ""), "images": images or listing.get("images") or [],
        "thumbnails": images or listing.get("thumbnails") or [], "shipping": detail.get("shipping") or listing.get("shipping"),
        "location": str(detail.get("item_location") or listing.get("location") or ""),
        "category": " · ".join(f"{key}: {value}" for key, value in (detail.get("attributes") or {}).items() if key in {"Manufacturer", "Model", "Model Year", "Condition"}) or listing.get("category"),
        "detail_status": "complete",
    })
    return listing
