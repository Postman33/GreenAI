from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf
from ezdxf.lldxf.const import DXFStructureError
from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.lldxf.types import DXFBinaryTag, DXFTag

from src.cad_io import dxf_document


def entity_snapshot(document):
    result = {}
    for handle, entity in document.entitydb.items():
        if not entity.is_alive:
            continue
        # Include in-memory BLOCK_RECORDs created for legacy R12 drawings too.
        collector = TagCollector(dxfversion="AC1032")
        entity.export_dxf(collector)
        result[handle] = [(tag.code, repr(tag.value)) for tag in collector.tags]
    return result


class DxfDocumentTests(unittest.TestCase):
    def test_spooled_loading_matches_ezdxf_for_nested_blocks_layouts_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.dxf"
            document = ezdxf.new("R2018")
            document.header["$INSUNITS"] = 6
            document.layers.new("Здания")
            document.appids.add("TEST_METADATA")
            block = document.blocks.new("Контур")
            block.add_line((0, 0, 1), (10, 10, 2), dxfattribs={"layer": "Здания"})
            block.add_polyline3d([(1, 1, 1), (2, 2, 2), (3, 3, 3)])
            hatch = block.add_hatch()
            hatch.paths.add_polyline_path([(0, 0), (2, 0), (2, 2), (0, 2)])
            nested = document.blocks.new("Nested")
            nested.add_blockref("Контур", (3, 4), dxfattribs={"rotation": 32})
            insert = document.modelspace().add_blockref("Nested", (100, 200))
            insert.add_attrib("ID", "Камера", (100, 200))
            insert.set_xdata("TEST_METADATA", [(1000, "plant-id"), (1040, 1.25)])
            document.layouts.new("Sheet").add_line((1, 2), (3, 4))
            xrecord = document.objects.add_xrecord(owner=document.rootdict.dxf.handle)
            xrecord.tags.extend([DXFTag(1, "stored text"), DXFBinaryTag(310, b"\x00\x01\xff")])
            document.rootdict["BINARY_TEST"] = xrecord
            document.saveas(path)
            original_bytes = path.read_bytes()

            standard = ezdxf.readfile(path)
            spooled = dxf_document.read_dxf_document(path, spool_threshold_bytes=0)
            self.assertEqual(entity_snapshot(standard), entity_snapshot(spooled))
            self.assertEqual(spooled.header["$INSUNITS"], 6)
            self.assertEqual(spooled.filename, str(path))
            self.assertFalse(spooled.audit().errors)
            spooled.saveas(Path(directory) / "saved.dxf")
            saved = ezdxf.readfile(Path(directory) / "saved.dxf")
            self.assertFalse(saved.audit().errors)
            self.assertEqual(len(saved.blocks.get("Контур")), 3)
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_spooled_loading_preserves_legacy_polylines_and_empty_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            for version in ("R12", "R2000", "R2018"):
                for populated in (False, True):
                    path = Path(directory) / f"{version}_{populated}.dxf"
                    document = ezdxf.new(version)
                    if populated:
                        block = document.blocks.new("Fixture")
                        block.add_polyline2d([(0, 0), (1, 2), (3, 4)])
                        document.modelspace().add_blockref("Fixture", (20, 30))
                    document.saveas(path)
                    standard = ezdxf.readfile(path)
                    spooled = dxf_document.read_dxf_document(path, spool_threshold_bytes=0)
                    self.assertEqual(entity_snapshot(standard), entity_snapshot(spooled))

    def test_corrupt_structure_is_rejected_without_altering_input(self):
        cases = [
            "0\nSECTION\n2\nBLOCKS\n0\nEOF\n",
            "0\nSECTION\n2\nBLOCKS\n0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n",
            "0\nSECTION\n2\nENTITIES\n0\nENDSEC\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            for text in cases:
                path = Path(directory) / "broken.dxf"
                path.write_text(text, encoding="ascii")
                original_bytes = path.read_bytes()
                with self.assertRaises(DXFStructureError):
                    dxf_document.read_dxf_document(path, spool_threshold_bytes=0)
                self.assertEqual(path.read_bytes(), original_bytes)

    def test_binary_dxf_uses_standard_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binary.dxf"
            document = ezdxf.new("R2018")
            document.modelspace().add_line((1, 2), (3, 4))
            document.saveas(path, fmt="bin")
            with patch.object(dxf_document.ezdxf, "readfile", wraps=ezdxf.readfile) as read:
                loaded = dxf_document.read_dxf_document(path, spool_threshold_bytes=0)
            read.assert_called_once_with(path)
            self.assertEqual(len(loaded.modelspace().query("LINE")), 1)

    def test_failed_save_leaves_previous_output_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "result.dxf"
            path.write_bytes(b"previous complete output")
            document = ezdxf.new("R2018")

            def fail_save(temporary):
                Path(temporary).write_bytes(b"partial new drawing")
                raise MemoryError("simulated failed save")

            with patch.object(document, "saveas", side_effect=fail_save):
                with self.assertRaises(MemoryError):
                    dxf_document.save_dxf_atomic(document, path, allow_version_fallback=False)
            self.assertEqual(path.read_bytes(), b"previous complete output")
            self.assertEqual(list(root.iterdir()), [path])

    def test_locked_output_falls_back_without_reserializing_the_drawing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "result.dxf"
            path.write_bytes(b"locked output")
            document = ezdxf.new("R2018")
            document.modelspace().add_line((0, 0), (1, 1))
            replace = dxf_document.os.replace

            def locked_first(source, target):
                if target == path:
                    raise PermissionError("locked by CAD")
                return replace(source, target)

            with patch.object(dxf_document.os, "replace", side_effect=locked_first), \
                    patch.object(document, "saveas", wraps=document.saveas) as save:
                actual = dxf_document.save_dxf_atomic(document, path, allow_version_fallback=True)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(actual, root / "result_v2.dxf")
            self.assertEqual(path.read_bytes(), b"locked output")
            self.assertEqual(len(ezdxf.readfile(actual).modelspace().query("LINE")), 1)


if __name__ == "__main__":
    unittest.main()
