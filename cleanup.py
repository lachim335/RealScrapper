#!/usr/bin/env python3
"""Safely inspect and optionally clean redundant or orphaned scraper data."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "reality.sqlite3"
IMAGE_NAME = re.compile(r"[a-f0-9]{64}\.(?:webp|jpe?g|png|gif)\Z")


def image_references(db: sqlite3.Connection) -> set[str]:
    referenced: set[str] = set()
    for (raw,) in db.execute("SELECT data_json FROM properties"):
        listing = json.loads(raw)
        for url in listing.get("display_image_urls", []):
            name = Path(urlsplit(url).path).name
            if IMAGE_NAME.fullmatch(name):
                referenced.add(name)
    return referenced


def backup_database(db: sqlite3.Connection, path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.stem}.backup-{timestamp}{path.suffix}")
    counter = 1
    while backup_path.exists():
        backup_path = path.with_name(
            f"{path.stem}.backup-{timestamp}-{counter}{path.suffix}"
        )
        counter += 1
    with closing(sqlite3.connect(backup_path)) as backup:
        db.backup(backup)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite databáze")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="provést uvedené úklidové operace (výchozí režim je pouze náhled)",
    )
    args = parser.parse_args()
    db_path = args.db.resolve()
    if not db_path.is_file():
        parser.error(f"Databáze neexistuje: {db_path}")

    try:
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            duplicate_prices = db.execute(
                """SELECT COUNT(*) FROM price_history AS item
                   WHERE EXISTS (
                       SELECT 1 FROM price_history AS earlier
                       WHERE earlier.property_id = item.property_id
                         AND earlier.observed_at = item.observed_at
                         AND earlier.price_czk = item.price_czk
                         AND earlier.history_id < item.history_id
                   )"""
            ).fetchone()[0]
            orphan_sources = db.execute(
                """SELECT COUNT(*) FROM source_ids AS source
                   WHERE NOT EXISTS (
                       SELECT 1 FROM properties
                       WHERE properties.property_id = source.property_id
                   )"""
            ).fetchone()[0]
            orphan_signatures = db.execute(
                """SELECT COUNT(*) FROM signatures AS signature
                   WHERE NOT EXISTS (
                       SELECT 1 FROM source_ids AS source
                       WHERE source.source_id = signature.source_id
                         AND source.property_id = signature.property_id
                   )"""
            ).fetchone()[0]
            orphan_prices = db.execute(
                """SELECT COUNT(*) FROM price_history AS history
                   WHERE NOT EXISTS (
                       SELECT 1 FROM properties
                       WHERE properties.property_id = history.property_id
                   )"""
            ).fetchone()[0]
            properties_without_sources = db.execute(
                """SELECT COUNT(*) FROM properties AS property
                   WHERE NOT EXISTS (
                       SELECT 1 FROM source_ids AS source
                       WHERE source.property_id = property.property_id
                   )"""
            ).fetchone()[0]
            referenced_images = image_references(db)
            image_directory = db_path.parent / "images"
            orphan_image_paths = (
                [
                    path
                    for path in image_directory.iterdir()
                    if path.is_file()
                    and not path.is_symlink()
                    and IMAGE_NAME.fullmatch(path.name)
                    and path.name not in referenced_images
                ]
                if image_directory.is_dir()
                else []
            )

            print(f"Databáze: {db_path}")
            print(f"Duplicitní záznamy stejné ceny a času: {duplicate_prices}")
            print(f"Zdrojová ID bez inzerátu: {orphan_sources}")
            print(f"Otisky bez odpovídajícího ID: {orphan_signatures}")
            print(f"Ceny bez odpovídajícího inzerátu: {orphan_prices}")
            print(f"Inzeráty bez zdrojového ID (ponechány): {properties_without_sources}")
            print(f"Nepoužívané lokální obrázky: {len(orphan_image_paths)}")

            if not args.apply:
                print("Náhled bez změn. Pro provedení úklidu spusťte znovu s --apply.")
                return 0

            backup_path = backup_database(db, db_path)
            print(f"Záloha databáze: {backup_path}")
            db.execute(
                """DELETE FROM price_history
                   WHERE history_id NOT IN (
                       SELECT MIN(history_id) FROM price_history
                       GROUP BY property_id, observed_at, price_czk
                   )"""
            )
            db.execute(
                """DELETE FROM source_ids
                   WHERE NOT EXISTS (
                       SELECT 1 FROM properties
                       WHERE properties.property_id = source_ids.property_id
                   )"""
            )
            db.execute(
                """DELETE FROM signatures
                   WHERE NOT EXISTS (
                       SELECT 1 FROM source_ids
                       WHERE source_ids.source_id = signatures.source_id
                         AND source_ids.property_id = signatures.property_id
                   )"""
            )
            db.execute(
                """DELETE FROM price_history
                   WHERE NOT EXISTS (
                       SELECT 1 FROM properties
                       WHERE properties.property_id = price_history.property_id
                   )"""
            )
            db.commit()
            db.execute("PRAGMA optimize")
            db.execute("VACUUM")
            for path in orphan_image_paths:
                path.unlink()
            print(
                f"Úklid dokončen. Odstraněno duplicitních cen: {duplicate_prices}, "
                f"osiřelých ID: {orphan_sources}, otisků: {orphan_signatures}, "
                f"cen: {orphan_prices}, obrázků: {len(orphan_image_paths)}."
            )
            print("Inzeráty a jejich zdrojová data nebyly automaticky slučovány ani mazány.")
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Chyba při čištění databáze: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
