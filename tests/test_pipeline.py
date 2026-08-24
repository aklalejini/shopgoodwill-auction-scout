import copy
import json
import re
import unittest
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from scraper.ctbids import (
    CTBIDS_BUYER_API,
    CTBIDS_SELLER_API,
    CTBidsClient,
    apply_ctbids_detail,
    ctbids_listing,
)
from scraper.ocr import (
    _detect_glass_color_signals,
    _extract_signal_hits,
    _parse_tesseract_tsv,
)
from scraper.scoring import score_listing, strip_boilerplate
from scraper.government import (
    govdeals_listing,
    gsa_listing,
    parse_govdeals_detail,
    parse_govdeals_search,
)
from scraper.scrape import (
    apply_hunt_scoring,
    apply_seller_clusters,
    browser_clusters,
    browser_detail_record,
    browser_index_record,
    clean_public_record,
    deduplicate,
    derive_high_priority,
    detail_bucket_id,
    is_expired,
    matches_hunt_domain,
    write_browser_feeds,
)
from scraper.shopgoodwill import (
    DataSourceError,
    apply_detail,
    listing_from_search,
    parse_api_time,
    parse_search_response,
)
from scraper.valuation import estimate_listing, load_valuation_rules


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "scraper" / "config.json").read_text(encoding="utf-8"))
PROFILE = CONFIG["scoring_profiles"]["minerals-geology"]
MEDIA_PROFILE = CONFIG["scoring_profiles"]["sealed-vintage-media"]
TUBE_PROFILE = CONFIG["scoring_profiles"]["vintage-electron-tubes"]
PEN_PROFILE = CONFIG["scoring_profiles"]["vintage-pens"]
PIPE_PROFILE = CONFIG["scoring_profiles"]["estate-tobacco-pipes"]
INSULATOR_PROFILE = CONFIG["scoring_profiles"]["glass-insulators"]
VALUATION_RULES = load_valuation_rules(ROOT / "scraper" / "valuation.csv")


