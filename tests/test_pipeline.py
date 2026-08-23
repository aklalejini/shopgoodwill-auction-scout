import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scraper.scoring import score_listing
from scraper.scrape import apply_hunt_scoring, deduplicate, is_expired, matches_hunt_domain
from scraper.shopgoodwill import (
    DataSourceError,
    apply_detail,
    parse_api_time,
    parse_search_response,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "scraper" / "config.json").read_text(encoding="utf-8"))
PROFILE = CONFIG["scoring_profiles"]["minerals-geology"]


class ScoringTests(unittest.TestCase):
    def test_collectible_collection_scores_above_retail_product(self):
        promising = {
            "title": "Old Estate Collection with Wulfenite from Red Cloud Mine",
            "description": "Unknown mixed mineral specimens",
            "price": 24,
            "bids": 0,
            "images": [str(index) for index in range(8)],
        }
        retail = {
            "title": "Decorative Chakra Healing Crystal Tree",
            "description": "Aura coated polished tower",
            "price": 24,
            "bids": 0,
            "images": ["one"],
        }
        high = score_listing(promising, PROFILE)
        low = score_listing(retail, PROFILE)
        self.assertGreater(high["score"], low["score"])
        self.assertIn("wulfenite", high["matched_minerals"])
        self.assertTrue(any("Red Cloud Mine" in reason for reason in high["score_reasons"]))

    def test_word_boundary_prevents_lot_inside_pilot(self):
        result = score_listing(
            {"title": "Pilot geology book", "price": 500, "bids": 10, "images": []},
            PROFILE,
        )
        self.assertFalse(any("'lot'" in reason for reason in result["score_reasons"]))

    def test_rockwell_false_positive_does_not_receive_collection_bonus(self):
        result = score_listing(
            {
                "title": "Vintage Norman Rockwell Collector Plate Collection Lot",
                "description": "",
                "category": "Collectibles > Plates",
                "price": 12,
                "bids": 0,
                "images": ["1", "2", "3", "4"],
            },
            PROFILE,
        )
        self.assertFalse(any("Opportunity term" in reason for reason in result["score_reasons"]))
        self.assertLess(result["score"], PROFILE["high_priority_threshold"])

    def test_hunt_scoring_records_category_metadata(self):
        item = {
            "title": "Estate mineral collection with fluorite",
            "description": "",
            "price": 20,
            "bids": 0,
            "images": ["1", "2", "3", "4"],
            "hunt_categories": ["minerals-geology"],
            "hunt_labels": ["Minerals & Geology"],
        }
        apply_hunt_scoring(item, CONFIG["hunts"], CONFIG["scoring_profiles"])
        self.assertEqual(item["primary_hunt"]["id"], "minerals-geology")
        self.assertEqual(item["hunt_scores"][0]["label"], "Minerals & Geology")
        self.assertIn("fluorite", item["matched_keywords"])

    def test_generic_bulk_language_cannot_stack_into_high_priority(self):
        listing = {
            "title": "Assorted Mixed Mineral Geology Rock Specimen Collection Lot",
            "description": "Four pounds of polished stones and decorative rocks.",
            "price": 20,
            "bids": 0,
            "images": ["1", "2", "3", "4"],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        result = score_listing(listing, PROFILE)
        self.assertLess(result["score"], PROFILE["high_priority_threshold"])
        self.assertTrue(any("keyword stacking" in reason for reason in result["score_reasons"]))

    def test_heavy_collection_with_penny_shipping_can_be_high_priority(self):
        listing = {
            "title": "Estate Mineral Collection 5 lbs",
            "description": "Raw geological specimens with a collector label.",
            "price": 50,
            "bids": 0,
            "images": [str(index) for index in range(8)],
            "detail_status": "complete",
            "shipping": {"listed_price": 0.01, "handling_price": 0, "pickup_only": False},
        }
        result = score_listing(listing, PROFILE)
        self.assertGreaterEqual(result["score"], PROFILE["high_priority_threshold"])
        self.assertTrue(result["high_priority_eligible"])
        self.assertTrue(any("favorable flat shipping" in reason for reason in result["score_reasons"]))

    def test_one_photo_listing_fails_high_priority_quality_gate(self):
        result = score_listing(
            {
                "title": "Estate Wulfenite from Red Cloud Mine",
                "price": 10,
                "bids": 0,
                "images": ["only"],
                "detail_status": "complete",
                "shipping": {"listed_price": 0.01, "pickup_only": False},
            },
            PROFILE,
        )
        self.assertFalse(result["high_priority_eligible"])
        self.assertTrue(any("Only 1 listing photo" in reason for reason in result["score_reasons"]))

    def test_proven_seller_specimen_lot_can_be_high_priority(self):
        result = score_listing(
            {
                "seller_id": 192,
                "title": "4 lb. Lot Assorted Various Minerals/Crystals/Gems/Rocks",
                "description": "Grab-bag style auction with vintage mineral specimens.",
                "price": 35.56,
                "bids": 6,
                "images": [str(index) for index in range(7)],
                "detail_status": "complete",
                "shipping": {
                    "listed_price": 0,
                    "handling_price": 2,
                    "pickup_only": False,
                },
            },
            PROFILE,
        )
        self.assertGreaterEqual(result["score"], PROFILE["high_priority_threshold"])
        self.assertTrue(result["high_priority_eligible"])
        self.assertTrue(any("Proven source" in reason for reason in result["score_reasons"]))

    def test_calculated_shipping_is_not_mistaken_for_free_shipping(self):
        result = score_listing(
            {
                "title": "Mineral collection 6 lbs",
                "price": 20,
                "bids": 0,
                "images": ["1", "2", "3", "4"],
                "detail_status": "complete",
                "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
            },
            PROFILE,
        )
        self.assertTrue(any("calculated and not included" in reason for reason in result["score_reasons"]))
        self.assertTrue(any("unresolved shipping cost" in reason for reason in result["score_reasons"]))


class PipelineTests(unittest.TestCase):
    def test_seller_sweep_filters_to_hunt_domain(self):
        mineral = {"title": "4 lb assorted minerals and rocks"}
        clothing = {"title": "Four assorted men's shirts"}
        self.assertTrue(matches_hunt_domain(mineral, PROFILE))
        self.assertFalse(matches_hunt_domain(clothing, PROFILE))

    def test_deduplication_unions_search_terms(self):
        records = [
            {"item_id": "42", "title": "Rock lot", "discovered_by": ["rock"]},
            {"item_id": "42", "title": "Rock lot", "discovered_by": ["mineral", "rock"]},
        ]
        result = deduplicate(records)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["discovered_by"], ["mineral", "rock"])

    def test_expiration_uses_offset_aware_time(self):
        now = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(is_expired({"end_time": "2026-08-22T16:00:00-07:00"}, now))
        self.assertFalse(is_expired({"end_time": "2026-08-22T18:00:00-07:00"}, now))

    def test_malformed_search_response_fails_cleanly(self):
        with self.assertRaises(DataSourceError):
            parse_search_response({"unexpected": []})
        with self.assertRaises(DataSourceError):
            parse_search_response({"searchResults": {"items": "not a list"}})

    def test_detail_builds_all_full_size_urls(self):
        listing = {
            "item_id": "42", "title": "A", "price": 1, "bids": 0,
            "images": [], "thumbnails": [], "shipping": {},
        }
        detail = {
            "itemId": 42,
            "title": "Mineral lot",
            "imageServer": "https://cdn.example/production/",
            "imageUrlString": "7\\Item\\one.jpg;7\\Item\\two.jpg",
            "thumbnailUrlString": "7\\Item\\one-t.jpg;7\\Item\\two-t.jpg",
            "description": "<p>Two <strong>specimens</strong></p>",
            "endTime": "2026-08-25T12:30:00",
        }
        result = apply_detail(copy.deepcopy(listing), detail)
        self.assertEqual(result["images"], [
            "https://cdn.example/production/7/Item/one.jpg",
            "https://cdn.example/production/7/Item/two.jpg",
        ])
        self.assertEqual(result["description"], "Two\nspecimens")
        self.assertEqual(result["detail_status"], "complete")

    def test_naive_api_time_is_marked_as_pacific(self):
        parsed = parse_api_time("2026-08-22T17:09:00")
        self.assertEqual(parsed, "2026-08-22T17:09:00-07:00")


if __name__ == "__main__":
    unittest.main()
