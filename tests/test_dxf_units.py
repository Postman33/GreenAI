from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ezdxf

from scripts.detect_dxf_units import resolve_units


class DxfUnitDetectionTests(unittest.TestCase):
    def make_dxf(self, directory: Path, insert_units: int) -> Path:
        path = directory / f"units_{insert_units}.dxf"
        document = ezdxf.new("R2013")
        document.header["$INSUNITS"] = insert_units
        document.modelspace().add_line((0, 0), (1, 0))
        document.saveas(path)
        return path

    def test_reads_metres_from_insunits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.make_dxf(Path(temporary), 6)
            report = resolve_units(path)

        self.assertEqual(report["insert_units_code"], 6)
        self.assertEqual(report["insert_units_name"], "Meters")
        self.assertEqual(report["dxf_units_per_meter"], 1.0)
        self.assertTrue(report["unit_scale_confirmed"])
        self.assertTrue(report["units_confirmed_as_metres"])
        self.assertEqual(report["unit_scale_source"], "dxf_header_insunits")

    def test_converts_millimetres_to_units_per_metre(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.make_dxf(Path(temporary), 4)
            report = resolve_units(path)

        self.assertEqual(report["dxf_units_per_meter"], 1000.0)
        self.assertFalse(report["units_confirmed_as_metres"])

    def test_unitless_dxf_requires_explicit_scale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.make_dxf(Path(temporary), 0)
            with self.assertRaisesRegex(ValueError, "does not declare usable"):
                resolve_units(path)

            report = resolve_units(path, explicit_units_per_meter=2.5)

        self.assertEqual(report["dxf_units_per_meter"], 2.5)
        self.assertEqual(report["unit_scale_source"], "explicit_parameter")

    def test_explicit_scale_records_header_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.make_dxf(Path(temporary), 6)
            report = resolve_units(path, explicit_units_per_meter=1000.0)

        self.assertEqual(report["dxf_units_per_meter"], 1000.0)
        self.assertTrue(report["warnings"])


if __name__ == "__main__":
    unittest.main()
