"""Download and verify the 3rd Parkovaya demo drawing from Google Drive."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import os
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, build_opener


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "Пилотный проект 20 улиц" / "input_10001759_bound.dxf"
FILE_ID = "1kFEoMvMNR5xNLVW5t1XIx4I_Gq_s37Wd"
DOWNLOAD_URL = f"https://drive.google.com/uc?{urlencode({'export': 'download', 'id': FILE_ID})}"
EXPECTED_SIZE = 347_508_736
EXPECTED_SHA256 = "a58369ba24b35b86be9ac70a20de4e81b6e49d78156692696a86e0eedfc855b3"
CHUNK_SIZE = 1024 * 1024


class DownloadForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self.in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and values.get("id") == "download-form":
            self.action = values.get("action")
            self.in_form = True
        elif tag == "input" and self.in_form and values.get("type") == "hidden":
            name, value = values.get("name"), values.get("value")
            if name and value is not None:
                self.fields[name] = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.in_form = False


def download_response(opener):
    response = opener.open(DOWNLOAD_URL, timeout=45)
    if "text/html" not in response.headers.get("Content-Type", "").lower():
        return response
    page_url = response.url
    page = response.read(1024 * 1024).decode("utf-8", "replace")
    response.close()
    form = DownloadForm()
    form.feed(page)
    if not form.action or form.fields.get("id") != FILE_ID or form.fields.get("confirm") != "t":
        raise RuntimeError("Google Drive did not provide a valid download confirmation form")
    action = urljoin(page_url, form.action)
    if urlparse(action).hostname not in {"drive.google.com", "drive.usercontent.google.com"}:
        raise RuntimeError("Google Drive confirmation points to an unexpected host")
    response = opener.open(f"{action}?{urlencode(form.fields)}", timeout=45)
    if "text/html" in response.headers.get("Content-Type", "").lower():
        response.close()
        raise RuntimeError("Google Drive returned a page instead of the DXF download")
    return response


def fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def verified(path: Path) -> bool:
    if path.stat().st_size != EXPECTED_SIZE:
        return False
    return fingerprint(path) == (EXPECTED_SIZE, EXPECTED_SHA256)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    destination = args.output.expanduser().resolve()
    if destination.exists():
        if not destination.is_file() or not verified(destination):
            raise RuntimeError(f"Existing demo file differs from the expected DXF; left untouched: {destination}")
        print(f"Demo DXF already verified: {destination}", flush=True)
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    temporary: Path | None = None
    try:
        with download_response(opener) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".demo-dxf-", suffix=".part",
                dir=destination.parent, delete=False,
            ) as target:
                temporary = Path(target.name)
                digest = hashlib.sha256()
                size = 0
                while chunk := response.read(CHUNK_SIZE):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size // (32 * CHUNK_SIZE) != (size - len(chunk)) // (32 * CHUNK_SIZE):
                        print(f"Downloaded {size // CHUNK_SIZE} MiB...", flush=True)
        if size != EXPECTED_SIZE or digest.hexdigest() != EXPECTED_SHA256:
            raise RuntimeError(
                f"Downloaded file failed validation (size {size}, SHA-256 {digest.hexdigest()}); "
                "the destination was not changed"
            )
        if destination.exists():
            raise RuntimeError(f"Destination appeared during download; left untouched: {destination}")
        os.replace(temporary, destination)
        temporary = None
        print(f"Demo DXF downloaded and verified: {destination}", flush=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(f"Demo DXF error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
