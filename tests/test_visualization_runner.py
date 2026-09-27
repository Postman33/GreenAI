from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from src.visualization.render import run_blender


class RendererLauncherTests(unittest.TestCase):
    def test_success_captures_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "renderer.log"
            run_blender([sys.executable, "-u", "-c",
                         "import sys; print('frame saved'); print('diagnostic', file=sys.stderr)"], log, 10)
            self.assertIn("frame saved", log.read_text())
            self.assertIn("diagnostic", log.read_text())

    def test_failure_points_to_renderer_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "renderer.log"
            with self.assertRaisesRegex(RuntimeError, "code 7.*renderer.log"):
                run_blender([sys.executable, "-c", "raise SystemExit(7)"], log, 10)

    def test_stalled_renderer_is_stopped_at_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "renderer.log"
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "was stopped"):
                run_blender([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"],
                            log, timeout_seconds=0.5, progress_seconds=0.2)
            self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
