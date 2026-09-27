"""Exercise the real Windows launcher's path validation without running stages."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import ROOT


def quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(shutil.which("powershell"), "Windows launcher requires PowerShell")
class PipelineLauncherTests(unittest.TestCase):
    def validate(self, drawing: Path, output: str | Path, *, absolute_configs: bool = False):
        command = (
            "$ErrorActionPreference = 'Stop'; "
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
            f"& {quote(ROOT / 'scripts/run_pipeline.ps1')} "
            f"-InputDxf {quote(drawing)} -OutputDirectory {quote(output)} "
            "-PipelineMode full -ValidateOnly"
        )
        if absolute_configs:
            command += (
                f" -SemanticConfig {quote(ROOT / 'src/core/config.yaml')}"
                f" -SurfaceConfig {quote(ROOT / 'src/core/surface_inspector_config.yaml')}"
            )
        command += " | ConvertTo-Json -Compress"
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        return subprocess.run(
            [shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded], cwd=ROOT, capture_output=True, timeout=30,
        )

    def test_absolute_and_relative_paths_resolve_to_same_files_without_writes(self):
        with tempfile.TemporaryDirectory(prefix=".launcher-test-", dir=ROOT) as directory:
            root = Path(directory)
            drawing = root / "Исходный чертёж.dxf"
            drawing.write_text("path-validation fixture", encoding="utf-8")
            output = root / "Результат с пробелами"
            reports = []
            for absolute in (False, True):
                result = self.validate(
                    drawing if absolute else drawing.relative_to(ROOT),
                    output if absolute else output.relative_to(ROOT),
                    absolute_configs=absolute,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                reports.append(json.loads(result.stdout.decode("utf-8-sig")))
                self.assertFalse(output.exists(), "Validation must not create output artifacts")
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(Path(reports[0]["output_directory"]), output)
            self.assertEqual(Path(reports[0]["input_dxf"]), drawing)
            self.assertEqual(reports[0]["pipeline_mode"], "full")

    def test_output_cannot_escape_workspace_through_absolute_or_relative_path(self):
        with tempfile.TemporaryDirectory(prefix=".launcher-test-", dir=ROOT) as directory:
            drawing = Path(directory) / "input.dxf"
            drawing.write_text("path-validation fixture", encoding="utf-8")
            sibling = ROOT.with_name(ROOT.name + "-outside") / "result"
            for output in (sibling, Path("..") / sibling.parent.name / "result", ROOT):
                result = self.validate(drawing, output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"OutputDirectory must be inside the workspace", result.stderr)


if __name__ == "__main__":
    unittest.main()
