#!/usr/bin/env python3
"""Collect Sreality apartment listings and keep their price history in SQLite."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import logging
import math
import mimetypes
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "reality.sqlite3"
DEFAULT_SEARCH_URL = (
    "https://www.sreality.cz/hledani/prodej/byty"
    "?region=Fr%C3%BDdek-M%C3%ADstek"
)
WANTED_DISPOSITIONS = {"2+1", "3+1", "3+kk", "Atypický"}
USER_AGENT = "RealScrapper/1.0 (personal home-LAN listing tracker)"
DETAIL_DELAY_SECONDS = 0.4
IMAGE_LIMIT_PER_LISTING = 2
MAX_IMAGE_BYTES = 12 * 1024 * 1024
logger = logging.getLogger("sreality")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Nepodařilo se stáhnout {url}: {exc}") from exc


def next_data(page_html: str, url: str) -> dict[str, Any]:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        page_html,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError(
            f"Stránka {url} neobsahuje očekávaná data Sreality "
            "(může být změněná nebo dočasně nedostupná)."
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Neplatná data na stránce {url}: {exc}") from exc


def estate_search_data(page_html: str, url: str) -> tuple[dict[str, Any], int, int]:
    props = next_data(page_html, url).get("props", {}).get("pageProps", {})
    for query in props.get("dehydratedState", {}).get("queries", []):
        key = query.get("queryKey", [])
        if key and key[0] == "estatesSearch":
            data = query.get("state", {}).get("data", {})
            results = data.get("results")
            pagination = data.get("pagination", {})
            if isinstance(results, list) and pagination.get("total") is not None:
                return data, int(pagination["total"]), int(pagination.get("limit", 20))
    raise RuntimeError(f"Ve stránce {url} nebyl nalezen seznam inzerátů.")


def page_url(search_url: str, page: int) -> str:
    parts = urlsplit(search_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "strana"]
    if page > 1:
        query.append(("strana", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def listing_links(page_html: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for match in re.finditer(r'''href=["']([^"']*/detail/[^"']+)["']''', page_html):
        href = html.unescape(match.group(1))
        identifier = re.search(r"/(\d+)(?:[?#]|$)", href)
        if identifier:
            links[identifier.group(1)] = urljoin("https://www.sreality.cz", href)
    return links


def detail_data(page_html: str, url: str) -> dict[str, Any]:
    props = next_data(page_html, url).get("props", {}).get("pageProps", {})
    for query in props.get("dehydratedState", {}).get("queries", []):
        key = query.get("queryKey", [])
        if key and key[0] == "estate":
            data = query.get("state", {}).get("data")
            if isinstance(data, dict):
                return data
    raise RuntimeError(f"V detailu {url} nebyly nalezeny údaje o inzerátu.")


def _field_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "value", "text"):
            if value.get(key) is not None:
                return _field_value(value[key])
    if isinstance(value, list):
        return ", ".join(filter(None, (_field_value(item) for item in value)))
    if value is None:
        return ""
    return str(value)


def _location_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    fields = (
        "street",
        "houseNumber",
        "cityPart",
        "city",
        "municipality",
        "district",
        "region",
    )
    parts = []
    for field in fields:
        item = value.get(field)
        if item and str(item) not in parts:
            parts.append(str(item))
    return ", ".join(parts)


def _area_from_params(params: Any) -> float | None:
    if isinstance(params, dict):
        values = (params.get("usableArea"), params.get("floorArea"))
    elif isinstance(params, list):
        values = (
            param.get("value")
            for param in params
            if isinstance(param, dict)
            and any(
                label in normalize(_field_value(param.get("name")))
                for label in ("uzitna plocha", "podlahova plocha")
            )
        )
    else:
        return None
    for value in values:
        match = re.search(r"\d+(?:[,.]\d+)?", _field_value(value).replace("\xa0", " "))
        if match:
            return float(match.group(0).replace(",", "."))
    return None


PARAM_LABELS = {
    "usableArea": "Užitná plocha",
    "floorArea": "Podlahová plocha",
    "buildingArea": "Zastavěná plocha",
    "ownership": "Vlastnictví",
    "buildingType": "Typ stavby",
    "buildingCondition": "Stav objektu",
    "floorNumber": "Podlaží",
    "floors": "Počet podlaží",
    "energyEfficiencyRating": "Energetická náročnost",
    "furnished": "Vybavení",
    "elevator": "Výtah",
    "balcony": "Balkon",
    "loggia": "Lodžie",
    "terrace": "Terasa",
    "cellar": "Sklep",
    "garage": "Garáž",
    "parking": "Parkování",
    "acceptanceYear": "Rok kolaudace",
    "reconstructionYear": "Rok rekonstrukce",
    "heatingSet": "Vytápění",
    "costOfLiving": "Náklady na bydlení",
    "annuity": "Anuita",
    "priceNote": "Poznámka k ceně",
}


def listing_attributes(params: Any) -> list[dict[str, str]]:
    if not isinstance(params, dict):
        return []
    attributes = []
    for key, label in PARAM_LABELS.items():
        value = params.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            text = "Ano" if value else "Ne"
        else:
            text = _field_value(value)
        if text:
            attributes.append({"name": label, "value": text})
    return attributes


def _image_url(image: Any) -> str:
    if isinstance(image, str):
        url = image
    elif isinstance(image, dict):
        url = str(image.get("url") or "")
    else:
        return ""
    if not url:
        return ""
    image_url = urljoin("https://www.sreality.cz", url)
    parts = urlsplit(image_url)
    if "fl=" not in parts.query:
        image_url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                parts.query + ("&" if parts.query else "")
                + "fl=res,800,600,3|shr,,20|webp,60",
                parts.fragment,
            )
        )
    return image_url


def download_listing_images(
    listing: dict[str, Any], image_directory: Path
) -> None:
    """Cache the thumbnails used by the web UI, with transformed CDN URLs as fallback."""
    display_urls = []
    image_directory.mkdir(parents=True, exist_ok=True)
    for index, image_url in enumerate(listing["image_urls"]):
        display_url = image_url
        if index < IMAGE_LIMIT_PER_LISTING:
            parts = urlsplit(image_url)
            host = (parts.hostname or "").lower()
            if parts.scheme == "https" and (
                host == "sdn.cz" or host.endswith(".sdn.cz")
            ):
                digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
                try:
                    cached = next(image_directory.glob(f"{digest}.*"), None)
                    if cached:
                        display_url = f"/images/{cached.name}"
                    else:
                        request = Request(
                            image_url,
                            headers={
                                "User-Agent": USER_AGENT,
                                "Referer": "https://www.sreality.cz/",
                            },
                        )
                        with urlopen(request, timeout=20) as response:
                            content_type = response.headers.get_content_type()
                            if not content_type.startswith("image/"):
                                raise RuntimeError(
                                    f"neočekávaný typ obrázku {content_type}"
                                )
                            image_data = response.read(MAX_IMAGE_BYTES + 1)
                        if len(image_data) > MAX_IMAGE_BYTES:
                            raise RuntimeError("obrázek překračuje limit 12 MiB")
                        extension = mimetypes.guess_extension(content_type) or ".img"
                        filename = digest + extension
                        image_path = image_directory / filename
                        image_path.write_bytes(image_data)
                        display_url = f"/images/{filename}"
                except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
                    logger.warning("Obrázek inzerátu %s: %s", listing["source_id"], exc)
        display_urls.append(display_url)
    listing["display_image_urls"] = display_urls


def normalized_listing(summary: dict[str, Any], detail: dict[str, Any] | None, url: str) -> dict[str, Any]:
    detail = detail or {}
    name = _field_value(detail.get("name") or summary.get("name"))
    disposition = _field_value(
        detail.get("categorySubCb") or summary.get("categorySubCb")
    )
    locality = detail.get("locality") or summary.get("locality")
    locality_text = _location_text(locality)
    images = detail.get("images") or summary.get("images") or []
    image_urls = list(dict.fromkeys(filter(None, (_image_url(image) for image in images))))
    price = detail.get("priceCzk")
    if price is None:
        price = summary.get("priceCzk")
    try:
        price = int(price) if price is not None and int(price) > 0 else None
    except (TypeError, ValueError):
        price = None

    text_for_area = f"{name} {_field_value(detail.get('description'))}"
    area = _area_from_params(detail.get("params"))
    if area is None:
        match = re.search(r"(\d+(?:[,.]\d+)?)\s*m(?:²|2)", text_for_area, re.IGNORECASE)
        if match:
            area = float(match.group(1).replace(",", "."))

    listing = {
        "source_id": str(summary.get("id") or detail.get("id") or ""),
        "title": name,
        "disposition": disposition,
        "location": locality_text,
        "area_m2": area,
        "price_czk": price,
        "description": _field_value(detail.get("description")),
        "details_loaded": bool(detail),
        "attributes": listing_attributes(detail.get("params")),
        "image_urls": image_urls,
        "url": url,
        "first_seen": None,
        "last_seen": None,
        "status": "active",
    }
    listing["signature"] = make_signature(listing)
    return listing


def make_signature(listing: dict[str, Any]) -> dict[str, Any]:
    location = normalize(listing.get("location"))
    images = []
    for image in listing.get("image_urls", []):
        path = urlsplit(image).path.lower()
        if path:
            images.append(path)
    description = normalize(listing.get("description"))
    return {
        "location": location,
        "disposition": normalize(listing.get("disposition")),
        "area_m2": listing.get("area_m2"),
        "images": sorted(set(images)),
        "description": description,
        "text_hash": hashlib.sha256(description.encode("utf-8")).hexdigest(),
    }


def fill_signature(signature: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Fill empty signature fields (description, photos, area) from stored data."""
    fallback = make_signature(data)
    filled = dict(signature)
    if not filled.get("description") and fallback["description"]:
        filled["description"] = fallback["description"]
        filled["text_hash"] = fallback["text_hash"]
    if not filled.get("images") and fallback["images"]:
        filled["images"] = fallback["images"]
    if filled.get("area_m2") is None and fallback["area_m2"] is not None:
        filled["area_m2"] = fallback["area_m2"]
    return filled


