"""Exercise the real Windows launcher's path validation without running stages."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
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


class LinuxPipelineLauncherTests(unittest.TestCase):
    """Validate the Linux entry point's paths and optional survey contract."""

    def validate(self, drawing: Path, output: Path, *options: str):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/run_pipeline.py"),
             str(drawing), str(output), *options, "--validate-only"],
            cwd=ROOT, env=environment, capture_output=True, timeout=30,
        )

    def test_relative_and_absolute_paths_and_survey_only(self):
        with tempfile.TemporaryDirectory(prefix=".linux-launcher-test-", dir=ROOT) as directory:
            folder = Path(directory)
            drawing = folder / "Исходный чертёж.dxf"
            drawing.write_text("path-validation fixture", encoding="utf-8")
            survey = folder / "shrubs.geojson"
            survey.write_text("{}", encoding="utf-8")
            output = folder / "Результат с пробелами"
            reports = []
            for relative in (True, False):
                result = self.validate(
                    drawing.relative_to(ROOT) if relative else drawing,
                    output.relative_to(ROOT) if relative else output,
                    "--existing-shrub-survey", str(survey.relative_to(ROOT) if relative else survey),
                    "--mode", "full",
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
                reports.append(json.loads(result.stdout.decode("utf-8")))
                self.assertFalse(output.exists(), "Validation must not create output artifacts")
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(reports[0]["existing_shrub_survey"], str(survey))
            self.assertEqual(reports[0]["planting_request"], None)

    def test_invalid_request_or_model_is_rejected_before_running(self):
        with tempfile.TemporaryDirectory(prefix=".linux-launcher-test-", dir=ROOT) as directory:
            folder = Path(directory)
            drawing = folder / "input.dxf"
            drawing.write_text("path-validation fixture", encoding="utf-8")
            output = folder / "result"
            result = self.validate(drawing, output, "--request", str(folder / "missing.json"))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
