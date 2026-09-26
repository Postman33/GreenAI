"""Validate and write the reusable preprocessing cache for run_pipeline.ps1.

The expensive part of the pipeline ends at plant allow-zone construction.
Planting presets, spacing, limits and user requests are deliberately excluded
from the cache key so a designer can iterate on them without reparsing DXF.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = 1

# Every file that can change semantic extraction, utility reconstruction or
# spatial constraints must invalidate the cache. Directory entries are walked
# recursively by ``path_signature``.
DEPENDENCIES = (
    "parser/dxf_extract_go",
    "src/core/config.yaml",
    "src/core/surface_inspector_config.yaml",
    "src/geometry/normalizer.py",
    "src/geometry/constraint_builder.py",
    "src/core/heat_chamber_detector.py",
    "src/rules/plant_allow_zone.py",
    "src/detection/network_reconstructor.py",
    "src/detection/overhead_power_reconstructor.py",
    "src/core/network_reconstructor_config.yaml",
    "src/detection/utilities/detector.py",
    "src/detection/utilities/onnx_model.py",
    "src/detection/cleaning/clean_utilities.py",
    "src/detection/cleaning/config.yaml",
    "init.sql",
    "scripts/seed.py",
)

# Diagnostics (PNG and large debug DXF files) are intentionally absent. They
# are presentation artifacts and are not needed to regenerate a planting plan.
ARTIFACTS = (
    "dxf_units_report.json",
    "extracted_objects.jsonl",
    "surface_candidates_raw.jsonl",
    "normalized_objects.geojsonl",
    "normalization_report.json",
    "cleaned_utilities.geojsonl",
    "review_utility_graphics.geojsonl",
    "rejected_utility_graphics.geojsonl",
    "utility_cleaning_report.json",
    "reconstructed_utilities.geojsonl",
    "inferred_utility_connections.geojsonl",
    "review_utility_connections.geojsonl",
    "network_reconstruction_report.json",
    "overhead_power_review.geojsonl",
    "overhead_power_reconstruction_report.json",
    "constraint_map.geojsonl",
    "constraint_report.json",
    "plant_allow_zones.geojsonl",
    "plant_allow_zones_report.json",
    "zone_verification_report.json",
)


def file_signature(path: Path, base: Path | None = None) -> dict[str, Any]:
    """Return a cheap signature suitable for invalidating local build data."""
    resolved = path.resolve()
    label = (
        resolved.relative_to(base.resolve()).as_posix()
        if base is not None and resolved.is_relative_to(base.resolve())
        else str(resolved)
    )
    if not resolved.exists():
        return {"path": label, "missing": True}
    stat = resolved.stat()
    return {
        "path": label,
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


def path_signature(path: Path, base: Path | None = None) -> list[dict[str, Any]]:
    if not path.exists() or path.is_file():
        return [file_signature(path, base)]
    return [
        file_signature(child, base)
        for child in sorted(path.rglob("*"), key=lambda item: str(item).casefold())
        if child.is_file()
    ]


def build_cache_key(
    workspace: Path,
    input_dxf: Path,
    detector_model: Path | None,
    units_argument: str,
    extra_dependencies: list[Path] | None = None,
) -> dict[str, Any]:
    dependencies: list[dict[str, Any]] = []
    for relative in DEPENDENCIES:
        dependencies.extend(path_signature(workspace / relative, workspace))
    model = (
        path_signature(detector_model, workspace)
        if detector_model is not None
        else [{"path": None, "disabled": True}]
    )
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "input_dxf": file_signature(input_dxf),
        "dxf_units_argument": units_argument,
        "detector_model": model,
        "dependencies": dependencies,
        "extra_dependencies": [
            signature
            for path in (extra_dependencies or [])
            for signature in path_signature(path, workspace)
        ],
    }


def artifact_signatures(output_directory: Path) -> list[dict[str, Any]]:
    return [file_signature(output_directory / name, output_directory) for name in ARTIFACTS]


def validate_manifest(
    manifest_path: Path,
    expected_key: dict[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    if not manifest_path.exists():
        return {"hit": False, "reason": "cache manifest is missing"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as error:
        return {"hit": False, "reason": f"cache manifest is invalid: {error}"}
    if manifest.get("cache_key") != expected_key:
        return {"hit": False, "reason": "input, model, scale or preprocessing code changed"}
    current = artifact_signatures(output_directory)
    if any(item.get("missing") for item in current):
        missing = [item["path"] for item in current if item.get("missing")]
        return {
            "hit": False,
            "reason": "cached artifacts are missing",
            "missing": missing,
        }
    if manifest.get("artifacts") != current:
        return {"hit": False, "reason": "cached artifacts were modified"}
    return {
        "hit": True,
        "reason": "preprocessing cache is valid",
        "created_at": manifest.get("created_at"),
    }


def write_manifest(
    manifest_path: Path,
    cache_key: dict[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    artifacts = artifact_signatures(output_directory)
    invalid = [item["path"] for item in artifacts if item.get("missing")]
    if invalid:
        raise ValueError("cannot cache missing artifacts: " + ", ".join(invalid))
    manifest = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cache_key": cache_key,
        "artifacts": artifacts,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return {"hit": True, "reason": "preprocessing cache written", "created_at": manifest["created_at"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "write"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input-dxf", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--detector-model", type=Path)
    parser.add_argument("--extra-dependency", type=Path, action="append", default=[])
    parser.add_argument(
        "--dxf-units-argument",
        required=True,
        help="'auto' or the exact explicitly confirmed drawing-units-per-metre value.",
    )
    parser.add_argument("--status-output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    key = build_cache_key(
        args.workspace,
        args.input_dxf,
        args.detector_model,
        args.dxf_units_argument,
        args.extra_dependency,
    )
    try:
        if args.action == "write":
            status = write_manifest(args.manifest, key, args.output_directory)
        else:
            status = validate_manifest(args.manifest, key, args.output_directory)
    except (OSError, ValueError, TypeError) as error:
        status = {"hit": False, "reason": str(error)}
        args.status_output.parent.mkdir(parents=True, exist_ok=True)
        args.status_output.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        raise SystemExit(f"Pipeline cache error: {error}") from error
    args.status_output.parent.mkdir(parents=True, exist_ok=True)
    args.status_output.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Pipeline cache: {'HIT' if status['hit'] else 'MISS'} — {status['reason']}")


if __name__ == "__main__":
    main()
