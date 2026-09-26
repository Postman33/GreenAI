from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.visualization.openrouter_images import generate_image, load_api_key


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


class OpenRouterImageTests(unittest.TestCase):
    def test_key_file_accepts_legacy_name_without_exposing_value(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            key_file = Path(folder) / "key.env"
            key_file.write_text("# comment\nOPENAI_API_KEY='sk-or-secret'\n", encoding="utf-8")
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": "", "OPENAI_API_KEY": ""}):
                self.assertEqual(load_api_key(key_file), "sk-or-secret")

    def test_reference_edit_sends_low_quality_and_saves_reported_cost(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference.png"
            reference.write_bytes(PNG)
            response = {
                "data": [{"b64_json": base64.b64encode(PNG).decode("ascii"), "media_type": "image/png"}],
                "usage": {"cost": 0.0142},
            }
            seen = {}

            def fake_urlopen(request, timeout):
                seen["url"] = request.full_url
                seen["payload"] = json.loads(request.data)
                seen["timeout"] = timeout
                return io.BytesIO(json.dumps(response).encode("utf-8"))

            with patch("src.visualization.openrouter_images.urlopen", side_effect=fake_urlopen):
                output, cost = generate_image(
                    key="secret", model="openai/gpt-image-2", prompt="Keep the street",
                    references=[reference], output_stem=root / "result",
                )
            self.assertEqual(output.read_bytes(), PNG)
            self.assertEqual(cost, 0.0142)
            self.assertEqual(seen["url"], "https://openrouter.ai/api/v1/images")
            self.assertEqual(seen["payload"]["quality"], "low")
            self.assertEqual(seen["payload"]["aspect_ratio"], "16:9")
            self.assertEqual(len(seen["payload"]["input_references"]), 1)


if __name__ == "__main__":
    unittest.main()
