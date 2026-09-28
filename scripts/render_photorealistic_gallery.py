"""Turn a Blender gallery into low-cost, matched photorealistic pairs."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.visualization.openrouter_images import (
    CONTENT_HASH_VERSION,
    DEFAULT_MODEL,
    content_hash,
    generate_image,
    legacy_content_hash,
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
        "the BEFORE state: do not add proposed trees or shrubs. Keep the exact building silhouettes "
        "and heights; do not invent buildings, extend facades, or fill empty areas with new objects. "
        "No people, cars, text or labels."
    )


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def place_paths(index_path: Path, place: dict) -> tuple[Path, Path]:
    """Prefer this gallery's assets when the output directory has been moved."""
    local = index_path.resolve().parent / place["id"]
    if (local / "scene.json").is_file():
        return local / "scene.json", local / "renders"
    return Path(place["scene"]), Path(place["renders"])


def cached_image(report: dict, photo_dir: Path, name: str, digest: str,
                 references: list[Path], prompt: str, model: str) -> dict | None:
    legacy_digest = None
    for entry in reversed(report["images"]):
        if entry["name"] != name or not (photo_dir / entry["file"]).is_file():
            continue
        if entry.get("source_hash_version") == CONTENT_HASH_VERSION:
            if entry["source_sha256"] == digest:
                return entry
        elif entry.get("source_hash_version", 1) == 1:
            if legacy_digest is None:
                legacy_digest = legacy_content_hash(*references, prompt=prompt, model=model)
            if entry["source_sha256"] == legacy_digest:
                entry.update(legacy_source_sha256=entry["source_sha256"],
                             source_sha256=digest, source_hash_version=CONTENT_HASH_VERSION)
                return entry
    return None


def image_stem(photo_dir: Path, name: str, digest: str, report: dict) -> Path:
    stem = f"{name}__{digest[:12]}"
    used = {Path(entry["file"]).stem for entry in report["images"]}
    candidate = stem
    version = 1
    while candidate in used or any(photo_dir.glob(candidate + ".*")):
        version += 1
        candidate = f"{stem}_{version}"
    return photo_dir / candidate


def normalize_image_names(report: dict, photo_dir: Path) -> bool:
    """Keep paid images while replacing old numbered labels with stable names."""
    changed = False
    renamed: dict[str, str] = {}
    for entry in report.get("images", []):
        old_name = entry["name"]
        match = re.fullmatch(r"(overview|pedestrian)_(before|after)(?:_[a-z]+)?_v\d+", old_name)
        if match is None:
            continue
        name = f"{match[1]}_{match[2]}"
        old_file = photo_dir / entry["file"]
        if not old_file.is_file():
            continue
        stem = f"{name}__{entry['source_sha256'][:12]}"
        target = photo_dir / f"{stem}{old_file.suffix}"
        suffix = 2
        while target.exists() and target != old_file:
            target = photo_dir / f"{stem}_{suffix}{old_file.suffix}"
            suffix += 1
        old_file.rename(target)
        renamed[old_file.name] = target.name
        entry["name"] = name
        entry["file"] = target.name
        changed = True
    if renamed:
        for entry in report["images"]:
            entry["references"] = [
                next((reference.replace(old, new) for old, new in renamed.items() if old in reference), reference)
                for reference in entry.get("references", [])
            ]
        report["current_images"] = {
            re.sub(r"_(?:context|ground|paired|species)_v\d+$", "", name): renamed.get(file, file)
            for name, file in report.get("current_images", {}).items()
        }
    return changed


