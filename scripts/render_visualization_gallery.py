"""Render several spatially distinct Blender before/after comparisons."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.visualization.prepare_scene import build_manifest, choose_focuses, load_planting_plan


def render_gallery(pipeline_output: Path, output: Path, places: int,
                   radius: float, quality: str, blender: Path | None,
                   prepare_only: bool = False) -> dict:
    normalized = pipeline_output / "normalized_objects.geojsonl"
    constraints = pipeline_output / "constraint_map.geojsonl"
    planting = pipeline_output / "planting_plan.geojsonl"
    for path in (normalized, constraints, planting):
        if not path.is_file():
            raise FileNotFoundError(path)
    focuses = choose_focuses(load_planting_plan(planting), radius, places)
    if len(focuses) < places:
        raise ValueError(f"Only {len(focuses)} distinct planted places found; requested {places}")
    output.mkdir(parents=True, exist_ok=True)
    index = {"version": 1, "source": str(pipeline_output.resolve()),
             "focus_radius_m": radius, "places": []}
    renderer = ROOT / "src" / "visualization" / "render.py"
    for number, focus in enumerate(focuses, start=1):
        place_dir = output / f"place_{number:02d}"
        scene_path = place_dir / "scene.json"
        renders = place_dir / "renders"
        print(f"[{number}/{len(focuses)}] Place at ({focus.x:.2f}, {focus.y:.2f})", flush=True)
        manifest = build_manifest(normalized, constraints, planting, scene_path,
                                  place_dir / "scene_preview.png", radius,
                                  focus.x, focus.y)
        pedestrian = next(camera for camera in manifest["cameras"]
                          if camera["name"] == "pedestrian")
        if pedestrian.get("placement") != "road_with_clear_view":
            raise ValueError(f"{place_dir}: no unobstructed pedestrian camera on a road")
        if not prepare_only:
            command = [sys.executable, str(renderer), str(scene_path),
                       "--output", str(renders), "--quality", quality,
                       "--views", "overview,pedestrian"]
            if blender is not None:
                command += ["--blender", str(blender)]
            subprocess.run(command, cwd=ROOT, check=True)
        index["places"].append({
            "id": f"place_{number:02d}",
            "origin": manifest["source_origin"],
            "counts": manifest["counts"],
            "cameras": [camera for camera in manifest["cameras"]
                        if camera["name"] in {"overview", "pedestrian"}],
            "scene": str(scene_path.resolve()),
            "renders": str(renders.resolve()),
        })
        (output / "gallery_index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description="Blender preview at several planted places")
    parser.add_argument("--pipeline-output", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--places", type=int, default=3)
    parser.add_argument("--radius", type=float, default=45.0)
    parser.add_argument("--quality", choices=("draft", "final"), default="draft")
    parser.add_argument("--blender", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.places <= 8:
        parser.error("--places must be between 1 and 8")
    if not math.isfinite(args.radius) or args.radius <= 5:
        parser.error("--radius must be greater than 5 m")
    output = args.output or args.pipeline_output / "blender_gallery"
    index = render_gallery(args.pipeline_output, output, args.places,
                           args.radius, args.quality, args.blender,
                           args.prepare_only)
    print(f"Gallery index: {output / 'gallery_index.json'}")
    for place in index["places"]:
        print(f"  {place['id']}: {place['counts']['proposed_trees']} trees, "
              f"{place['counts']['proposed_shrub_instances']} shrubs")


if __name__ == "__main__":
    main()
