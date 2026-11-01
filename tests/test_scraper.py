import hashlib
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from scraper import (
    ListingStore,
    _image_url,
    download_listing_images,
    estate_search_data,
    identity_match,
    make_signature,
    normalized_listing,
    page_url,
)


class ScraperTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
