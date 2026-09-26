"""Turn a Blender gallery into low-cost, matched photorealistic pairs."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.visualization.openrouter_images import (
    DEFAULT_MODEL,
    content_hash,
    generate_image,
    load_api_key,
)
from src.visualization.plant_prompt import after_prompt, scene_plant_summary


def before_prompt(view: str) -> str:
    angle = "aerial oblique architectural view" if view == "overview" else "street-level pedestrian view"
    return (
        f"Edit reference image 1 into a natural photorealistic summer photograph of the same Moscow "
        f"street, {angle}. Preserve the exact camera, framing, road and sidewalk edges, buildings, "
        "existing vegetation, and every visible object position. Every pale green ground surface "
        "in the Blender image is existing grass or soil, including any large foreground area; "
        "keep its exact boundary and do not turn it into paving. Replace only low-poly materials "
        "with realistic facades, asphalt, paving, bark and foliage under soft daylight. This is "
        "the BEFORE state: do not add proposed trees or shrubs. No people, cars, text or labels."
    )


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gallery-index", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, default=ROOT / "config" / "openai.env")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--places", type=int, default=1, help="maximum number of places; default 1")
    parser.add_argument("--views", choices=("overview", "pedestrian", "both"), default="overview")
    parser.add_argument("--max-images", type=int, default=2)
    parser.add_argument("--max-cost-usd", type=float, default=0.10)
    args = parser.parse_args()
    if args.places < 1 or args.max_images < 1:
        parser.error("--places and --max-images must be positive")
    if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
        parser.error("--max-cost-usd must be positive")
    gallery = json.loads(args.gallery_index.read_text(encoding="utf-8"))
    places = gallery.get("places") or []
    if not places:
        parser.error("gallery_index.json contains no places")
    key = load_api_key(args.key_file)
    views = ("overview", "pedestrian") if args.views == "both" else (args.views,)
    sent = 0
    spent = 0.0
    # Count earlier successful calls too; re-running the command must not reset the budget.
    for place in places[:args.places]:
        previous = Path(place["scene"]).parent / "photorealistic" / "generation_report.json"
        if previous.exists():
            for entry in json.loads(previous.read_text(encoding="utf-8")).get("images", []):
                spent += float(entry["cost_usd"]) if entry.get("cost_usd") is not None else 0.04
    for place in places[:args.places]:
        renders = Path(place["renders"])
        scene = json.loads(Path(place["scene"]).read_text(encoding="utf-8"))
        photo_dir = Path(place["scene"]).parent / "photorealistic"
        photo_dir.mkdir(parents=True, exist_ok=True)
        report_path = photo_dir / "generation_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {
            "model": args.model, "quality": "low", "images": []
        }
        if report["model"] != args.model:
            raise ValueError(f"{report_path}: model changed; choose another output directory")
        for view in views:
            before_source = renders / f"{view}_before.png"
            after_source = renders / f"{view}_after.png"
            mask_source = renders / f"{view}_plant_mask.png"
            for path in (before_source, after_source):
                if not path.is_file():
                    raise FileNotFoundError(path)
            before_photo: Path | None = None
            for phase in ("before", "after"):
                prompt = before_prompt(view) if phase == "before" else after_prompt(view, scene)
                if phase == "after" and not mask_source.is_file():
                    raise FileNotFoundError(f"{mask_source}: rerender the Blender gallery to create a species mask")
                references = [before_source] if phase == "before" else [after_source, mask_source]
                assert all(isinstance(path, Path) for path in references)
                digest = content_hash(*references, prompt=prompt, model=args.model)
                name = f"{view}_after_species_v3" if phase == "after" else f"{view}_before_ground_v2"
                cached = next((entry for entry in report["images"] if entry["name"] == name), None)
                if cached:
                    cached_path = photo_dir / cached["file"]
                    if cached["source_sha256"] != digest or not cached_path.is_file():
                        raise ValueError(f"{name}: cached output differs from source; remove it to regenerate")
                    print(f"SKIP {place['id']} {name}: {cached_path}", flush=True)
                    if phase == "before":
                        before_photo = cached_path
                    continue
                if sent >= args.max_images:
                    print("Stopped at --max-images", flush=True)
                    return
                # Reserve a conservative allowance before each call. The API cost is only known after it returns.
                if spent + 0.04 > args.max_cost_usd:
                    print(f"Stopped at cost cap; spent ${spent:.4f}", flush=True)
                    return
                print(f"Generating {place['id']} {name} with {args.model} (low quality)...", flush=True)
                output, cost = generate_image(
                    key=key, model=args.model, prompt=prompt, references=references,
                    output_stem=photo_dir / name,
                )
                sent += 1
                spent += cost if cost is not None else 0.04
                report["images"].append({
                    "name": name, "file": output.name, "source_sha256": digest,
                    "cost_usd": cost, "references": [str(path) for path in references],
                    **({"plants": scene_plant_summary(scene)} if phase == "after" else {}),
                })
                _write_json(report_path, report)
                print(f"  Saved: {output} | billed: {('$' + format(cost, '.4f')) if cost is not None else 'unreported'}", flush=True)
                if phase == "before":
                    before_photo = output
    print(f"Done: {sent} new image(s), cumulative budget usage ${spent:.4f}", flush=True)


if __name__ == "__main__":
    main()