def identity_match(
    candidate: dict[str, Any], previous: dict[str, Any]
) -> bool:
    new = candidate.get("signature") or make_signature(candidate)
    old = previous.get("signature") or {}
    if not new.get("location") or new.get("disposition") != old.get("disposition"):
        return False
    new_area, old_area = new.get("area_m2"), old.get("area_m2")
    if (
        new_area is not None
        and old_area is not None
        and abs(float(new_area) - float(old_area)) > 2.0
    ):
        return False

    overlap = set(new.get("images", [])) & set(old.get("images", []))
    same_location = new["location"] == old.get("location")
    new_description = new.get("description", "")
    old_description = old.get("description", "")
    descriptions_are_useful = len(new_description) >= 100 and len(old_description) >= 100
    text_similarity = (
        difflib.SequenceMatcher(None, new_description, old_description).ratio()
        if descriptions_are_useful
        else 0
    )
    if new_area is None or old_area is None:
        return same_location and (
            len(overlap) >= 2
            or (descriptions_are_useful and text_similarity >= 0.80)
        )
    return (
        (same_location and len(overlap) >= 2)
        or (
            same_location
            and descriptions_are_useful
            and text_similarity >= 0.80
            and abs(float(new_area) - float(old_area)) <= 2.0
        )
    )


class ListingStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.image_directory = path.parent / "images"
        # Source IDs present in the current search results. Two listings that
        # are online at the same time are separate offers (e.g. a flat with and
        # without a garage), so they must never share one price history.
        self.current_ids: set[str] = set()
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS properties (
                property_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_ids (
                source_id TEXT PRIMARY KEY,
                property_id INTEGER NOT NULL REFERENCES properties(property_id)
            );
            CREATE TABLE IF NOT EXISTS signatures (
                source_id TEXT PRIMARY KEY,
                property_id INTEGER NOT NULL REFERENCES properties(property_id),
                signature_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS price_history (
                history_id INTEGER PRIMARY KEY,
                property_id INTEGER NOT NULL REFERENCES properties(property_id),
                observed_at TEXT NOT NULL,
                price_czk INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS price_history_property_date
                ON price_history(property_id, observed_at);
            """
        )
        self._repair_signatures()

    def _repair_signatures(self) -> None:
        """Fill signatures saved without description/photos from stored listing data."""
        rows = self.db.execute(
            """SELECT signatures.source_id, signatures.signature_json, properties.data_json
               FROM signatures JOIN properties USING (property_id)"""
        ).fetchall()
        for row in rows:
            signature = json.loads(row["signature_json"])
            filled = fill_signature(signature, json.loads(row["data_json"]))
            if filled != signature:
                self.db.execute(
                    "UPDATE signatures SET signature_json = ? WHERE source_id = ?",
                    (json.dumps(filled, ensure_ascii=False), row["source_id"]),
                )
        self.db.commit()

    def existing_ids(self) -> set[str]:
        return {
            row["source_id"]
            for row in self.db.execute("SELECT source_id FROM source_ids")
        }

    def needs_detail(self, source_id: str) -> bool:
        row = self.db.execute(
            """SELECT properties.data_json FROM properties
               JOIN source_ids USING (property_id) WHERE source_ids.source_id = ?""",
            (source_id,),
        ).fetchone()
        return row is None or not json.loads(row["data_json"]).get("details_loaded")

    def _has_other_current_id(self, property_id: int, source_id: str) -> bool:
        return any(
            row["source_id"] in self.current_ids
            for row in self.db.execute(
                "SELECT source_id FROM source_ids WHERE property_id = ? AND source_id != ?",
                (property_id, source_id),
            )
        )

    def _find_property(self, listing: dict[str, Any]) -> int | None:
        matches: list[tuple[int, str]] = []
        rows = self.db.execute(
            "SELECT property_id, signature_json FROM signatures"
        ).fetchall()
        for row in rows:
            if self._has_other_current_id(row["property_id"], listing["source_id"]):
                continue
            previous = json.loads(row["signature_json"])
            if identity_match(listing, {"signature": previous}):
                matches.append((row["property_id"], row["signature_json"]))
        property_ids = {row[0] for row in matches}
        return next(iter(property_ids)) if len(property_ids) == 1 else None

    def _remove_mismatched_aliases(
        self, property_id: int, listing: dict[str, Any]
    ) -> bool:
        rows = self.db.execute(
            """SELECT source_ids.source_id, signatures.signature_json
               FROM source_ids
               LEFT JOIN signatures USING (source_id)
               WHERE source_ids.property_id = ? AND source_ids.source_id != ?""",
            (property_id, listing["source_id"]),
        ).fetchall()
        mismatched = [
            row["source_id"]
            for row in rows
            if not row["signature_json"]
            or row["source_id"] in self.current_ids
            or not identity_match(
                listing, {"signature": json.loads(row["signature_json"])}
            )
        ]
        if not mismatched:
            return False
        for source_id in mismatched:
            self.db.execute("DELETE FROM signatures WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM source_ids WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM price_history WHERE property_id = ?", (property_id,))
        return True

    def save(self, listing: dict[str, Any], observed_at: str) -> int:
        source_id = listing["source_id"]
        listing["first_seen"] = listing.get("first_seen") or observed_at
        listing["last_seen"] = observed_at
        listing["status"] = "active"
        row = self.db.execute(
            "SELECT property_id FROM source_ids WHERE source_id = ?", (source_id,)
        ).fetchone()
        property_id = row["property_id"] if row else self._find_property(listing)
        history_reset = (
            self._remove_mismatched_aliases(property_id, listing)
            if property_id is not None
            else False
        )

        if property_id is None:
            cursor = self.db.execute(
                """INSERT INTO properties(first_seen, last_seen, status, data_json)
                   VALUES (?, ?, 'active', ?)""",
                (observed_at, observed_at, json.dumps(listing, ensure_ascii=False)),
            )
            property_id = int(cursor.lastrowid)
        else:
            previous_row = self.db.execute(
                "SELECT data_json FROM properties WHERE property_id = ?",
                (property_id,),
            ).fetchone()
            previous = json.loads(previous_row["data_json"])
            merged = {**previous, **listing}
            if not listing.get("details_loaded"):
                for field in (
                    "description",
                    "attributes",
                    "image_urls",
                    "area_m2",
                    "details_loaded",
                ):
                    if previous.get(field):
                        merged[field] = previous[field]
            listing["signature"] = fill_signature(listing["signature"], merged)
            merged["signature"] = listing["signature"]
            merged["first_seen"] = previous.get("first_seen") or observed_at
            merged["last_seen"] = observed_at
            merged["status"] = "active"
            self.db.execute(
                """UPDATE properties
                   SET last_seen = ?, status = 'active', data_json = ?
                   WHERE property_id = ?""",
                (observed_at, json.dumps(merged, ensure_ascii=False), property_id),
            )

        self.db.execute(
            "INSERT OR IGNORE INTO source_ids(source_id, property_id) VALUES (?, ?)",
            (source_id, property_id),
        )
        self.db.execute(
            """INSERT INTO signatures(source_id, property_id, signature_json)
               VALUES (?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                   property_id = excluded.property_id,
                   signature_json = excluded.signature_json""",
            (
                source_id,
                property_id,
                json.dumps(listing["signature"], ensure_ascii=False),
            ),
        )

        price = listing.get("price_czk")
        if price is not None:
            last_price = self.db.execute(
                """SELECT price_czk FROM price_history
                   WHERE property_id = ? ORDER BY history_id DESC LIMIT 1""",
                (property_id,),
            ).fetchone()
            if history_reset or last_price is None or last_price["price_czk"] != price:
                self.db.execute(
                    """INSERT INTO price_history(property_id, observed_at, price_czk)
                       VALUES (?, ?, ?)""",
                    (property_id, observed_at, price),
                )
        return property_id

    def mark_unseen(self, seen_ids: set[str]) -> int:
        rows = self.db.execute(
            "SELECT property_id FROM properties WHERE status = 'active'"
        ).fetchall()
        inactive = 0
        for row in rows:
            ids = {
                item["source_id"]
                for item in self.db.execute(
                    "SELECT source_id FROM source_ids WHERE property_id = ?",
                    (row["property_id"],),
                )
            }
            if ids and not ids.intersection(seen_ids):
                self.db.execute(
                    "UPDATE properties SET status = 'inactive' WHERE property_id = ?",
                    (row["property_id"],),
                )
                inactive += 1
        return inactive

    def close(self) -> None:
        self.db.close()


def collect(search_url: str, store: ListingStore) -> tuple[int, int]:
    first_page = fetch_html(page_url(search_url, 1))
    _, total, page_size = estate_search_data(first_page, page_url(search_url, 1))
    page_count = max(1, math.ceil(total / max(1, page_size)))
    logger.info("Sreality hlásí %s výsledků na %s stránkách.", total, page_count)
    pages = [first_page]
    for number in range(2, page_count + 1):
        url = page_url(search_url, number)
        pages.append(fetch_html(url))

    observed_at = now_utc()
    store.current_ids = {
        str(summary.get("id"))
        for page_number, page_html in enumerate(pages, start=1)
        for summary in estate_search_data(page_html, page_url(search_url, page_number))[0]["results"]
        if summary.get("id")
        and _field_value(summary.get("categorySubCb")) in WANTED_DISPOSITIONS
    }
    seen_ids: set[str] = set()
    saved = 0
    known_ids = store.existing_ids()
    for page_number, page_html in enumerate(pages, start=1):
        data, _, _ = estate_search_data(page_html, page_url(search_url, page_number))
        links = listing_links(page_html)
        for summary in data["results"]:
            disposition = _field_value(summary.get("categorySubCb"))
            if disposition not in WANTED_DISPOSITIONS:
                continue
            source_id = str(summary.get("id") or "")
            if not source_id:
                logger.warning("Přeskakuji inzerát bez ID na stránce %s.", page_number)
                continue
            seen_ids.add(source_id)
            url = links.get(source_id)
            detail = None
            if url and (source_id not in known_ids or store.needs_detail(source_id)):
                try:
                    detail = detail_data(fetch_html(url), url)
                except RuntimeError as exc:
                    logger.warning("Detail inzerátu %s: %s", source_id, exc)
                time.sleep(DETAIL_DELAY_SECONDS)
            elif not url:
                logger.warning(
                    "Pro inzerát %s nebyl nalezen odkaz na detail; ukládám dostupná data.",
                    source_id,
                )
            listing = normalized_listing(summary, detail, url or "")
            if not listing["details_loaded"] and source_id in known_ids:
                previous = store.db.execute(
                    """SELECT data_json FROM properties
                       JOIN source_ids USING (property_id)
                       WHERE source_ids.source_id = ?""",
                    (source_id,),
                ).fetchone()
                if previous:
                    previous_data = json.loads(previous["data_json"])
                    listing["image_urls"] = [
                        _image_url(image)
                        for image in previous_data.get(
                            "image_urls", listing["image_urls"]
                        )
                    ]
            download_listing_images(listing, store.image_directory)
            store.save(listing, observed_at)
            saved += 1
    inactive = store.mark_unseen(seen_ids)
    store.db.commit()
    return saved, inactive


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"search_url": DEFAULT_SEARCH_URL, "database": str(DEFAULT_DB)}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Konfigurační soubor {path} nelze načíst: {exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"Konfigurační soubor {path} musí obsahovat JSON objekt.")
    database = Path(config.get("database", str(DEFAULT_DB)))
    if not database.is_absolute():
        database = path.resolve().parent / database
    return {
        "search_url": config.get("search_url", DEFAULT_SEARCH_URL),
        "database": str(database),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config.json", help="cesta ke konfiguraci"
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="opakovat sběr v sekundovém intervalu (výchozí: spustit jednou)",
    )
    args = parser.parse_args()
    if args.interval is not None and args.interval < 60:
        parser.error("--interval musí být alespoň 60 sekund.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        config = load_config(args.config)
        search_url = str(config["search_url"])
        store = ListingStore(Path(config["database"]))
        try:
            while True:
                saved, inactive = collect(search_url, store)
                logger.info(
                    "Aktualizováno %s inzerátů; %s inzerátů označeno jako neaktivní.",
                    saved,
                    inactive,
                )
                if args.interval is None:
                    break
                time.sleep(args.interval)
        finally:
            store.close()
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Ukončeno uživatelem.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
