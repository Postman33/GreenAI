"""Round-trip and invalid-input checks for the optional DXF georeference."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.cad_io import georeference


class GeoreferenceTests(unittest.TestCase):
    def test_rotated_affine_transform_round_trips_through_wgs84(self) -> None:
        config = {
            "local_to_epsg3857": {
                "matrix": [[0.0, -2.0], [2.0, 0.0]],
                "offset": [4_187_000.0, 7_500_000.0],
            }
        }

        longitude, latitude = georeference.local_to_wgs84(120.0, 50.0, config)
        x, y = georeference.wgs84_to_local(longitude, latitude, config)

        self.assertAlmostEqual(x, 120.0, places=6)
        self.assertAlmostEqual(y, 50.0, places=6)

    def test_web_mercator_clamps_latitudes_at_projection_limit(self) -> None:
        _, y = georeference.wgs84_to_epsg3857(37.0, 90.0)
        _, expected = georeference.wgs84_to_epsg3857(
            37.0, georeference.WEB_MERCATOR_LIMIT_DEG,
        )

        self.assertTrue(math.isfinite(y))
        self.assertAlmostEqual(y, expected, places=6)

    def test_singular_affine_transform_cannot_be_inverted(self) -> None:
        config = {
            "local_to_epsg3857": {
                "matrix": [[1.0, 2.0], [2.0, 4.0]],
                "offset": [0.0, 0.0],
            }
        }

        with self.assertRaisesRegex(ValueError, "singular"):
            georeference.epsg3857_to_local(1.0, 2.0, config)

    def test_georeference_file_rejects_missing_affine_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "georeference.json"
            path.write_text(json.dumps({
                "local_to_epsg3857": {
                    "matrix": [[1.0, 0.0]],
                    "offset": [0.0, 0.0],
                }
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid local_to_epsg3857"):
                georeference.load_georeference(path)


if __name__ == "__main__":
    unittest.main()
