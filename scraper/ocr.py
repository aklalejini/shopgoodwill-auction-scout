"""Bounded, cached, zero-API-cost OCR for auction listing images."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import csv
from io import BytesIO, StringIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image


SIGNAL_PATTERNS = (
    re.compile(r"\b(?:14K|18K|585|750)\b", re.I),
    re.compile(
        r"\b(?:12AX7|12AU7|ECC803S?|ECC8[123]|6922|E88CC|CV4004|M8137|"
        r"EL34|GZ34|5AR4|KT88|KT66|6550|6SN7(?:GT|W)?|6L6GC|EL84|300B|5751|"
        r"XF[1-4]|F3[1-4])\b",
        re.I,
    ),
    re.compile(r"\bMADE IN (?:ENGLAND|IRELAND|USA|U\.S\.A\.|HOLLAND|GERMANY|DENMARK|ITALY|FRANCE|JAPAN)\b", re.I),
    re.compile(r"\b(?:TELEFUNKEN|MULLARD|AMPEREX|WESTERN ELECTRIC|SHEAFFER|PARKER|WATERMAN|DUNHILL|CASTELLO|PETERSON|HEMINGRAY|BROOKFIELD|WHITALL TATUM|NATCO|EMMINGER|TWIGGS|HARLOE)\b", re.I),
)


def _extract_signal_hits(text: str) -> list[str]:
    """Keep only recognized collector identifiers, never arbitrary OCR fragments."""
    return sorted({
        match.group(0).upper()
        for pattern in SIGNAL_PATTERNS
        for match in pattern.finditer(text)
    })


def _parse_tesseract_tsv(output: str, minimum_confidence: float) -> str:
    """Rebuild OCR lines using only words Tesseract read with useful confidence."""
    lines: dict[tuple[str, str, str, str], list[tuple[int, str]]] = {}
    for row in csv.DictReader(StringIO(output), delimiter="\t"):
        word = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        if not word:
            continue
        try:
            confidence = float(row.get("conf") or -1)
            word_number = int(row.get("word_num") or 0)
        except (TypeError, ValueError):
            continue
        if confidence < minimum_confidence:
            continue
        if sum(character.isalnum() for character in word) < 2:
            continue
        key = tuple(str(row.get(field) or "") for field in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append((word_number, word))
    return "\n".join(
        " ".join(word for _, word in sorted(words))
        for words in lines.values()
        if words
    )


def available() -> bool:
    return shutil.which("tesseract") is not None


def _detect_glass_color_signals(content: bytes) -> list[str]:
    """Return conservative hue clues; these are leads, never color authentication."""
    try:
        with Image.open(BytesIO(content)) as source:
            image = source.convert("RGB")
            image.thumbnail((240, 240))
            width, height = image.size
            if width < 20 or height < 20:
                return []
            margin_x, margin_y = int(width * 0.12), int(height * 0.12)
            image = image.crop((margin_x, margin_y, width - margin_x, height - margin_y))
            hsv = image.convert("HSV")
    except (OSError, ValueError):
        return []

    bins = {"blue": 0, "purple": 0, "amber": 0, "yellow_olive": 0, "green_teal": 0}
    colored = 0
    total = max(1, hsv.width * hsv.height)
    for hue, saturation, value in hsv.get_flattened_data():
        sat = saturation / 255
        val = value / 255
        if sat < 0.30 or val < 0.16 or val > 0.96:
            continue
        degrees = hue * 360 / 255
        bucket = None
        if 195 <= degrees < 255:
            bucket = "blue"
        elif 255 <= degrees < 330:
            bucket = "purple"
        elif 12 <= degrees < 50:
            bucket = "amber"
        elif 50 <= degrees < 105:
            bucket = "yellow_olive"
        elif 105 <= degrees < 195:
            bucket = "green_teal"
        if bucket:
            bins[bucket] += 1
            colored += 1

    if colored < total * 0.07:
        return []
    bucket, count = max(bins.items(), key=lambda entry: entry[1])
    if count < colored * 0.55 or count < total * 0.055:
        return []
    return [{
        "blue": "strong blue glass color",
        "purple": "purple glass color",
        "amber": "amber glass color",
        "yellow_olive": "yellow or olive glass color",
        "green_teal": "green or teal glass color",
    }[bucket]]


def _read_image_text(
    url: str, timeout: float, user_agent: str, minimum_confidence: float
) -> tuple[list[str], str, list[str]]:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        response.raise_for_status()
        visual_signals = _detect_glass_color_signals(response.content)
        suffix = Path(url.split("?", 1)[0]).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(response.content)
            image_path = Path(handle.name)
        try:
            result = subprocess.run(
                ["tesseract", str(image_path), "stdout", "--psm", "11", "-l", "eng", "tsv"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                return [], (result.stderr or "OCR failed").strip()[:240], visual_signals
            text = _parse_tesseract_tsv(result.stdout, minimum_confidence)
            return _extract_signal_hits(text), "", visual_signals
        finally:
            image_path.unlink(missing_ok=True)
    except (OSError, requests.RequestException, subprocess.SubprocessError) as exc:
        return [], str(exc)[:240], []


def _is_insulator(item: dict[str, Any]) -> bool:
    return (
        "glass-insulators" in (item.get("hunt_categories") or [])
        or (item.get("primary_hunt") or {}).get("id") == "glass-insulators"
    )


def _attach_cached_text(item: dict[str, Any], cache: dict[str, Any]) -> None:
    hits = sorted({
        str(hit).upper()
        for url in item.get("images") or []
        if url in cache
        for hit in (cache.get(url) or {}).get("hits", [])
        if hit
    })
    if hits:
        # This field is internal scoring input and is removed before publication.
        item["ocr_text"] = "\n".join(hits)
        item["ocr_hits"] = hits
    else:
        item.pop("ocr_text", None)
        item.pop("ocr_hits", None)
    if _is_insulator(item):
        visual_hits = sorted({
            str(signal)
            for url in item.get("images") or []
            for signal in (cache.get(url) or {}).get("visual_signals", [])
            if signal
        })
        if visual_hits:
            item["visual_hits"] = visual_hits
            item["visual_text"] = "\n".join(visual_hits)
        else:
            item.pop("visual_hits", None)
            item.pop("visual_text", None)
    else:
        item.pop("visual_hits", None)
        item.pop("visual_text", None)


def process_ocr(
    items: list[dict[str, Any]],
    cache: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """OCR a bounded number of uncached listings and attach cached results."""
    enabled = bool(config.get("enabled", True))
    executable = available()
    timestamp = datetime.now(timezone.utc).isoformat()
    # Migrate the old cache format even when OCR is disabled or unavailable.
    for cached in cache.values():
        if isinstance(cached, dict):
            cached.pop("text", None)
    for item in items:
        _attach_cached_text(item, cache)
    if not enabled or not executable:
        return {"available": executable, "listings_processed": 0, "images_processed": 0}

    max_listings = int(config.get("max_listings_per_run", 12))
    max_images = int(config.get("max_images_per_listing", 6))
    timeout = float(config.get("timeout_seconds", 20))
    minimum_confidence = float(config.get("minimum_confidence", 65))
    user_agent = str(config.get("user_agent") or "AuctionScout-OCR/1.0")
    candidates = [
        item for item in items
        if item.get("detail_status") == "complete"
        and any(
            url not in cache
            or "hits" not in (cache.get(url) or {})
            or (_is_insulator(item) and "visual_signals" not in (cache.get(url) or {}))
            for url in (item.get("images") or [])[:max_images]
        )
    ]
    candidates.sort(
        key=lambda item: (
            0 if _is_insulator(item) else 1,
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
            if url in cache and "hits" in (cache.get(url) or {}) and not (
                _is_insulator(item) and "visual_signals" not in (cache.get(url) or {})
            ):
                continue
            hits, error, visual_signals = _read_image_text(
                url, timeout, user_agent, minimum_confidence
            )
            cache[url] = {
                "hits": hits, "error": error, "visual_signals": visual_signals,
                "processed_at": timestamp,
            }
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
