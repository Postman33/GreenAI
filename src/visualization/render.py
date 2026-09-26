"""Locate Blender and launch the background GreenAI renderer."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def find_blender(explicit: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    if os.environ.get("BLENDER_EXE"):
        candidates.append(Path(os.environ["BLENDER_EXE"]))
    executable = shutil.which("blender")
    if executable:
        candidates.append(Path(executable))
    root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
    if root.exists():
        candidates.extend(sorted(root.glob("Blender */blender.exe"), reverse=True))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a prepared GreenAI scene with Blender")
    parser.add_argument("scene", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/visualization/renders"))
    parser.add_argument("--quality", choices=("draft", "final"), default="draft")
    parser.add_argument("--blender", type=Path)
    parser.add_argument("--views", default="overview,pedestrian,top")
    args = parser.parse_args()
    blender = find_blender(args.blender)
    if blender is None:
        raise SystemExit(
            "Blender is not installed. Install Blender, add blender.exe to PATH, "
            "set BLENDER_EXE, or pass --blender C:\\path\\to\\blender.exe. "
            "No API token is required."
        )
    script = Path(__file__).with_name("blender_render.py").resolve()
    command = [str(blender), "-b", "--python-exit-code", "1", "--python", str(script), "--",
               "--scene", str(args.scene.resolve()), "--output", str(args.output.resolve()),
               "--quality", args.quality, "--views", args.views]
    print("Blender:", blender)
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
