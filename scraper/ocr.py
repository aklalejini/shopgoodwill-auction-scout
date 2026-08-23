"""Bounded, cached, zero-API-cost OCR for auction listing images."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


SIGNAL_PATTERNS = (
    re.compile(r"\b(?:14K|18K|585|750)\b", re.I),
    re.compile(r"\b\d{1,2}[A-Z]{1,3}\d{1,2}[A-Z]{0,2}\b"),
    re.compile(r"\bMADE IN (?:ENGLAND|IRELAND|USA|U\.S\.A\.|HOLLAND|GERMANY|DENMARK|ITALY|FRANCE|JAPAN)\b", re.I),
    re.compile(r"\b(?:TELEFUNKEN|MULLARD|AMPEREX|WESTERN ELECTRIC|SHEAFFER|PARKER|WATERMAN|DUNHILL|CASTELLO|PETERSON)\b", re.I),
)


def available() -> bool:
    return shutil.which("tesseract") is not None


def _read_image_text(url: str, timeout: float, user_agent: str) -> tuple[str, str]:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        response.raise_for_status()
        suffix = Path(url.split("?", 1)[0]).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(response.content)
            image_path = Path(handle.name)
        try:
            result = subprocess.run(
                ["tesseract", str(image_path), "stdout", "--psm", "11", "-l", "eng"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                return "", (result.stderr or "OCR failed").strip()[:240]
            text = re.sub(r"[ \t]+", " ", result.stdout)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text[:12000], ""
        finally:
            image_path.unlink(missing_ok=True)
    except (OSError, requests.RequestException, subprocess.SubprocessError) as exc:
        return "", str(exc)[:240]


def _attach_cached_text(item: dict[str, Any], cache: dict[str, Any]) -> None:
    texts = [
        str((cache.get(url) or {}).get("text") or "")
        for url in item.get("images") or []
        if url in cache
    ]
    combined = "\n".join(text for text in texts if text).strip()
    if combined:
        item["ocr_text"] = combined
        hits = {
            match.group(0).upper()
            for pattern in SIGNAL_PATTERNS
            for match in pattern.finditer(combined)
        }
        item["ocr_hits"] = sorted(hits)
    else:
        item.pop("ocr_text", None)
        item.pop("ocr_hits", None)


def process_ocr(
    items: list[dict[str, Any]],
    cache: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """OCR a bounded number of uncached listings and attach cached results."""
    enabled = bool(config.get("enabled", True))
    executable = available()
    timestamp = datetime.now(timezone.utc).isoformat()
    for item in items:
        _attach_cached_text(item, cache)
    if not enabled or not executable:
        return {"available": executable, "listings_processed": 0, "images_processed": 0}

    max_listings = int(config.get("max_listings_per_run", 12))
    max_images = int(config.get("max_images_per_listing", 6))
    timeout = float(config.get("timeout_seconds", 20))
    user_agent = str(config.get("user_agent") or "AuctionScout-OCR/1.0")
    candidates = [
        item for item in items
        if item.get("detail_status") == "complete"
        and any(url not in cache for url in (item.get("images") or [])[:max_images])
    ]
    candidates.sort(
        key=lambda item: (
            -len(item.get("images") or []),
            len(str(item.get("title") or "")),
            -int(item.get("score") or 0),
        )
    )
    processed_listings = 0
    processed_images = 0
    for item in candidates[:max_listings]:
        changed = False
        for url in (item.get("images") or [])[:max_images]:
            if url in cache:
                continue
            text, error = _read_image_text(url, timeout, user_agent)
            cache[url] = {"text": text, "error": error, "processed_at": timestamp}
            processed_images += 1
            changed = True
        if changed:
            processed_listings += 1
        _attach_cached_text(item, cache)

    maximum_cache = int(config.get("maximum_cache_records", 10000))
    if len(cache) > maximum_cache:
        active_urls = {url for item in items for url in item.get("images") or []}
        ordered = sorted(
            cache,
            key=lambda url: (url in active_urls, str((cache.get(url) or {}).get("processed_at") or "")),
            reverse=True,
        )
        keep = set(ordered[:maximum_cache])
        for url in list(cache):
            if url not in keep:
                cache.pop(url, None)

    return {
        "available": True,
        "listings_processed": processed_listings,
        "images_processed": processed_images,
    }

