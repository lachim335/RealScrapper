import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from scraper import (
    ListingStore,
    WANTED_DISPOSITIONS,
    _image_url,
    download_listing_images,
    estate_search_data,
    fallback_detail_url,
    fetch_czech_page,
    identity_match,
    make_signature,
    normalized_listing,
    page_url,
)
from run import (
    build_interactive_command,
    cleanup_command,
    scraper_command,
    server_command,
)


class ScraperTests(unittest.TestCase):
    def test_launcher_builds_commands_with_options(self):
        self.assertEqual(
            scraper_command("settings.json", "3600"),
            [
                sys.executable,
                str(Path(__file__).parents[1] / "scraper.py"),
                "--config",
                "settings.json",
                "--interval",
                "3600",
            ],
        )
        self.assertEqual(
            server_command("127.0.0.1", "8080", "custom.sqlite3")[-6:],
            ["--host", "127.0.0.1", "--port", "8080", "--db", "custom.sqlite3"],
        )
        self.assertNotIn("--apply", cleanup_command("db.sqlite3", False))
        self.assertEqual(
            cleanup_command("db.sqlite3", True)[-1],
            "--apply",
        )

    def test_launcher_interactively_confirms_cleanup_mutations(self):
        answers = iter(["db.sqlite3", "ano"])
        command = build_interactive_command("3", lambda _: next(answers))
        self.assertEqual(command[-2:], ["db.sqlite3", "--apply"])

    def test_launcher_validates_interval_and_port(self):
        scraper_answers = iter(["config.json", "30", "3600"])
        scrape = build_interactive_command("1", lambda _: next(scraper_answers))
        self.assertEqual(scrape[-2:], ["--interval", "3600"])

        server_answers = iter(["127.0.0.1", "70000", "8000", ""])
        server = build_interactive_command("2", lambda _: next(server_answers))
        self.assertEqual(server[-4:], ["--host", "127.0.0.1", "--port", "8000"])

    def test_scraper_includes_requested_dispositions(self):
        self.assertTrue({"2+1", "3+1", "3+kk", "Atypický"} <= WANTED_DISPOSITIONS)

    def test_extracts_server_rendered_results(self):
        payload = {
            "props": {
                "pageProps": {
                    "dehydratedState": {
                        "queries": [
                            {
                                "queryKey": ["estatesSearch", {}],
                                "state": {
                                    "data": {
                                        "results": [{"id": 123}],
                                        "pagination": {"total": 42, "limit": 22},
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
        page = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
        data, total, limit = estate_search_data(page, "https://example.test")
        self.assertEqual(data["results"][0]["id"], 123)
        self.assertEqual((total, limit), (42, 22))

    def test_page_url_preserves_search_filters(self):
        url = page_url("https://example.test/search?region=F-M&strana=4", 2)
        self.assertIn("region=F-M", url)
        self.assertIn("strana=2", url)
        self.assertNotIn("strana=4", url)

    def test_image_url_uses_sreality_transform(self):
        image = _image_url("//d18-a.sdn.cz/photo.jpeg")
        self.assertEqual(
            image,
            "https://d18-a.sdn.cz/photo.jpeg?fl=res,800,600,3|shr,,20|webp,60",
        )

    def test_downloaded_thumbnails_are_cached_and_served_locally(self):
        class FakeResponse(io.BytesIO):
            headers = Message()
            headers.set_type("image/webp")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        with tempfile.TemporaryDirectory() as directory:
            listing = {
                "source_id": "123",
                "image_urls": [_image_url("//d18-a.sdn.cz/photo.jpeg")],
            }
            image_dir = Path(directory) / "images"
            with patch("scraper.urlopen", return_value=FakeResponse(b"webp data")) as fetch:
                download_listing_images(listing, image_dir)
                self.assertEqual(fetch.call_count, 1)
            image_name = hashlib.sha256(
                listing["image_urls"][0].encode("utf-8")
            ).hexdigest() + ".webp"
            self.assertEqual(listing["display_image_urls"], [f"/images/{image_name}"])
            self.assertEqual((image_dir / image_name).read_bytes(), b"webp data")
            with patch("scraper.urlopen") as fetch:
                download_listing_images(listing, image_dir)
                fetch.assert_not_called()

    def test_identity_uses_photos_and_property_features(self):
        listing = {
            "location": "F. Čejky, Frýdek-Místek",
            "disposition": "2+1",
            "area_m2": 80,
            "image_urls": ["https://img.test/one.jpeg", "https://img.test/two.jpeg"],
            "description": "Prostorný byt s parkováním.",
        }
        listing["signature"] = make_signature(listing)
        repost = {
            **listing,
            "image_urls": [
                "https://img.test/one.jpeg",
                "https://img.test/two.jpeg",
                "https://img.test/three.jpeg",
            ],
        }
        repost["signature"] = make_signature(repost)
        self.assertTrue(identity_match(repost, listing))
        repost["disposition"] = "3+1"
        repost["signature"] = make_signature(repost)
        self.assertFalse(identity_match(repost, listing))

    def test_empty_descriptions_do_not_match_different_listings(self):
        first = {
            "location": "Tolstého, Frýdek-Místek",
            "disposition": "3+1",
            "area_m2": 76,
            "image_urls": ["https://img.test/first.jpeg"],
            "description": "",
        }
        second = {
            **first,
            "area_m2": 77,
            "image_urls": ["https://img.test/second.jpeg"],
        }
        first["signature"] = make_signature(first)
        second["signature"] = make_signature(second)
        self.assertFalse(identity_match(first, second))

    def test_mismatched_old_alias_is_removed_and_bad_history_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ListingStore(Path(directory) / "test.sqlite3")
            current = {
                "source_id": "current-id",
                "title": "Current listing",
                "disposition": "3+1",
                "location": "Tolstého, Frýdek-Místek",
                "area_m2": 77,
                "price_czk": 5_300_000,
                "description": "",
                "image_urls": ["https://img.test/current.jpeg"],
                "url": "https://example.test/current-id",
            }
            current["signature"] = make_signature(current)
            property_id = store.save(current, "2026-01-01T10:00:00+00:00")
            old_signature = {
                "location": current["signature"]["location"],
                "disposition": current["signature"]["disposition"],
                "area_m2": 76,
                "images": ["/old-listing.jpeg"],
                "description": "",
            }
            store.db.execute(
                "INSERT INTO source_ids(source_id, property_id) VALUES (?, ?)",
                ("old-id", property_id),
            )
            store.db.execute(
                """INSERT INTO signatures(source_id, property_id, signature_json)
                   VALUES (?, ?, ?)""",
                ("old-id", property_id, json.dumps(old_signature)),
            )
            store.db.executemany(
                """INSERT INTO price_history(property_id, observed_at, price_czk)
                   VALUES (?, ?, ?)""",
                [
                    (property_id, "2026-01-01T10:00:00+00:00", 4_499_000),
                    (property_id, "2026-01-01T10:00:00+00:00", 5_300_000),
                ],
            )

            store.save(current, "2026-01-02T10:00:00+00:00")

            source_ids = store.db.execute(
                "SELECT source_id FROM source_ids WHERE property_id = ?",
                (property_id,),
            ).fetchall()
            self.assertEqual([row[0] for row in source_ids], ["current-id"])
            history = store.db.execute(
                "SELECT price_czk FROM price_history WHERE property_id = ?",
                (property_id,),
            ).fetchall()
            self.assertEqual([row[0] for row in history], [5_300_000])
            store.close()

    def test_cleanup_defaults_to_dry_run_and_creates_backup_when_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "reality.sqlite3"
            store = ListingStore(db_path)
            listing = {
                "source_id": "listing-id",
                "title": "Byt 3+kk",
                "disposition": "3+kk",
                "location": "Frýdek-Místek",
                "area_m2": 70,
                "price_czk": 4_000_000,
                "description": "Popis",
                "image_urls": [],
                "url": "https://example.test/listing-id",
            }
            listing["signature"] = make_signature(listing)
            property_id = store.save(listing, "2026-01-01T10:00:00+00:00")
            store.db.execute(
                """INSERT INTO price_history(property_id, observed_at, price_czk)
                   VALUES (?, ?, ?)""",
                (property_id, "2026-01-01T10:00:00+00:00", 4_000_000),
            )
            store.db.commit()
            store.close()

            command = [sys.executable, str(Path(__file__).parents[1] / "cleanup.py"), "--db", str(db_path)]
            preview = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertIn("Duplicitní záznamy stejné ceny a času: 1", preview.stdout)
            self.assertFalse(list(Path(directory).glob("*.backup-*.sqlite3")))

            applied = subprocess.run(
                [*command, "--apply"], capture_output=True, text=True, check=True
            )
            self.assertIn("Úklid dokončen", applied.stdout)
            self.assertEqual(
                sqlite3.connect(db_path)
                .execute("SELECT COUNT(*) FROM price_history")
                .fetchone()[0],
                1,
            )
            self.assertEqual(len(list(Path(directory).glob("*.backup-*.sqlite3"))), 1)

    def test_price_changes_are_recorded_and_reissued_id_is_linked(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ListingStore(Path(directory) / "test.sqlite3")
            first = {
                "source_id": "old-id",
                "title": "Prodej bytu 2+1 80 m²",
                "disposition": "2+1",
                "location": "F. Čejky, Frýdek-Místek",
                "area_m2": 80,
                "price_czk": 4_600_000,
                "description": "Prostorný byt s parkováním.",
                "image_urls": ["https://img.test/one.jpeg", "https://img.test/two.jpeg"],
                "url": "https://example.test/old-id",
            }
            first["signature"] = make_signature(first)
            property_id = store.save(first, "2026-01-01T10:00:00+00:00")
            repost = {**first, "source_id": "new-id", "price_czk": 4_400_000}
            repost["signature"] = make_signature(repost)
            same_id = store.save(repost, "2026-01-02T10:00:00+00:00")
            store.db.commit()
            self.assertEqual(property_id, same_id)
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM properties").fetchone()[0], 1
            )
            prices = store.db.execute(
                "SELECT price_czk FROM price_history ORDER BY history_id"
            ).fetchall()
            self.assertEqual([row[0] for row in prices], [4_600_000, 4_400_000])
            self.assertEqual(len(store.existing_ids()), 2)
            store.close()

    def test_simultaneous_listings_of_same_flat_are_kept_apart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ListingStore(Path(directory) / "test.sqlite3")
            description = "Prodej bytu 3+1 Bavlnářská, slunný byt po rekonstrukci se sklepem a lodžií. " * 3
            flat = {
                "source_id": "flat-only",
                "title": "Prodej bytu 3+1 65 m²",
                "disposition": "3+1",
                "location": "Bavlnářská, Frýdek-Místek",
                "area_m2": 65,
                "price_czk": 3_990_000,
                "description": description,
                "image_urls": [],
                "url": "https://example.test/flat-only",
            }
            flat["signature"] = make_signature(flat)
            with_garage = {**flat, "source_id": "with-garage", "price_czk": 4_750_000,
                           "description": "Garáž. " + description}
            with_garage["signature"] = make_signature(with_garage)

            # A buggy earlier run merged both offers into one property.
            property_id = store.save(flat, "2026-01-01T10:00:00+00:00")
            store.db.execute(
                "INSERT INTO source_ids(source_id, property_id) VALUES (?, ?)",
                ("with-garage", property_id),
            )
            store.db.execute(
                """INSERT INTO signatures(source_id, property_id, signature_json)
                   VALUES (?, ?, ?)""",
                ("with-garage", property_id, json.dumps(with_garage["signature"])),
            )
            store.db.execute(
                """INSERT INTO price_history(property_id, observed_at, price_czk)
                   VALUES (?, ?, ?)""",
                (property_id, "2026-01-01T10:00:00+00:00", 4_750_000),
            )

            store.current_ids = {"flat-only", "with-garage"}
            for day in ("2026-01-02", "2026-01-03"):
                first = store.save(flat, f"{day}T10:00:00+00:00")
                second = store.save(with_garage, f"{day}T10:00:00+00:00")
                self.assertNotEqual(first, second)
            for pid, price in ((first, 3_990_000), (second, 4_750_000)):
                history = store.db.execute(
                    "SELECT price_czk FROM price_history WHERE property_id = ?", (pid,)
                ).fetchall()
                self.assertEqual([row[0] for row in history], [price])
            store.close()

    def test_signature_is_completed_from_stored_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            store = ListingStore(path)
            description = "Prostorný byt po kompletní rekonstrukci se dvěma lodžiemi a garáží v domě. " * 2
            detailed = {
                "source_id": "flat",
                "title": "Prodej bytu 3+1 98 m²",
                "disposition": "3+1",
                "location": "J. Opletala, Frýdek-Místek",
                "area_m2": 98,
                "price_czk": 4_450_000,
                "description": description,
                "details_loaded": True,
                "image_urls": ["https://img.test/a.jpeg"],
                "url": "https://example.test/flat",
            }
            detailed["signature"] = make_signature(detailed)
            property_id = store.save(detailed, "2026-01-01T10:00:00+00:00")
            # An older version stored a signature without the description.
            empty = {**detailed["signature"], "description": "", "text_hash": ""}
            store.db.execute(
                "UPDATE signatures SET signature_json = ? WHERE source_id = 'flat'",
                (json.dumps(empty),),
            )
            store.db.commit()
            store.close()

            store = ListingStore(path)
            signature = lambda: json.loads(store.db.execute(
                "SELECT signature_json FROM signatures WHERE source_id = 'flat'"
            ).fetchone()[0])
            self.assertEqual(signature()["description"], make_signature(detailed)["description"])

            # A later run without details must not erase the description again.
            summary_only = {**detailed, "description": "", "details_loaded": False, "image_urls": []}
            summary_only["signature"] = make_signature(summary_only)
            self.assertEqual(store.save(summary_only, "2026-01-02T10:00:00+00:00"), property_id)
            self.assertEqual(signature()["description"], make_signature(detailed)["description"])
            self.assertEqual(signature()["images"], ["/a.jpeg"])
            store.close()

    @staticmethod
    def _page(locale):
        data = {"locale": locale, "props": {}}
        return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'

    def test_non_czech_page_is_retried_and_then_rejected(self):
        with patch("scraper.time.sleep"), patch(
            "scraper.fetch_html", side_effect=[self._page("ru"), self._page("cs")]
        ) as fetch:
            self.assertIn('"cs"', fetch_czech_page("https://www.sreality.cz/x"))
            self.assertEqual(fetch.call_count, 2)
        with patch("scraper.time.sleep"), patch(
            "scraper.fetch_html", return_value=self._page("ru")
        ):
            with self.assertRaises(RuntimeError):
                fetch_czech_page("https://www.sreality.cz/x")

    def test_detail_url_is_built_when_page_has_no_link(self):
        summary = {
            "id": 2347552844,
            "categoryTypeCb": {"name": "Продажа", "value": 1},
            "categoryMainCb": {"name": "Квартиры", "value": 1},
            "categorySubCb": {"name": "3+kk", "value": 6},
            "locality": {"citySeoName": "frydek-mistek", "cityPartSeoName": "frydek"},
        }
        self.assertEqual(
            fallback_detail_url(summary),
            "https://www.sreality.cz/detail/prodej/byt/3+kk/frydek-mistek-frydek/2347552844",
        )
        self.assertEqual(fallback_detail_url({"id": 1}), "")

    def test_missing_url_does_not_overwrite_stored_link(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ListingStore(Path(directory) / "test.sqlite3")
            listing = {
                "source_id": "1",
                "title": "Prodej bytu 2+1 55 m²",
                "disposition": "2+1",
                "location": "Ostravská, Frýdek-Místek",
                "area_m2": 55,
                "price_czk": 3_000_000,
                "description": "",
                "image_urls": [],
                "url": "https://www.sreality.cz/detail/prodej/byt/2+1/x/1",
            }
            listing["signature"] = make_signature(listing)
            property_id = store.save(listing, "2026-01-01T10:00:00+00:00")
            store.save({**listing, "url": ""}, "2026-01-02T10:00:00+00:00")
            data = json.loads(store.db.execute(
                "SELECT data_json FROM properties WHERE property_id = ?", (property_id,)
            ).fetchone()[0])
            self.assertEqual(data["url"], listing["url"])
            store.close()


if __name__ == "__main__":
    unittest.main()