def main() -> int:
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
    key = None
    views = ("overview", "pedestrian") if args.views == "both" else (args.views,)
    sent = 0
    spent = 0.0
    current_images = []
    status_path = args.gallery_index.resolve().parent / "photorealistic_status.json"

    def finish(status: str, message: str) -> int:
        _write_json(status_path, {
            "status": status, "new_image_count": sent, "spent_usd": spent,
            "max_cost_usd": args.max_cost_usd, "current_images": current_images,
            "message": message,
        })
        print(message, flush=True)
        return 0 if status == "complete" else 2
    # Count earlier successful calls too; re-running the command must not reset the budget.
    for place in places[:args.places]:
        scene_path, _ = place_paths(args.gallery_index, place)
        previous = scene_path.parent / "photorealistic" / "generation_report.json"
        if previous.exists():
            for entry in json.loads(previous.read_text(encoding="utf-8")).get("images", []):
                spent += float(entry["cost_usd"]) if entry.get("cost_usd") is not None else 0.04
    for place in places[:args.places]:
        scene_path, renders = place_paths(args.gallery_index, place)
        scene = json.loads(scene_path.read_text(encoding="utf-8"))
        photo_dir = scene_path.parent / "photorealistic"
        photo_dir.mkdir(parents=True, exist_ok=True)
        report_path = photo_dir / "generation_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {
            "model": args.model, "quality": "low", "images": []
        }
        if report["model"] != args.model:
            raise ValueError(f"{report_path}: model changed; choose another output directory")
        if normalize_image_names(report, photo_dir):
            _write_json(report_path, report)
        # The ledger keeps every paid version. Only matching files from this
        # invocation are advertised as current; old images remain untouched.
        report["current_images"] = {}
        _write_json(report_path, report)
        for view in views:
            before_source = renders / f"{view}_before.png"
            after_source = renders / f"{view}_after.png"
            mask_source = renders / f"{view}_plant_mask.png"
            for path in (before_source, after_source, mask_source):
                if not path.is_file():
                    raise FileNotFoundError(path)
            before_photo: Path | None = None
            for phase in ("before", "after"):
                prompt = before_prompt(view) if phase == "before" else after_prompt(view, scene, paired_before=True)
                if phase == "after" and not mask_source.is_file():
                    raise FileNotFoundError(f"{mask_source}: rerender the Blender gallery to create a species mask")
                if phase == "after" and before_photo is None:
                    raise ValueError("The AFTER image requires a matching BEFORE photograph")
                references = [before_source] if phase == "before" else [after_source, mask_source, before_photo]
                assert all(isinstance(path, Path) for path in references)
                digest = content_hash(*references, prompt=prompt, model=args.model)
                name = f"{view}_{phase}"
                cached = cached_image(report, photo_dir, name, digest, references, prompt, args.model)
                if cached:
                    cached_path = photo_dir / cached["file"]
                    report["current_images"][name] = cached["file"]
                    current_images.append(str(cached_path.resolve()))
                    _write_json(report_path, report)
                    print(f"SKIP {place['id']} {name}: {cached_path}", flush=True)
                    if phase == "before":
                        before_photo = cached_path
                    continue
                if any(entry["name"] == name for entry in report["images"]):
                    print(f"STALE {place['id']} {name}: prior version preserved; a new image is needed", flush=True)
                if sent >= args.max_images:
                    return finish("image_limit", "Photo generation paused at --max-images; the planting DXF and PDF are ready.")
                # Reserve a conservative allowance before each call. The API cost is only known after it returns.
                if spent + 0.04 > args.max_cost_usd:
                    return finish("budget_limit",
                                  f"Photo generation paused: ${spent:.4f} already spent of ${args.max_cost_usd:.2f}; "
                                  "the next request reserves $0.04. Prior images and costs are preserved. "
                                  "The planting DXF and PDF are ready.")
                if key is None:
                    key = load_api_key(args.key_file)
                print(f"Generating {place['id']} {name} with {args.model} (low quality)...", flush=True)
                output, cost = generate_image(
                    key=key, model=args.model, prompt=prompt, references=references,
                    output_stem=image_stem(photo_dir, name, digest, report),
                )
                sent += 1
                spent += cost if cost is not None else 0.04
                report["images"].append({
                    "name": name, "file": output.name, "source_sha256": digest,
                    "source_hash_version": CONTENT_HASH_VERSION,
                    "cost_usd": cost, "references": [str(path) for path in references],
                    **({"plants": scene_plant_summary(scene)} if phase == "after" else {}),
                })
                report["current_images"][name] = output.name
                current_images.append(str(output.resolve()))
                _write_json(report_path, report)
                print(f"  Saved: {output} | billed: {('$' + format(cost, '.4f')) if cost is not None else 'unreported'}", flush=True)
                if phase == "before":
                    before_photo = output
    return finish("complete", f"Done: {sent} new image(s), cumulative budget usage ${spent:.4f}")


if __name__ == "__main__":
    raise SystemExit(main())
