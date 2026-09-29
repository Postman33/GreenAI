"""Package a runnable project directly, with models and docs but no run outputs."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile

from package_submission import ROOT, sources

EXCLUDED_DOCS = {"DEMO_WINDOWS.md", "DEMO_20260929.md", "RELEASE_CHECK.md"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "Sylvitect-core.zip")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error(f"Archive already exists; choose a new --output: {output}")
    files = set(sources()) | {ROOT / name for name in ("START_HERE.md", "run.ps1", "run.sh")}
    required = [ROOT / name for name in (
        "init.ps1", "init.sh", "run.ps1", "run.sh", "START_HERE.md",
        "models/utility_detector/latest/metadata.json", "poetry.lock",
        "output/pdf/Sylvitect-core_documentation.pdf", "output/pdf/sylvitect-core.pdf",
    )]
    for path in required:
        if not path.is_file():
            parser.error(f"Missing: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(files):
            relative = path.relative_to(ROOT)
            if relative.parent == Path("docs") and path.name in EXCLUDED_DOCS:
                continue
            if relative.parts[:2] == ("output", "pdf"):
                relative = Path("docs/pdf") / path.name
            data = path.read_bytes()
            if path.suffix == ".md":
                content = data.decode("utf-8-sig")
                content = content.replace("../output/pdf/", "pdf/")
                if relative == Path("docs/README.md"):
                    content = "\n".join(line for line in content.splitlines()
                                        if not any(name in line for name in EXCLUDED_DOCS)) + "\n"
                elif relative == Path("docs/development.md"):
                    content = content.replace("[docs/DEMO_20260929.md](DEMO_20260929.md)",
                                              "[инструкцию первого запуска](quickstart.md)")
                data = content.encode("utf-8")
            if path.suffix == ".sh":
                data = data.replace(b"\r\n", b"\n")
            name = "Sylvitect-core/" + relative.as_posix()
            info = zipfile.ZipInfo.from_file(path, name)
            info.create_system = 3
            info.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
            manifest.append(f"{hashlib.sha256(data).hexdigest()}  {relative.as_posix()}\n")
        archive.writestr("Sylvitect-core/SHA256SUMS.txt", "".join(manifest).encode("utf-8"))
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None, "ZIP integrity check failed"
        names = archive.namelist()
        assert not any("/output/" in name or name.endswith((".env", ".dxf", ".dwg", ".zip"))
                       for name in names), "Unexpected runtime data in archive"
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
    print(f"Project archive: {output}\nFiles: {len(manifest)}\nSize: {output.stat().st_size / 1_000_000:.1f} MB")


if __name__ == "__main__":
    main()
