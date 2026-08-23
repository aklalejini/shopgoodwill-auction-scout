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
    listing_from_search,
    parse_api_time,
    parse_search_response,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "scraper" / "config.json").read_text(encoding="utf-8"))
PROFILE = CONFIG["scoring_profiles"]["minerals-geology"]
MEDIA_PROFILE = CONFIG["scoring_profiles"]["sealed-vintage-media"]
TUBE_PROFILE = CONFIG["scoring_profiles"]["vintage-electron-tubes"]
PEN_PROFILE = CONFIG["scoring_profiles"]["vintage-pens"]


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

    def test_obvious_product_collisions_are_excluded(self):
        collisions = [
            {"title": "Fossil Quartz Crystal Women's Watch", "category": "Women's Watches"},
            {"title": "Crystal Doll with Amethyst Color Dress", "category": "Dolls"},
            {"title": "Mineral Wash Rock & Republic Jeans", "category": "Clothing"},
            {"title": "Sterling Silver Geode Pendant Necklace", "category": "Necklaces"},
            {"title": "Loose Oval & Marquise Cut Gemstones", "category": "Loose Gemstones"},
            {"title": "Waterford Crystal Bowl", "category": "Glass > Fine Crystal"},
        ]
        for listing in collisions:
            with self.subTest(title=listing["title"]):
                self.assertFalse(matches_hunt_domain(listing, PROFILE))

    def test_true_specimens_survive_misleading_categories(self):
        specimens = [
            {
                "title": "4.9 lbs Mixed Minerals Crystals Rocks Geodes Quartz Amethyst Agate Lot",
                "category": "Glass > Fine Crystal",
            },
            {
                "title": "Light Pink Rough Tourmaline Gemstones - 104g Total",
                "category": "Jewelry & Gemstones > Loose Gemstones",
            },
            {
                "title": "Natural Crystal & Mineral Specimen Lot 13pc",
                "category": "Jewelry & Gemstones",
            },
        ]
        for listing in specimens:
            with self.subTest(title=listing["title"]):
                self.assertTrue(matches_hunt_domain(listing, PROFILE))

    def test_ambiguous_terms_need_multiple_signals_and_collection_context(self):
        self.assertFalse(matches_hunt_domain({"title": "Classic Rock Collection"}, PROFILE))
        self.assertTrue(
            matches_hunt_domain(
                {"title": "Mixed Natural Rocks Crystals Collection Lot"}, PROFILE
            )
        )

    def test_sealed_blank_media_hunt_accepts_real_media_lots(self):
        matches = [
            {
                "title": "Lot of 9 Sealed DV Camcorder Tapes TDK & Maxell",
                "category": "Cameras & Camcorders",
            },
            {"title": "Factory Sealed Maxell XLII Blank Cassette Tape Lot"},
            {"title": "New Old Stock Sony Hi8 Video8 Recording Tapes 5 Pack"},
            {"title": "Sealed Case of LTO-5 Data Cartridges"},
            {"title": "TDK MA-R Metal Cassette Tape Lot"},
            {"title": "Sealed Sony 80 Minute Blank MiniDiscs Color Collection"},
            {"title": "Factory Sealed 5.25 Floppy Disk Box"},
            {"title": "Lot of Blank 8-Track Recording Cartridges"},
        ]
        for listing in matches:
            with self.subTest(title=listing["title"]):
                self.assertTrue(matches_hunt_domain(listing, MEDIA_PROFILE))

    def test_sealed_blank_media_hunt_rejects_used_media_and_tape_collisions(self):
        collisions = [
            {"title": "Sealed Disney VHS Movie Tape"},
            {"title": "Lot of Maxell XLII Cassette Tapes"},
            {"title": "Sealed Scotch Heavy Duty Packing Tape"},
            {"title": "New in Box Sony Cassette Player"},
            {"title": "Sealed TDK Head Cleaner Tape"},
            {"title": "Vintage 1989 Jell-O Fairy Tales Cassette Tapes Sealed Promo"},
            {"title": "Sony MZ-R55 Portable MiniDisc Recorder"},
        ]
        for listing in collisions:
            with self.subTest(title=listing["title"]):
                self.assertFalse(matches_hunt_domain(listing, MEDIA_PROFILE))

    def test_sealed_media_scoring_rewards_resale_signals(self):
        promising = {
            "title": "Lot of 9 Sealed DV Camcorder Tapes TDK & Maxell",
            "description": "All nine tapes are individually sealed in original shrink wrap.",
            "price": 9.99,
            "bids": 1,
            "images": ["1", "2", "3", "4"],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        weak = {
            "title": "Opened Used Cassette Tapes",
            "description": "Home recorded and untested.",
            "price": 9.99,
            "bids": 1,
            "images": ["1", "2", "3", "4"],
        }
        high = score_listing(promising, MEDIA_PROFILE)
        low = score_listing(weak, MEDIA_PROFILE)
        self.assertGreaterEqual(high["score"], MEDIA_PROFILE["high_priority_threshold"])
        self.assertTrue(high["high_priority_eligible"])
        self.assertGreater(high["score"], low["score"])
        self.assertIn("dv", high["matched_keywords"])

    def test_common_small_sealed_media_mix_is_not_high_priority(self):
        listing = {
            "title": "Mixed Lot Blank Media Memorex Mini DVD-R Maxell Audio Cassette TDK VHS-C",
            "description": "Three factory sealed common items in original packaging.",
            "price": 19.99,
            "bids": 0,
            "images": ["1", "2", "3", "4", "5"],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        result = score_listing(listing, MEDIA_PROFILE)
        self.assertFalse(result["high_priority_eligible"])

    def test_tube_hunt_accepts_vague_lots_and_factory_signals(self):
        matches = [
            {"title": "Estate Lot of Assorted Vintage Radio Tubes"},
            {"title": "Tube Caddy Full of Untested Electron Tubes"},
            {"title": "Western Electric 300B Matched Pair"},
            {"title": "Baldwin Organ Amplifier Tubes Box Lot"},
        ]
        for listing in matches:
            with self.subTest(title=listing["title"]):
                self.assertTrue(matches_hunt_domain(listing, TUBE_PROFILE))

    def test_tube_hunt_rejects_hardware_and_unrelated_tubes(self):
        collisions = [
            {"title": "Vintage Tube Amplifier Receiver"},
            {"title": "Hickok Vacuum Tube Tester"},
            {"title": "Box of Glass Laboratory Test Tubes"},
            {"title": "LED Fluorescent Replacement Tubes"},
            {"title": "Bicycle Inner Tubes Lot"},
            {"title": "Vintage Tabletop Tube Radio"},
            {"title": "Tri-Chem Liquid Embroidery Paint Tubes Assorted Lot"},
            {"title": "Women's Two Piece Pants and Tube Top"},
            {"title": "Vintage POG Collection with Storage Tubes"},
            {"title": "Lot of Copper Pipe Tubes and PVC Parts"},
            {"title": "Digital Clock in the Style of Old Vacuum Tubes"},
            {"title": "Rock CDs Featuring The Tubes and Van Halen"},
        ]
        for listing in collisions:
            with self.subTest(title=listing["title"]):
                self.assertFalse(matches_hunt_domain(listing, TUBE_PROFILE))

    def test_vague_many_photo_tube_lot_beats_tv_sweep_lot(self):
        promising = {
            "title": "Estate Tube Caddy Lot of Assorted Vintage Radio Tubes",
            "description": "Baldwin, Conn and Fisher rebrands from an old repair shop.",
            "price": 39.99,
            "bids": 0,
            "images": [str(index) for index in range(10)],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        sweep = {
            "title": "Lot of 30 Untested 6DQ6 TV Sweep Tubes",
            "description": "Common television repair stock, sold as is.",
            "price": 39.99,
            "bids": 0,
            "images": [str(index) for index in range(10)],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        high = score_listing(promising, TUBE_PROFILE)
        low = score_listing(sweep, TUBE_PROFILE)
        self.assertGreaterEqual(high["score"], TUBE_PROFILE["high_priority_threshold"])
        self.assertTrue(high["high_priority_eligible"])
        self.assertGreater(high["score"], low["score"])
        self.assertTrue(any("Generic multi-photo" in reason for reason in high["score_reasons"]))

    def test_vintage_pen_hunt_accepts_lots_sets_and_collector_models(self):
        matches = [
            {"title": "Estate Lot of 8 Old Pens"},
            {"title": "Vintage Parker 51 Fountain Pen and Pencil Set"},
            {"title": "Antique Waterman Lever Filler with Warranted 14K Nib"},
            {"title": "Sheaffer Snorkel White Dot Fountain Pen"},
            {"title": "Box of Vintage Fountain Pen Parts and Gold Nibs"},
        ]
        for listing in matches:
            with self.subTest(title=listing["title"]):
                self.assertTrue(matches_hunt_domain(listing, PEN_PROFILE))

    def test_vintage_pen_hunt_rejects_unrelated_and_modern_pen_products(self):
        collisions = [
            {"title": "Digital Insulin Injector Pen New in Box"},
            {"title": "Wacom Digital Stylus Pen Tablet Accessory"},
            {"title": "Lot of Acrylic Paint Pens and Markers"},
            {"title": "Professional Tattoo Pen Machine Kit"},
            {"title": "Original Pen and Ink Drawing Framed Artwork"},
            {"title": "Vintage Pen & Ink Signed Drawing by Local Artist"},
            {"title": "Vintage Framed Pen Ink Lion Head Drawing"},
            {"title": "Crafting Supplies Lot with Colored Pens and Markers"},
            {"title": "Vintage Decorative Lacquered Wood Trinket or Pen Box"},
            {"title": "Vintage Empty Leather Pen Case Only"},
            {"title": "Set of LED Pen Flashlights"},
            {"title": "Apple Pencil Stylus for iPad"},
        ]
        for listing in collisions:
            with self.subTest(title=listing["title"]):
                self.assertFalse(matches_hunt_domain(listing, PEN_PROFILE))

    def test_photo_rich_generic_pen_lot_rewards_hidden_parts_opportunity(self):
        promising = {
            "title": "Estate Lot of 8 Old Pens",
            "description": "Assorted writing instruments from an estate; no nib or imprint closeups.",
            "price": 39.99,
            "bids": 0,
            "images": [str(index) for index in range(12)],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        damaged = {
            "title": "Vintage Fountain Pen Lot Modern Reproduction Steel Nib",
            "description": "Personalized with cracked caps, sprung tines and missing iridium.",
            "price": 39.99,
            "bids": 0,
            "images": [str(index) for index in range(12)],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        high = score_listing(promising, PEN_PROFILE)
        low = score_listing(damaged, PEN_PROFILE)
        self.assertGreaterEqual(high["score"], PEN_PROFILE["high_priority_threshold"])
        self.assertTrue(high["high_priority_eligible"])
        self.assertGreater(high["score"], low["score"])
        self.assertTrue(any("Generic photo-rich pen lot" in reason for reason in high["score_reasons"]))

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
            "buyNowPrice": 39.99,
            "discountedBuyNowPrice": 29.99,
            "endTime": "2026-08-25T12:30:00",
        }
        result = apply_detail(copy.deepcopy(listing), detail)
        self.assertEqual(result["images"], [
            "https://cdn.example/production/7/Item/one.jpg",
            "https://cdn.example/production/7/Item/two.jpg",
        ])
        self.assertEqual(result["description"], "Two\nspecimens")
        self.assertEqual(result["buy_now_price"], 29.99)
        self.assertTrue(result["has_buy_now"])
        self.assertEqual(result["detail_status"], "complete")

    def test_search_listing_records_buy_now_price(self):
        item = {
            "itemId": 42,
            "title": "Buy now specimen",
            "currentPrice": 12.99,
            "buyNowPrice": 24.99,
            "discountedBuyNowPrice": 19.99,
            "numBids": 0,
        }
        hunt = {"id": "minerals-geology", "label": "Minerals & Geology"}
        result = listing_from_search(item, "mineral", "2026-08-23T12:00:00+00:00", hunt)
        self.assertEqual(result["buy_now_price"], 19.99)
        self.assertTrue(result["has_buy_now"])

    def test_naive_api_time_is_marked_as_pacific(self):
        parsed = parse_api_time("2026-08-22T17:09:00")
        self.assertEqual(parsed, "2026-08-22T17:09:00-07:00")


if __name__ == "__main__":
    unittest.main()