class ScoringTests(unittest.TestCase):
    def test_boilerplate_is_removed_before_scoring(self):
        description = (
            "Sheaffer fountain pen with a 14K nib.\n"
            "Shipping\nItem profile: oversize 20 lb lot. All items are as is and untested."
        )
        cleaned = strip_boilerplate(description)
        self.assertIn("14K nib", cleaned)
        self.assertNotIn("oversize", cleaned)
        result = score_listing({
            "title": "Sheaffer Fountain Pen",
            "description": description,
            "price": 20,
            "images": ["1", "2", "3", "4"],
        }, PEN_PROFILE)
        self.assertFalse(any("oversize" in reason or "20 lb" in reason for reason in result["score_reasons"]))

    def test_non_mineral_hunts_do_not_publish_matched_minerals(self):
        result = score_listing(
            {"title": "Sealed TDK SA-X Blank Cassette", "images": ["1", "2", "3"]},
            MEDIA_PROFILE,
        )
        self.assertIn("matched_keywords", result)
        self.assertNotIn("matched_minerals", result)

    def test_local_surplus_profile_accepts_all_without_priority_claim(self):
        profile = CONFIG["scoring_profiles"]["local-government-surplus"]
        self.assertTrue(matches_hunt_domain({"title": "2008 utility truck"}, profile))
        result = score_listing({"title": "2008 utility truck", "price": 100}, profile)
        self.assertFalse(result["high_priority_eligible"])

    def test_local_surplus_can_flag_photo_rich_high_value_equipment(self):
        profile = CONFIG["scoring_profiles"]["local-government-surplus"]
        result = score_listing({
            "title": "Industrial forklift in working condition",
            "price": 100,
            "images": ["1.jpg", "2.jpg", "3.jpg"],
            "shipping": {"pickup_only": True},
        }, profile)
        self.assertTrue(result["high_priority_eligible"])
        self.assertGreaterEqual(result["score"], profile["high_priority_threshold"])

    def test_keyword_stacking_uses_top_matches_instead_of_clawback_cap(self):
        result = score_listing({
            "title": "NOS Telefunken Mullard Amperex 12AX7 ECC83 EL34 KT88 Tube Lot",
            "description": "Matched pair tested strong old stock collection.",
            "price": 40,
            "images": [str(i) for i in range(8)],
        }, TUBE_PROFILE)
        self.assertTrue(any("Only the top" in reason for reason in result["score_reasons"]))
        self.assertFalse(any("clawback" in reason for reason in result["score_reasons"]))
        self.assertFalse(any("bonuses capped" in reason for reason in result["score_reasons"]))
        self.assertEqual(
            sum(
                int(match.group(1))
                for reason in result["score_reasons"]
                if (match := re.search(r"\(([+-]\d+)\)$", reason))
            ),
            result["score"],
        )

    def test_margin_model_demotes_lightscribe_and_values_known_tube(self):
        media = {
            "title": "Sealed Memorex LightScribe CD-R Spindle",
            "description": "Factory sealed.", "price": 12, "score": 74,
            "primary_hunt": {"id": "sealed-vintage-media"},
            "images": ["1", "2", "3", "4"], "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        tube = {
            "title": "Telefunken ECC83 12AX7 Vacuum Tube",
            "description": "Raised diamond visible in base photo.", "price": 40, "score": 48,
            "primary_hunt": {"id": "vintage-electron-tubes"},
            "images": ["1", "2", "3", "4"], "detail_status": "complete",
            "shipping": {"listed_price": 8, "handling_price": 2, "pickup_only": False},
        }
        media_value = estimate_listing(media, VALUATION_RULES)
        tube_value = estimate_listing(tube, VALUATION_RULES)
        self.assertLess(media_value["estimated_resale_high"], 30)
        self.assertGreater(tube_value["expected_margin"], media_value["expected_margin"])
        self.assertGreater(tube_value["max_bid"], media_value["max_bid"])

    def test_shipping_estimate_is_weight_sensitive_in_dollars(self):
        light = {
            "title": "Parker 51 Fountain Pen", "description": "Approximate weight: 1 lb",
            "price": 20, "score": 50, "primary_hunt": {"id": "vintage-pens"},
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        heavy = {**light, "description": "Approximate weight: 20 lbs"}
        self.assertGreater(
            estimate_listing(heavy, VALUATION_RULES)["estimated_shipping"],
            estimate_listing(light, VALUATION_RULES)["estimated_shipping"] + 40,
        )

    def test_seller_clusters_flag_close_auctions(self):
        items = [
            {"item_id": "1", "seller_id": 9, "end_time": "2026-08-24T12:00:00+00:00", "estimated_shipping": 20},
            {"item_id": "2", "seller_id": 9, "end_time": "2026-08-25T12:00:00+00:00", "estimated_shipping": 25},
            {"item_id": "3", "seller_id": 10, "end_time": "2026-08-25T12:00:00+00:00", "estimated_shipping": 10},
        ]
        apply_seller_clusters(items, 72)
        self.assertEqual(items[0]["seller_cluster"]["count"], 2)
        self.assertNotIn("potential_shipping_savings", items[0]["seller_cluster"])
        self.assertNotIn("seller_cluster", items[2])

    def test_seller_clusters_do_not_chain_beyond_window(self):
        items = [
            {
                "item_id": str(index),
                "seller_id": 9,
                "end_time": end_time,
                "estimated_shipping": 15,
                "shipping": {"policy": "Combined shipping temporarily unavailable"},
            }
            for index, end_time in enumerate(
                (
                    "2026-08-01T12:00:00-05:00",
                    "2026-08-03T12:00:00-05:00",
                    "2026-08-05T12:00:00-05:00",
                )
            )
        ]
        apply_seller_clusters(items, 72)
        self.assertEqual(items[0]["seller_cluster"]["count"], 2)
        self.assertNotIn("seller_cluster", items[2])
        self.assertNotIn("potential_shipping_savings", items[0]["seller_cluster"])
        self.assertTrue(items[0]["seller_cluster"]["combined_shipping_unavailable"])

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
        self.assertIn("wulfenite", high["matched_keywords"])
        self.assertTrue(any("Red Cloud Mine" in reason for reason in high["score_reasons"]))

    def test_high_priority_is_derived_from_canonical_active_records(self):
        active = [
            {"item_id": "low", "score": 20, "high_priority": False},
            {"item_id": "high", "score": 50, "high_priority": True, "evidence": ["x"]},
        ]
        high = derive_high_priority(active, 100)
        self.assertEqual(high, [active[1]])
        self.assertIs(high[0], active[1])

    def test_public_cleanup_removes_internal_fields_recursively(self):
        item = {
            "item_id": "1",
            "ocr_text": "raw guess",
            "matched_minerals": ["fluorite"],
            "fee_rate": 0.15,
            "seller_cluster": {"potential_shipping_savings": 18.0, "count": 2},
            "hunt_scores": [{"target_multiple": 2.5, "score": 40}],
            "ocr_hits": ["TELEFUNKEN"],
        }
        clean_public_record(item)
        self.assertEqual(item["ocr_hits"], ["TELEFUNKEN"])
        self.assertEqual(item["seller_cluster"], {"count": 2})
        self.assertEqual(item["hunt_scores"], [{"score": 40}])
        for field in ("ocr_text", "matched_minerals", "fee_rate"):
            self.assertNotIn(field, item)

    def test_browser_index_is_compact_and_detail_is_lazy(self):
        item = {
            "item_id": "ctbids-123",
            "title": "Vintage pen lot",
            "score": 42,
            "score_reasons": [f"Reason {index} (+1)" for index in range(8)],
            "images": [f"full-{index}.jpg" for index in range(7)],
            "thumbnails": [f"thumb-{index}.jpg" for index in range(7)],
            "description": "Useful item copy.\nShipping\nVery long policy text.",
            "listing_url": "https://example.test/item/123",
            "shipping": {
                "listed_price": 8.0, "handling_price": 2.0,
                "pickup_only": False, "carrier": "Ground",
                "policy": "Very long policy text",
            },
            "visual_hits": ["amber glass color"],
            "seller_cluster": {
                "id": "seller:1", "count": 3,
                "item_ids": ["1", "2", "3"],
                "combined_shipping_unavailable": False,
                "close_window_hours": 72,
            },
            "hunt_scores": [{"id": "vintage-pens", "score": 42}],
        }
        index = browser_index_record(item)
        detail = browser_detail_record(item)
        self.assertNotIn("description", index)
        self.assertNotIn("images", index)
        self.assertNotIn("policy", index["shipping"])
        self.assertNotIn("item_ids", index["seller_cluster"])
        self.assertEqual(index["photo_count"], 7)
        self.assertEqual(len(index["thumbnails"]), 5)
        self.assertEqual(len(index["score_reasons"]), 5)
        self.assertEqual(index["evidence_types"], ["visual"])
        self.assertEqual(detail["description"], "Useful item copy.")
        self.assertEqual(len(detail["images"]), 7)
        self.assertEqual(len(detail["score_reasons"]), 8)
        self.assertEqual(index["detail_bucket"], detail_bucket_id("ctbids-123"))

    def test_browser_clusters_store_membership_once(self):
        cluster = {
            "id": "9:1", "count": 2, "item_ids": ["1", "2"],
            "combined_shipping_unavailable": True, "close_window_hours": 72,
        }
        items = [
            {"item_id": "1", "seller_id": 9, "seller": "Seller", "seller_cluster": cluster},
            {"item_id": "2", "seller_id": 9, "seller": "Seller", "seller_cluster": cluster},
        ]
        clusters = browser_clusters(items)
        self.assertEqual(list(clusters), ["9:1"])
        self.assertEqual(clusters["9:1"]["item_ids"], ["1", "2"])

    def test_browser_feed_preserves_legacy_url_as_a_small_pointer(self):
        item = {
            "item_id": "1", "title": "Listing", "score": 40,
            "high_priority": True, "images": ["one.jpg"],
            "listing_url": "https://example.test/1",
        }
        with TemporaryDirectory() as directory:
            output = Path(directory)
            write_browser_feeds(output, [item], [item], [], {"active_count": 1})
            manifest = json.loads((output / "listings.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["index"], "index.json")
            self.assertEqual(len(list((output / "details").glob("*.json"))), 64)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            high = json.loads((output / "high_priority.json").read_text(encoding="utf-8"))
            self.assertEqual(high, index)

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
        self.assertGreaterEqual(result["score"], PROFILE["undervalued_threshold"])
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
        self.assertGreaterEqual(result["score"], PROFILE["undervalued_threshold"])
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

    def test_small_lightscribe_dvd_pack_is_not_high_potential(self):
        profile = CONFIG["scoring_profiles"]["sealed-vintage-media"]
        result = score_listing({
            "title": "Memorex 10-Pack DVD+R LightScribe Recordable Discs Sealed",
            "price": 25,
            "images": ["1.jpg", "2.jpg", "3.jpg"],
            "shipping": {"listed_price": 8},
        }, profile)
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

    def test_tobacco_pipe_hunt_accepts_estate_lots_racks_and_collectible_makers(self):
        matches = [
            {"title": "Estate Lot of Assorted Vintage Smoking Pipes"},
            {"title": "Wooden Pipe Rack with 6 Briar Tobacco Pipes"},
            {"title": "Dunhill Briar Smoking Pipe Made in England"},
            {"title": "Antique Block Meerschaum Pipe in Fitted Case"},
            {"title": "Peterson Estate Pipe Made in Ireland"},
        ]
        for listing in matches:
            with self.subTest(title=listing["title"]):
                self.assertTrue(matches_hunt_domain(listing, PIPE_PROFILE))

    def test_tobacco_pipe_hunt_rejects_plumbing_drug_and_accessory_collisions(self):
        collisions = [
            {"title": "Lot of Vintage Copper Plumbing Pipes and Fittings"},
            {"title": "Heavy Duty Steel Pipe Wrench Tool"},
            {"title": "Hand Blown Glass Water Bong Smoking Pipe"},
            {"title": "Scottish Bagpipes with Carrying Case"},
            {"title": "Vintage Pipe Tobacco Tins Collection"},
            {"title": "Box of Smoking Pipe Cleaners and Filters"},
            {"title": "Vintage Empty Pipe Rack Only"},
            {"title": "Automotive Exhaust Pipe and Muffler"},
            {"title": "Antique Chinese Opium Pipe"},
            {"title": "Vintage Avon Smoking Pipe Aftershave Decanter Bottle"},
            {"title": "Laptop Cooling Fans and Copper Heat Pipes Lot"},
            {"title": "Signed Elder Smoking Pipe Oil on Canvas Painting"},
            {"title": "Vintage Amber Tobacco Pipe Stem Replacement Part"},
        ]
        for listing in collisions:
            with self.subTest(title=listing["title"]):
                self.assertFalse(matches_hunt_domain(listing, PIPE_PROFILE))

    def test_photo_rich_estate_pipe_lot_beats_fatally_damaged_low_grade_pipes(self):
        promising = {
            "title": "Estate Lot of 12 Vintage Smoking Pipes",
            "description": "Assorted unmarked briar pipes with oxidized stems that need cleaning; rack included.",
            "price": 39.99,
            "bids": 0,
            "images": [str(index) for index in range(12)],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        damaged = {
            "title": "Lot of Dr. Grabow Medico Smoking Pipes",
            "description": "Cracked bowls, burnout, broken stems, bite-through and mold. For parts only.",
            "price": 39.99,
            "bids": 0,
            "images": [str(index) for index in range(12)],
            "detail_status": "complete",
            "shipping": {"listed_price": 0, "handling_price": 2, "pickup_only": False},
        }
        high = score_listing(promising, PIPE_PROFILE)
        low = score_listing(damaged, PIPE_PROFILE)
        self.assertGreaterEqual(high["score"], PIPE_PROFILE["high_priority_threshold"])
        self.assertTrue(high["high_priority_eligible"])
        self.assertGreater(high["score"], low["score"])
        self.assertTrue(any("Generic estate pipe lot" in reason for reason in high["score_reasons"]))

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


class GlassInsulatorTests(unittest.TestCase):
    def test_relevance_accepts_naive_glass_language_and_rejects_non_glass_hardware(self):
        self.assertTrue(matches_hunt_domain(
            {"title": "Old Telegraph Glass Bell Estate Lot"}, INSULATOR_PROFILE
        ))
        self.assertTrue(matches_hunt_domain(
            {"title": "Antique Blue Glass Telephone Pole Thing"}, INSULATOR_PROFILE
        ))
        for title in (
            "Porcelain Insulator Lot", "Ceramic Electric Fence Insulators",
            "Fiberglass Building Insulation", "Rubber Wire Insulator Sleeves",
            "Electric Glass Cold Brew Coffee Maker and Carafe",
        ):
            self.assertFalse(matches_hunt_domain({"title": title}, INSULATOR_PROFILE), title)

    def test_rare_color_maker_and_shape_can_be_high_potential(self):
        result = score_listing({
            "title": "Barn Find Cobalt Blue Cal Elec Glass Insulator Lot",
            "description": "Threadless profile, as found.",
            "price": 12, "bids": 0,
            "images": [f"{index}.jpg" for index in range(8)],
            "shipping": {"listed_price": 0.01, "pickup_only": False},
        }, INSULATOR_PROFILE)
        self.assertTrue(result["high_priority_eligible"])
        self.assertGreaterEqual(result["score"], INSULATOR_PROFILE["high_priority_threshold"])

    def test_common_researched_piece_and_altered_damage_are_demoted(self):
        common = score_listing({
            "title": "Hemingray-42 Clear Glass Insulator CD 154 Collectible",
            "price": 10, "bids": 0, "images": [f"{index}.jpg" for index in range(8)],
        }, INSULATOR_PROFILE)
        altered = score_listing({
            "title": "Deep Purple Hemingray Glass Insulator",
            "description": "Irradiated reproduction with chipped base.",
            "price": 10, "bids": 0, "images": [f"{index}.jpg" for index in range(8)],
        }, INSULATOR_PROFILE)
        self.assertFalse(common["high_priority_eligible"])
        self.assertFalse(altered["high_priority_eligible"])
        self.assertLess(common["score"], INSULATOR_PROFILE["high_priority_threshold"])
        self.assertLess(altered["score"], INSULATOR_PROFILE["high_priority_threshold"])

    def test_common_teal_lot_with_disclosed_damage_is_not_high_potential(self):
        result = score_listing({
            "title": "Hemingray Glass Insulators 3 Piece Lot Aqua Teal Clear Vintage",
            "description": "Embossed pieces with minor chipping, roughness, and base fleabites.",
            "price": 10, "bids": 0,
            "images": [f"{index}.jpg" for index in range(8)],
        }, INSULATOR_PROFILE)
        self.assertLess(result["score"], INSULATOR_PROFILE["high_priority_threshold"])

    def test_image_color_clue_is_conservative(self):
        blue = Image.new("RGB", (160, 160), (35, 95, 220))
        blue_bytes = BytesIO()
        blue.save(blue_bytes, format="PNG")
        self.assertEqual(
            _detect_glass_color_signals(blue_bytes.getvalue()),
            ["strong blue glass color"],
        )
        neutral = Image.new("RGB", (160, 160), (210, 210, 210))
        neutral_bytes = BytesIO()
        neutral.save(neutral_bytes, format="PNG")
        self.assertEqual(_detect_glass_color_signals(neutral_bytes.getvalue()), [])

    def test_ocr_keeps_only_confident_recognized_collector_signals(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
        rows = [
            "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t22\tTELEFUNKEN",
            "5\t1\t1\t1\t2\t1\t0\t0\t1\t1\t94\tMULLARD",
            "5\t1\t1\t1\t2\t2\t0\t0\t1\t1\t91\t6L6GC",
            "5\t1\t1\t1\t2\t3\t0\t0\t1\t1\t99\t1F2",
        ]
        confident_text = _parse_tesseract_tsv("\n".join([header, *rows]), 65)
        hits = _extract_signal_hits(confident_text)
        self.assertEqual(hits, ["6L6GC", "MULLARD"])
        self.assertNotIn("1F2", hits)
        self.assertNotIn("TELEFUNKEN", hits)

    def test_visual_color_clue_is_labeled_as_image_analysis(self):
        result = score_listing({
            "title": "Vintage Glass Insulator",
            "visual_text": "strong blue glass color",
            "price": 8, "bids": 0, "images": [f"{index}.jpg" for index in range(6)],
        }, INSULATOR_PROFILE)
        self.assertTrue(any("image color analysis" in reason for reason in result["score_reasons"]))


class GovernmentSourceTests(unittest.TestCase):
    def test_govdeals_search_cards_are_namespaced_and_time_zoned(self):
        document = """
        <h1>2 Results</h1>
        <div id="asset-292-1462">
          <a class="link-click" title="Gym Tire Flip" href="/en/asset/1462/292">Gym Tire</a>
          <p name="pAssetLocation" title="Memphis, Tennessee, USA"></p>
          <p name="pAssetCurrentBid" title="77"></p>
          <app-ux-timer>(August 25, 2026 01:00 AM UTC)</app-ux-timer>
        </div>
        """
        items, total = parse_govdeals_search(document)
        self.assertEqual(total, 2)
        self.assertEqual(items[0]["end_time"], "2026-08-25T01:00:00+00:00")
        hunt = {"id": "local-government-surplus", "label": "Local Government Surplus"}
        listing = govdeals_listing(items[0], "2026-08-23T12:00:00+00:00", hunt, "38635", 50)
        self.assertEqual(listing["item_id"], "govdeals-292-1462")
        self.assertEqual(listing["source"], "govdeals")

    def test_govdeals_detail_extracts_images_seller_and_bids(self):
        document = """
        <meta property="og:image" content="https://webassets.lqdt1.com/assets/photos/292/a.jpg?cb=1&amp;w=1200">
        <div id="onlineAuctionBidBox">
          <h1 class="product-title">Gym Tire Flip</h1>
          <div id="currentBid" title="77"><a id="bid_count_link" title="Bids 8">8 Bids</a></div>
          <app-ux-timer>(Aug 25, 2026 01:00 AM UTC)</app-ux-timer>
        </div>
        <div class="description-table"><div class="long-description"><p>Heavy duty exercise equipment.</p></div></div>
        <div id="seller_information">
          <div class="row description-body"><div class="col-6"><h5>Seller:</h5></div><div class="col-6"><p>University of Memphis, TN</p><p>other assets</p></div></div>
          <div class="row description-body"><div class="col-6"><h5>Item Location:</h5></div><div class="col-6"><p>Memphis, TN 38111</p></div></div>
        </div>
        """
        result = parse_govdeals_detail(document, "1462", "292")
        self.assertEqual(result["bids"], 8)
        self.assertEqual(result["seller"], "University of Memphis, TN")
        self.assertEqual(result["images"], ["https://webassets.lqdt1.com/assets/photos/292/a.jpg?cb=1"])

    def test_gsa_listing_uses_distinct_source_id(self):
        hunt = {"id": "local-government-surplus", "label": "Local Government Surplus"}
        listing = gsa_listing({
            "auctionId": 374130, "lotName": "Lab equipment", "currentBid": 25,
            "endDate": "2026-08-25T12:00:00Z", "location": {"city": "Memphis", "state": "TN"},
        }, "2026-08-23T12:00:00+00:00", hunt, "38635", 50)
        self.assertEqual(listing["item_id"], "gsa-374130")
        self.assertEqual(listing["source"], "gsa-auctions")


class CTBidsSourceTests(unittest.TestCase):
    def test_client_uses_production_ctbids_services(self):
        self.assertEqual(CTBIDS_SELLER_API, "https://seller.ctbids.com/services")
        self.assertEqual(CTBIDS_BUYER_API, "https://api.ctbids.com/services")

    def test_nationwide_search_requires_ctbids_shippable_flag(self):
        body = CTBidsClient._search_body(
            None, None, 250, shippable_only=True
        )
        filters = {row["field"]: row["value"] for row in body["filter"]}
        self.assertEqual(filters["isshippable"], "1")
        self.assertNotIn("zipcode", filters)
        self.assertNotIn("miles", filters)

    def test_nationwide_search_keeps_paging_when_later_pages_omit_total(self):
        payloads = [
            {"status": "success", "data": [{"id": 1, "isshippable": 1}],
             "page": {"total": 3, "keyset": {"next": "one"}}},
            {"status": "success", "data": [{"id": 2, "isshippable": 1}],
             "page": {"keyset": {"next": "two"}}},
            {"status": "success", "data": [{"id": 3, "isshippable": 1}],
             "page": {"keyset": {}}},
        ]

        class Reply:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        client = CTBidsClient({
            "page_size": 1, "nationwide_maximum_pages": 3,
            "request_delay_seconds": 0,
        })
        client._request = lambda *args, **kwargs: Reply(payloads.pop(0))
        client._add_current_bids = lambda items: None
        items, total = client.search(shippable_only=True)
        self.assertEqual([item["id"] for item in items], [1, 2, 3])
        self.assertEqual(total, 3)

    def test_search_item_is_namespaced_and_keeps_buy_now(self):
        hunt = {"id": "local-estate-auctions", "label": "Local Estate Auctions"}
        listing = ctbids_listing({
            "id": 1079801, "saleid": 6406, "title": "Vintage Camera Collection",
            "itemseourl": "Vintage-Camera-Collection", "startingprice": 1,
            "buynowprice": 75, "isshippable": 1, "city": "Memphis",
            "state": "Tennessee", "zipcode": "38103", "locationid": 12,
            "locationtitle": "Caring Transitions Memphis",
            "itemclosetime": "2026-08-25 20:15:00",
            "current_bid": {"bidprice": 18, "bidcount": 3},
            "displayimageurl": "https://images.example/item.webp",
            "categoryGroup": "Cameras & Photo Equipment", "category": "Film",
        }, "2026-08-23T12:00:00+00:00", hunt, "38635", 50)
        self.assertEqual(listing["item_id"], "ctbids-6406-1079801")
        self.assertEqual(listing["source"], "ctbids")
        self.assertEqual(listing["seller_id"], "ctbids-12")
        self.assertEqual(listing["price"], 18)
        self.assertEqual(listing["buy_now_price"], 75)
        self.assertTrue(listing["has_buy_now"])
        self.assertEqual(listing["end_time"], "2026-08-25T20:15:00+00:00")
        self.assertEqual(listing["discovered_by"], ["CTBids within 50 mi of 38635"])

    def test_nationwide_listing_is_marked_shippable_without_local_scope(self):
        hunt = {"id": "local-estate-auctions", "label": "CTBids Estate Auctions"}
        listing = ctbids_listing({
            "id": 1079801, "saleid": 6406, "title": "Shippable Camera",
            "startingprice": 1, "isshippable": 1, "city": "Buffalo",
            "state": "New York", "zipcode": "14202",
        }, "2026-08-23T12:00:00+00:00", hunt, "38635", 50, scope="shippable")
        self.assertFalse(listing["shipping"]["pickup_only"])
        self.assertEqual(listing["discovered_by"], ["CTBids shippable nationwide"])
        self.assertNotIn("local_search", listing)

    def test_uat_images_and_wrong_item_detail_rows_are_rejected(self):
        hunt = {"id": "local-estate-auctions", "label": "CTBids Estate Auctions"}
        listing = ctbids_listing({
            "id": 123, "saleid": 45, "title": "Production item",
            "isshippable": 1,
            "displayimageurl": "https://imageuat.ctbids.com/seller/wrong.webp",
        }, "2026-08-23T12:00:00+00:00", hunt, "38635", 50, scope="shippable")
        self.assertEqual(listing["images"], [])
        listing["images"] = ["https://image.ctbids.com/seller/123_search.webp"]
        listing["thumbnails"] = ["https://image.ctbids.com/seller/123_thumb.webp"]
        result = apply_ctbids_detail(listing, {
            "item": {"title": "Production item", "isshippable": 1},
            "images": [{
                "itemid": 999,
                "url": "https://image.ctbids.com/seller/999_wrong.webp",
            }],
        })
        self.assertEqual(result["images"], ["https://image.ctbids.com/seller/123_search.webp"])

    def test_detail_adds_all_photos_description_and_delivery(self):
        listing = {
            "title": "Camera lot", "price": 1, "images": [], "thumbnails": [],
            "shipping": {}, "category": "Estate auction", "seller_id": "7",
        }
        result = apply_ctbids_detail(listing, {
            "item": {
                "title": "Leica Camera Lot", "description": "<p>Estate collection.</p>",
                "startingprice": 1, "buynowprice": 125, "isshippable": 0,
                "itemreceiptmethod": '{"pickup": true, "shipping": true}',
                "locationid": 22, "locationtitle": "Memphis", "city": "Memphis",
                "state": "Tennessee", "zipcode": "38103", "categoryGroup": "Cameras",
                "category": "Film", "condition": "Used",
                "itemclosetime": "2026-08-25 20:15:00",
            },
            "bid": {"bidprice": 0, "bidcount": 0},
            "images": [
                {"url": "https://images.example/one.webp", "thumbnailurl": "https://images.example/t1.webp"},
                {"url": "https://images.example/two.webp", "thumbnailurl": "https://images.example/t2.webp"},
            ],
        })
        self.assertEqual(result["description"], "Estate collection.")
        self.assertEqual(len(result["images"]), 2)
        self.assertFalse(result["shipping"]["pickup_only"])
        self.assertEqual(result["buy_now_price"], 125)
        self.assertEqual(result["detail_status"], "complete")


if __name__ == "__main__":
    unittest.main()
