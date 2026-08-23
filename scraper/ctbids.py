"""Public-source adapter for nearby CTBids estate-auction inventory."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .government import _PublicClient, _float, _int, _utc_time
from .shopgoodwill import DataSourceError, strip_html


CTBIDS_SELLER_API = "https://sellersearch.ctbids.com/services"
CTBIDS_BUYER_API = "https://buyersearch.ctbids.com/services"


class CTBidsClient(_PublicClient):
    """Read the same unauthenticated search data used by CTBids' buyer site."""

    source_id = "ctbids"
    source_label = "CTBids"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.session.headers.update({
            "Origin": "https://buyersearch.ctbids.com",
            "Referer": "https://buyersearch.ctbids.com/",
            "Content-Type": "application/json",
        })

    @staticmethod
    def _search_body(zip_code: str, radius_miles: int, page_size: int) -> dict[str, Any]:
        return {
            "sort": [{"field": "itemclosetime", "direction": "asc"}],
            "page": {"size": page_size},
            "field": [
                "id", "title", "displayimageurl", "thumbnailurl", "itemstatus",
                "itemtypeid", "itemtypename", "itemclosetime", "itemlistingtime",
                "isshippable", "saleid", "saletitle", "locationid", "locationtitle",
                "category", "categoryGroup", "itemseourl", "startingprice",
                "buynowprice", "city", "state", "zipcode",
            ],
            "filter": [
                {"field": "salestatus", "value": "Started", "op": "=", "join": "AND"},
                {"field": "itemstatus", "value": "Ready", "op": "=", "join": "AND"},
                {"field": "zipcode", "value": zip_code, "op": "=", "join": "AND"},
                {"field": "miles", "value": str(radius_miles), "op": "=", "join": "AND"},
            ],
        }

    def search(self, zip_code: str, radius_miles: int) -> tuple[list[dict[str, Any]], int]:
        page_size = min(750, max(1, int(self.config.get("page_size", 250))))
        maximum_pages = max(1, int(self.config.get("maximum_pages", 4)))
        body = self._search_body(zip_code, radius_miles, page_size)
        found: list[dict[str, Any]] = []
        total = 0
        for _ in range(maximum_pages):
            response = self._request(
                "POST", f"{CTBIDS_SELLER_API}/api/v1/search/item/new/list", json=body
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise DataSourceError("CTBids search returned invalid JSON") from exc
            items = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(items, list):
                raise DataSourceError("CTBids search response was missing its item list")
            found.extend(item for item in items if isinstance(item, dict) and item.get("id"))
            page = payload.get("page") or {}
            total = _int(page.get("total")) or len(found)
            next_key = (page.get("keyset") or {}).get("next")
            if not items or not next_key or len(found) >= total:
                break
            body["page"] = {"size": page_size, "next": next_key}

        self._add_current_bids(found)
        return found, total

    def _add_current_bids(self, items: list[dict[str, Any]]) -> None:
        batch_size = min(250, max(1, int(self.config.get("bid_batch_size", 100))))
        by_id = {str(item.get("id")): item for item in items}
        item_ids = [int(item_id) for item_id in by_id]
        for start in range(0, len(item_ids), batch_size):
            response = self._request(
                "POST",
                f"{CTBIDS_BUYER_API}/api/v1/buyer/auction/item/current/bid",
                json={"data": {"itemIds": item_ids[start:start + batch_size]}},
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise DataSourceError("CTBids current-bid lookup returned invalid JSON") from exc
            if not isinstance(payload, dict) or payload.get("status") != "success":
                raise DataSourceError("CTBids current-bid response was missing its bid list")
            for bid in payload.get("data") or []:
                item = by_id.get(str(bid.get("itemid")))
                if item is not None:
                    item["current_bid"] = bid

    def detail(self, item_id: str, sale_id: str) -> dict[str, Any]:
        detail_body = {
            "sort": [{"field": "title", "direction": "asc"}],
            "filter": [
                {"field": "saleid", "value": _int(sale_id), "op": "=", "join": "AND"},
                {"field": "itemid", "value": _int(item_id), "op": "=", "join": "AND"},
            ],
        }
        image_body = {
            "filter": [{"field": "itemid", "value": _int(item_id), "op": "=", "join": "AND"}],
            "field": [
                "id", "name", "type", "url", "itemid", "isdisplayimage",
                "displayorder", "thumbnailurl", "compressedurl",
            ],
            "sort": [{"field": "displayorder", "direction": "asc"}],
            "page": {"size": 1000},
        }
        detail_response = self._request(
            "POST", f"{CTBIDS_SELLER_API}/api/v1/search/item/detail/{item_id}",
            json=detail_body,
        )
        image_response = self._request(
            "POST", f"{CTBIDS_SELLER_API}/api/v1/search/item/image/list", json=image_body
        )
        try:
            detail_payload = detail_response.json()
            image_payload = image_response.json()
            details = detail_payload.get("data") or []
            images = image_payload.get("data") or []
        except (AttributeError, ValueError) as exc:
            raise DataSourceError(f"CTBids detail {item_id} returned invalid JSON") from exc
        if not details or not isinstance(details[0], dict):
            raise DataSourceError(f"CTBids detail {item_id} did not include the item")
        if not isinstance(images, list):
            raise DataSourceError(f"CTBids detail {item_id} did not include a valid image list")
        bids = [{"id": _int(item_id)}]
        self._add_current_bids(bids)
        return {"item": details[0], "images": images, "bid": bids[0].get("current_bid") or {}}


def _image_values(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    image = str(item.get("displayimageurl") or "")
    thumbnail = str(item.get("thumbnailurl") or image)
    return ([image] if image.startswith("http") else [], [thumbnail] if thumbnail.startswith("http") else [])


def ctbids_listing(
    item: dict[str, Any], timestamp: str, hunt: dict[str, Any], zip_code: str, radius: int
) -> dict[str, Any]:
    item_id = str(item.get("id"))
    sale_id = str(item.get("saleid") or "")
    slug = quote(str(item.get("itemseourl") or item.get("title") or "item"), safe="-")
    bid = item.get("current_bid") or {}
    images, thumbnails = _image_values(item)
    location = ", ".join(filter(None, [
        str(item.get("city") or ""), str(item.get("state") or ""), str(item.get("zipcode") or "")
    ]))
    category = " · ".join(filter(None, [
        str(item.get("categoryGroup") or ""), str(item.get("category") or "")
    ])) or "Estate auction"
    buy_now_price = _float(item.get("buynowprice"))
    bid_count = _int(bid.get("bidcount"))
    price = _float(bid.get("bidprice")) if bid_count else _float(item.get("startingprice"))
    return {
        "item_id": f"ctbids-{sale_id}-{item_id}", "source": "ctbids", "source_label": "CTBids",
        "source_native_id": item_id, "source_sale_id": sale_id,
        "title": str(item.get("title") or "Untitled CTBids lot"), "price": price,
        "buy_now_price": buy_now_price, "has_buy_now": buy_now_price > 0,
        "bids": bid_count, "seller": str(item.get("locationtitle") or item.get("saletitle") or ""),
        "seller_id": f"ctbids-{item.get('locationid') or sale_id}",
        "start_time": _utc_time(item.get("itemlistingtime")),
        "end_time": _utc_time(bid.get("itemclosetime") or item.get("itemclosetime")),
        "listing_url": f"https://ctbids.com/estate-sale/{sale_id}/item/{item_id}/{slug}",
        "shipping": {
            "listed_price": 0.0, "pickup_only": not bool(_int(item.get("isshippable"))),
            "carrier": "Shipping available" if _int(item.get("isshippable")) else "Local pickup",
        },
        "category": category, "images": images, "thumbnails": thumbnails,
        "description": "", "location": location,
        "local_search": {"zip_code": zip_code, "radius_miles": radius},
        "discovered_by": [f"CTBids within {radius} mi of {zip_code}"],
        "hunt_categories": [str(hunt["id"])], "hunt_labels": [str(hunt["label"])],
        "detail_status": "pending", "last_seen": timestamp, "last_updated": timestamp,
    }


def apply_ctbids_detail(listing: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    item = detail.get("item") or {}
    bid = detail.get("bid") or {}
    image_rows = [row for row in detail.get("images") or [] if isinstance(row, dict)]
    images = [
        str(row.get("url") or row.get("compressedurl") or "")
        for row in image_rows
        if str(row.get("url") or row.get("compressedurl") or "").startswith("http")
    ]
    thumbnails = [
        str(row.get("thumbnailurl") or row.get("url") or "")
        for row in image_rows
        if str(row.get("thumbnailurl") or row.get("url") or "").startswith("http")
    ]
    receipt: dict[str, Any] = {}
    try:
        receipt = json.loads(str(item.get("itemreceiptmethod") or item.get("saleitemreceiptmethod") or "{}"))
    except (TypeError, ValueError):
        pass
    shippable = bool(_int(item.get("isshippable"))) or bool(receipt.get("shipping"))
    bid_count = _int(bid.get("bidcount"))
    buy_now_price = _float(item.get("buynowprice"))
    category = " · ".join(filter(None, [
        str(item.get("categoryGroup") or ""), str(item.get("category") or ""),
        str(item.get("condition") or ""),
    ])) or listing.get("category")
    listing.update({
        "title": str(item.get("title") or listing.get("title")),
        "price": _float(bid.get("bidprice")) if bid_count else _float(item.get("startingprice") or listing.get("price")),
        "buy_now_price": buy_now_price, "has_buy_now": buy_now_price > 0,
        "bids": bid_count, "start_time": _utc_time(item.get("itemlistingtime")) or listing.get("start_time"),
        "end_time": _utc_time(bid.get("itemclosetime") or item.get("itemclosetime")) or listing.get("end_time"),
        "seller": str(item.get("locationtitle") or item.get("saletitle") or listing.get("seller") or ""),
        "seller_id": f"ctbids-{item.get('locationid')}" if item.get("locationid") else str(listing.get("seller_id") or ""),
        "description": strip_html(item.get("description") or ""),
        "images": images or listing.get("images") or [],
        "thumbnails": thumbnails or images or listing.get("thumbnails") or [],
        "shipping": {
            "listed_price": _float(item.get("shippingfee")), "pickup_only": not shippable,
            "carrier": "Shipping available" if shippable else "Local pickup",
            "policy": strip_html(item.get("shippinginfo") or item.get("pickupinfo") or ""),
        },
        "location": ", ".join(filter(None, [
            str(item.get("city") or ""), str(item.get("state") or ""), str(item.get("zipcode") or "")
        ])) or listing.get("location"),
        "category": category, "detail_status": "complete",
    })
    return listing
