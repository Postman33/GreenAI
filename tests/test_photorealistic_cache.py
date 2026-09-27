from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from scripts import render_photorealistic_gallery as photos
from src.visualization.openrouter_images import CONTENT_HASH_VERSION, content_hash, legacy_content_hash


class PhotoCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.place = self.root / "place_01"
        self.renders = self.place / "renders"
        self.renders.mkdir(parents=True)
        for name in ("overview_before", "overview_after", "overview_plant_mask"):
            self.image(self.renders / (name + ".png"))
        self.scene = {
            "plant_mask_legend": [{"plant_type": "tree", "species": "Липа мелколистная", "color": "#00BFFF"}],
            "proposed_trees": [{"species": "Липа мелколистная"}],
        }
        (self.place / "scene.json").write_text(json.dumps(self.scene), encoding="utf-8")
        self.index = self.root / "gallery_index.json"
        self.index.write_text(json.dumps({"places": [{"id": "place_01"}]}), encoding="utf-8")
        self.photo_dir = self.place / "photorealistic"
        self.report_path = self.photo_dir / "generation_report.json"
        self.requests = []

    def image(self, path: Path, color: str = "green", date: str = "first render") -> None:
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Date", date)
        metadata.add_text("RenderTime", date)
        Image.new("RGB", (3, 2), color).save(path, pnginfo=metadata)

    def generate(self, **kwargs):
        self.requests.append(kwargs)
        output = kwargs["output_stem"].with_suffix(".png")
        self.image(output, color="blue" if len(self.requests) % 2 else "red")
        return output, 0.01

    def run_photos(self, budget: str = "0.10") -> int:
        with patch("sys.argv", ["photos", "--gallery-index", str(self.index), "--max-cost-usd", budget]), \
                patch.object(photos, "load_api_key", return_value="test-placeholder"), \
                patch.object(photos, "generate_image", side_effect=self.generate):
            return photos.main()

    def report(self) -> dict:
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def test_rerender_with_new_metadata_reuses_both_photos(self) -> None:
        self.assertEqual(self.run_photos(), 0)
        for path in self.renders.glob("*.png"):
            self.image(path, date="second render")
        self.assertEqual(self.run_photos(), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.report()["current_images"]), 2)

    def test_changed_pixels_keep_old_files_and_generate_new_version(self) -> None:
        self.run_photos()
        originals = {i["file"]: (self.photo_dir / i["file"]).read_bytes() for i in self.report()["images"]}
        self.image(self.renders / "overview_after.png", color="yellow")
        self.assertEqual(self.run_photos(), 0)
        report = self.report()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(report["images"]), 3)
        self.assertAlmostEqual(sum(i["cost_usd"] for i in report["images"]), 0.03)
        self.assertNotIn(report["current_images"]["overview_after_paired_v4"], originals)
        for name, content in originals.items():
            self.assertEqual((self.photo_dir / name).read_bytes(), content)
        # Returning to an earlier scene reuses its previous valid version.
        self.image(self.renders / "overview_after.png")
        self.assertEqual(self.run_photos(), 0)
        self.assertEqual(len(self.requests), 3)

    def test_missing_cached_file_gets_new_version(self) -> None:
        self.run_photos()
        missing = self.report()["current_images"]["overview_after_paired_v4"]
        (self.photo_dir / missing).unlink()
        self.assertEqual(self.run_photos(), 0)
        self.assertEqual(len(self.requests), 3)
        self.assertNotEqual(self.report()["current_images"]["overview_after_paired_v4"], missing)

    def test_exhausted_budget_preserves_ledger_and_makes_no_requests(self) -> None:
        self.run_photos()
        report = self.report()
        for item in report["images"]:
            item["cost_usd"] = 0.045
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        self.image(self.renders / "overview_before.png", color="yellow")
        self.assertEqual(self.run_photos(), 2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report()["images"], report["images"])
        self.assertEqual(self.report()["current_images"], {})
        status = json.loads((self.root / "photorealistic_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "budget_limit")
        self.assertAlmostEqual(status["spent_usd"], 0.09)

    def test_exact_legacy_cache_is_migrated_without_new_charge(self) -> None:
        self.run_photos()
        report = self.report()
        for entry, request in zip(report["images"], self.requests):
            entry.pop("source_hash_version")
            entry["source_sha256"] = legacy_content_hash(*request["references"], prompt=request["prompt"], model=request["model"])
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(self.run_photos(), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(i["source_hash_version"] == CONTENT_HASH_VERSION for i in self.report()["images"]))

    def test_hash_detects_pixels_and_display_metadata_but_ignores_timestamps(self) -> None:
        path = self.renders / "overview_before.png"
        original = content_hash(path, prompt="test", model="test")
        self.image(path, date="new timestamp")
        self.assertEqual(original, content_hash(path, prompt="test", model="test"))
        self.image(path, color="yellow")
        self.assertNotEqual(original, content_hash(path, prompt="test", model="test"))
        self.image(path)
        self.assertNotEqual(original, content_hash(path, prompt="changed prompt", model="test"))
        metadata = PngImagePlugin.PngInfo()
        metadata.add(b"gAMA", struct.pack("!I", 45455))
        Image.new("RGB", (3, 2), "green").save(path, pnginfo=metadata)
        self.assertNotEqual(original, content_hash(path, prompt="test", model="test"))

    def test_legacy_cache_without_matching_source_bytes_is_not_assumed_valid(self) -> None:
        self.run_photos()
        report = self.report()
        for entry, request in zip(report["images"], self.requests):
            entry.pop("source_hash_version")
            entry["source_sha256"] = legacy_content_hash(*request["references"], prompt=request["prompt"], model=request["model"])
            entry["cost_usd"] = 0.045
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        self.image(self.renders / "overview_before.png", date="new metadata")
        self.assertEqual(self.run_photos(), 2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report()["images"], report["images"])


if __name__ == "__main__":
    unittest.main()
