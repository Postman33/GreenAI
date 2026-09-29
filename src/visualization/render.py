"""Locate Blender and launch the background Sylvitect-core renderer."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
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


def run_blender(command: list[str], log_path: Path, timeout_seconds: float = 180.0,
                progress_seconds: float = 15.0) -> None:
    """Bound the renderer wait and keep native output independent of the terminal."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"Blender log: {log_path}", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT) as process:
            try:
                while True:
                    remaining = timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Blender exceeded {timeout_seconds:g} seconds and was stopped. "
                            f"The planting DXF and PDF are already saved. Log: {log_path}"
                        )
                    try:
                        return_code = process.wait(timeout=min(progress_seconds, remaining))
                    except subprocess.TimeoutExpired:
                        elapsed = time.monotonic() - started
                        with log_path.open(encoding="utf-8", errors="replace") as reader:
                            reader.seek(max(0, log_path.stat().st_size - 4096))
                            lines = reader.read().splitlines()
                        latest = next((line for line in reversed(lines) if line.strip()), "starting")
                        print(f"Blender: {elapsed:.0f}s — {latest}", flush=True)
                        continue
                    if return_code:
                        raise RuntimeError(f"Blender exited with code {return_code}. Log: {log_path}")
                    print(f"Blender completed in {time.monotonic() - started:.1f}s", flush=True)
                    return
            finally:
                # Stop only the renderer launched by this invocation, including
                # cancellation, before Popen.__exit__ waits for it.
                if process.poll() is None:
                    process.kill()
                    process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a prepared Sylvitect-core scene with Blender")
    parser.add_argument("scene", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/visualization/renders"))
    parser.add_argument("--quality", choices=("draft", "final"), default="draft")
    parser.add_argument("--blender", type=Path)
    parser.add_argument("--views", default="overview,pedestrian,top")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    if not 0 < args.timeout_seconds < float("inf"):
        parser.error("--timeout-seconds must be finite and positive")
    blender = find_blender(args.blender)
    if blender is None:
        raise SystemExit(
            "Blender is not installed. Install Blender, add blender.exe to PATH, "
            "set BLENDER_EXE, or pass --blender C:\\path\\to\\blender.exe. "
            "No API token is required."
        )
    script = Path(__file__).with_name("blender_render.py").resolve()
    command = [str(blender), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(script), "--",
               "--scene", str(args.scene.resolve()), "--output", str(args.output.resolve()),
               "--quality", args.quality, "--views", args.views]
    print("Blender:", blender, flush=True)
    run_blender(command, args.output.resolve() / "blender_render.log", args.timeout_seconds)


if __name__ == "__main__":
    main()
