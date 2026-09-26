"""Low-cost OpenRouter image edits of matched Blender before/after renders."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


IMAGE_API = "https://openrouter.ai/api/v1/images"
DEFAULT_MODEL = "openai/gpt-image-2"
MIME_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def load_api_key(key_file: Path | None = None) -> str:
    """Read a key without placing it in logs, manifests, or command arguments."""
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if key_file is None:
        raise ValueError("Set OPENROUTER_API_KEY or pass --key-file")
    for line in key_file.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() in {"OPENROUTER_API_KEY", "OPENAI_API_KEY"}:
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    raise ValueError(f"No OPENROUTER_API_KEY or OPENAI_API_KEY in {key_file}")


def image_reference(path: Path) -> dict:
    image = path.read_bytes()
    extension = _detect_image(image, None)
    mime = {value: key for key, value in MIME_EXTENSIONS.items()}[extension]
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64," + base64.b64encode(image).decode("ascii")},
    }


def content_hash(*paths: Path, prompt: str, model: str) -> str:
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(prompt.encode("utf-8"))
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _detect_image(data: bytes, claimed_mime: str | None) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        actual = "image/webp"
    else:
        raise ValueError("OpenRouter returned an unsupported image format")
    if claimed_mime and claimed_mime != actual:
        raise ValueError(f"OpenRouter image format mismatch: {claimed_mime} vs {actual}")
    return MIME_EXTENSIONS[actual]


def generate_image(*, key: str, model: str, prompt: str,
                   references: list[Path], output_stem: Path,
                   timeout_seconds: int = 240) -> tuple[Path, float | None]:
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "quality": "low",
        "aspect_ratio": "16:9",
        "input_references": [image_reference(path) for path in references],
    }
    request = Request(
        IMAGE_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace").replace(key, "[REDACTED]")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from None
    except URLError as exc:
        raise RuntimeError(f"Cannot reach OpenRouter: {exc.reason}") from None
    images = result.get("data") or []
    if len(images) != 1 or not images[0].get("b64_json"):
        raise ValueError("OpenRouter did not return exactly one base64 image")
    raw = base64.b64decode(images[0]["b64_json"], validate=True)
    extension = _detect_image(raw, images[0].get("media_type"))
    cost = (result.get("usage") or {}).get("cost")
    if cost is not None:
        cost = float(cost)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("OpenRouter returned an invalid usage cost")
    output = output_stem.with_suffix(extension)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    return output, cost
