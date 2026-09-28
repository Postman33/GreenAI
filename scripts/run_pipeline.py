"""Run the complete GreenAI DXF pipeline on Linux or in the planner container."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
PRESETS = (
    "balanced_mixed", "dense_mixed", "tree_lawn", "trees_only", "shrub_lawn",
    "shrubs_only", "lawn_only", "alley", "hedge", "shrub_mass", "free_group",
    "mixed_flowerbed",
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("input_dxf", nargs="?", type=Path)
    result.add_argument("output_directory", nargs="?", type=Path)
    result.add_argument("planting_request", nargs="?", type=Path)
    result.add_argument("existing_shrub_survey", nargs="?", type=Path)
    result.add_argument("--request", dest="request_option", type=Path)
    result.add_argument("--existing-shrub-survey", dest="survey_option", type=Path)
    result.add_argument("--mode", choices=("auto", "full", "lean", "fast"), default="auto")
    result.add_argument("--preset", choices=PRESETS, default="dense_mixed")
    result.add_argument("--tree-spacing-m", type=float)
    result.add_argument("--tree-max-count", type=int)
    result.add_argument("--diagnostic-rejected-max-count", type=int)
    result.add_argument("--dxf-units-per-meter", type=float)
    result.add_argument("--semantic-config", type=Path, default=Path("src/core/config.yaml"))
    result.add_argument("--surface-config", type=Path, default=Path("src/core/surface_inspector_config.yaml"))
    result.add_argument("--road-corrections", type=Path)
    result.add_argument("--model", type=Path)
    result.add_argument("--extractor", type=Path)
    result.add_argument("--skip-database-start", action="store_true")
    result.add_argument("--validate-only", action="store_true")
    return result


def resolve_path(value: Path | None, *, required: bool = False) -> Path | None:
    if value is None:
        return None
    path = value.expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if required and not path.exists():
        raise ValueError(f"File or directory does not exist: {path}")
    return path


def execute(command: list[str | Path], environment: dict[str, str]) -> None:
    subprocess.run(
        [str(part) for part in command], cwd=ROOT, env=environment, check=True
    )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    args = parser().parse_args()
    if args.dxf_units_per_meter is None and os.environ.get("DXF_UNITS_PER_METER"):
        args.dxf_units_per_meter = float(os.environ["DXF_UNITS_PER_METER"])
    default_input = (
        Path("/data/input.dxf") if Path("/data/input.dxf").is_file()
        else ROOT / "Пилотный проект 20 улиц" / "input_10001759_bound.dxf"
    )
    input_dxf = resolve_path(args.input_dxf or default_input, required=True)
    assert input_dxf is not None
    if not input_dxf.is_file() or input_dxf.suffix.casefold() != ".dxf":
        raise ValueError(f"Expected a DXF file: {input_dxf}")
    default_output = Path("/data/output") if input_dxf == Path("/data/input.dxf") else ROOT / "output/latest"
    output = resolve_path(args.output_directory or default_output)
    assert output is not None
    if output == input_dxf or output.is_file():
        raise ValueError(f"Output must be a directory distinct from the input DXF: {output}")

    semantic = resolve_path(args.semantic_config, required=True)
    surface = resolve_path(args.surface_config, required=True)
    if args.planting_request and args.request_option:
        raise ValueError("Specify the planting request once, as a positional path or --request.")
    if args.existing_shrub_survey and args.survey_option:
        raise ValueError("Specify the shrub survey once, as a positional path or --existing-shrub-survey.")
    request = resolve_path(args.request_option or args.planting_request, required=True)
    survey = resolve_path(args.survey_option or args.existing_shrub_survey, required=True)
    corrections = resolve_path(args.road_corrections, required=True)
    if corrections is None:
        candidate = ROOT / "src/core/road_corrections" / f"{input_dxf.stem}.geojson"
        corrections = candidate.resolve() if candidate.is_file() else None
    model = resolve_path(args.model, required=True)
    if model is None:
        candidate = ROOT / "models/utility_detector/latest"
        model = candidate.resolve() if candidate.is_dir() else None
    for label, path in (("semantic config", semantic), ("surface config", surface),
                        ("planting request", request), ("shrub survey", survey),
                        ("road corrections", corrections)):
        if path is not None and not path.is_file():
            raise ValueError(f"Expected {label} file: {path}")
    if model is not None and not model.is_dir():
        raise ValueError(f"Expected model directory: {model}")
    if request and (args.tree_spacing_m is not None or args.tree_max_count is not None):
        raise ValueError("Put tree spacing and maximum count in the planting request JSON.")
    if args.tree_spacing_m is not None and not (0 < args.tree_spacing_m < float("inf")):
        raise ValueError("--tree-spacing-m must be positive and finite.")
    if args.tree_max_count is not None and args.tree_max_count <= 0:
        raise ValueError("--tree-max-count must be positive.")
    if args.diagnostic_rejected_max_count is not None and args.diagnostic_rejected_max_count < 0:
        raise ValueError("--diagnostic-rejected-max-count must be nonnegative.")
    if args.dxf_units_per_meter is not None and not (0 < args.dxf_units_per_meter < float("inf")):
        raise ValueError("--dxf-units-per-meter must be positive and finite.")

    settings = {
        "input_dxf": str(input_dxf),
        "output_directory": str(output),
        "semantic_config": str(semantic),
        "surface_config": str(surface),
        "road_corrections": str(corrections) if corrections else None,
        "utility_detector_model": str(model) if model else None,
        "planting_request": str(request) if request else None,
        "existing_shrub_survey": str(survey) if survey else None,
        "planting_preset": None if request else args.preset,
        "tree_spacing_m": args.tree_spacing_m,
        "tree_max_count": args.tree_max_count,
        "diagnostic_rejected_max_count": args.diagnostic_rejected_max_count,
        "requested_dxf_units_per_meter": args.dxf_units_per_meter,
        "requested_pipeline_mode": args.mode,
    }
    if args.validate_only:
        print(json.dumps(settings, ensure_ascii=False, indent=2))
        return

    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.setdefault("MPLBACKEND", "Agg")
    environment.setdefault("DATABASE_URL", "postgresql://admin:admin@localhost:5432/admin")
    python = sys.executable
    extractor = resolve_path(args.extractor, required=True) if args.extractor else None
    if extractor is None and environment.get("DXF_EXTRACTOR"):
        extractor = resolve_path(Path(environment["DXF_EXTRACTOR"]), required=True)
    if extractor is None:
        packaged = Path("/usr/local/bin/dxf_extract_go")
        extractor = packaged if packaged.is_file() else ROOT / ".gotmp/dxf_extract_go"

    files = {name: output / filename for name, filename in {
        "units": "dxf_units_report.json",
        "objects": "extracted_objects.jsonl",
        "surfaces": "surface_candidates_raw.jsonl",
        "normalized": "normalized_objects.geojsonl",
        "normalization_report": "normalization_report.json",
        "cleaned": "cleaned_utilities.geojsonl",
        "review_utilities": "review_utility_graphics.geojsonl",
        "rejected_utilities": "rejected_utility_graphics.geojsonl",
        "cleaning_report": "utility_cleaning_report.json",
        "utility_debug_dxf": "utility_cleaning_debug.dxf",
        "utility_debug_png": "utility_cleaning_debug.png",
        "reconstructed": "reconstructed_utilities.geojsonl",
        "inferred": "inferred_utility_connections.geojsonl",
        "review_connections": "review_utility_connections.geojsonl",
        "reconstruction_report": "network_reconstruction_report.json",
        "network_debug": "network_reconstruction_debug.dxf",
        "overhead_review": "overhead_power_review.geojsonl",
        "overhead_report": "overhead_power_reconstruction_report.json",
        "overhead_debug": "overhead_power_reconstruction_debug.dxf",
        "surface_dxf": "surface_candidates.dxf",
        "surface_png": "surface_candidates.png",
        "surface_report": "surface_candidates_report.json",
        "constraints": "constraint_map.geojsonl",
        "constraint_report": "constraint_report.json",
        "zones": "plant_allow_zones.geojsonl",
        "zone_report": "plant_allow_zones_report.json",
        "zone_verification": "zone_verification_report.json",
        "existing_tree_audit": "existing_tree_audit.geojsonl",
        "existing_tree_report": "existing_tree_audit_report.json",
        "plan": "planting_plan.geojsonl",
        "decisions": "planting_decisions.geojsonl",
        "plan_report": "planting_plan_report.json",
        "explanations": "planting_explanations.md",
        "layout_trace": "planting_layout_trace.jsonl",
        "debug_dxf": "planting_diagnostics.dxf",
        "debug_png": "plant_allow_zones_debug.png",
        "debug_legend": "planting_diagnostics_legend.md",
        "full_dxf": "result_with_planting_plan.dxf",
        "overlay_dxf": "planting_overlay.dxf",
        "verification": "verification_report.json",
        "pdf": "greenai_planting_report.pdf",
        "atlas": "planting_plan_atlas.pdf",
        "schedule": "planting_area_schedule.json",
        "cache_manifest": "preprocessing_cache.json",
        "cache_status": "preprocessing_cache_status.json",
    }.items()}
    f = files.__getitem__
    stages: list[dict[str, object]] = []
    started = time.monotonic()

    def stage(stage_id: str, title: str, command: list[str | Path]) -> None:
        print(f"[{stage_id}] {title}", flush=True)
        stage_started = time.monotonic()
        status = "passed"
        try:
            execute(command, environment)
        except subprocess.CalledProcessError:
            status = "failed"
            raise
        finally:
            stages.append({
                "id": stage_id, "name": title, "status": status,
                "elapsed_seconds": round(time.monotonic() - stage_started, 6),
            })

    def py(stage_id: str, title: str, *command: str | Path) -> None:
        stage(stage_id, title, [python, *command])

    def cache_command(action: str) -> list[str | Path]:
        command: list[str | Path] = [
            "scripts/pipeline_cache.py", action,
            "--workspace", ROOT, "--input-dxf", input_dxf,
            "--output-directory", output, "--manifest", f("cache_manifest"),
            "--status-output", f("cache_status"),
            "--dxf-units-argument",
            str(args.dxf_units_per_meter) if args.dxf_units_per_meter is not None else "auto",
            "--extra-dependency", semantic, "--extra-dependency", surface,
        ]
        if model:
            command += ["--detector-model", model]
        if corrections:
            command += ["--extra-dependency", corrections]
        return command

    try:
        if not extractor.is_file():
            extractor.parent.mkdir(parents=True, exist_ok=True)
            if shutil.which("go") is None:
                raise RuntimeError("Go DXF extractor is missing. Run scripts/install.sh first.")
            stage("00a", "Build Go DXF extractor", [
                "go", "build", "-buildvcs=false", "-o", extractor, "./parser/dxf_extract_go"
            ])

        py("00", "Check preprocessing cache", *cache_command("check"))
        cache_status = json.loads(f("cache_status").read_text(encoding="utf-8"))
        cache_hit = bool(cache_status["hit"])
        if args.mode == "fast" and not cache_hit:
            raise RuntimeError(f"Fast mode needs a valid preprocessing cache: {cache_status['reason']}")
        reuse = cache_hit and args.mode in ("auto", "fast")
        actual_mode = "fast" if reuse else "lean" if args.mode == "lean" else "full"
        result_dxf = f("overlay_dxf") if actual_mode in ("fast", "lean") else f("full_dxf")
        settings.update({
            "actual_pipeline_mode": actual_mode,
            "preprocessing_cache_hit": cache_hit,
            "preprocessing_cache_reason": cache_status["reason"],
            "result_dxf": str(result_dxf),
        })
        write_json(output / "pipeline_run_parameters.json", settings)

        if not reuse:
            database_host = urlparse(environment["DATABASE_URL"]).hostname
            if not args.skip_database_start and database_host in (None, "localhost", "127.0.0.1", "::1"):
                stage("01", "Start PostGIS", ["docker", "compose", "up", "-d", "--wait", "postgis"])
            unit_args: list[str | Path] = ["scripts/detect_dxf_units.py", input_dxf, "--output", f("units")]
            if args.dxf_units_per_meter is not None:
                unit_args += ["--dxf-units-per-meter", str(args.dxf_units_per_meter)]
            py("02", "Detect DXF units", *unit_args)
            stage("03", "Extract semantic objects", [
                extractor, "--config", semantic, "--output", f("objects"), input_dxf
            ])
            stage("04", "Extract surface candidates", [
                extractor, "--config", surface, "--output", f("surfaces"), input_dxf
            ])
            py("05", "Normalize CAD geometry", "-m", "src.geometry.normalizer",
               f("objects"), "--output", f("normalized"), "--report", f("normalization_report"))

            if model:
                cleaner: list[str | Path] = [
                    "-m", "src.detection.utilities.detector", "predict", f("objects"),
                    "--model", model, "--output", f("cleaned"),
                    "--review-output", f("review_utilities"),
                    "--rejected-output", f("rejected_utilities"),
                    "--report", f("cleaning_report"),
                ]
            else:
                cleaner = [
                    "-m", "src.detection.cleaning.clean_utilities", f("objects"),
                    "--work-boundary", f("normalized"), "--output", f("cleaned"),
                    "--review-output", f("review_utilities"),
                    "--rejected-output", f("rejected_utilities"),
                    "--report", f("cleaning_report"),
                    "--dxf-units-per-meter", str(json.loads(f("units").read_text(encoding="utf-8"))["dxf_units_per_meter"]),
                ]
            if actual_mode != "lean":
                cleaner += ["--debug-dxf", f("utility_debug_dxf"), "--debug-png", f("utility_debug_png")]
            py("06", "Clean engineering utilities", *cleaner)
            units = json.loads(f("units").read_text(encoding="utf-8"))
            unit_scale = str(units["dxf_units_per_meter"])
            network: list[str | Path] = [
                "-m", "src.detection.network_reconstructor", f("cleaned"),
                "--output", f("reconstructed"), "--inferred-output", f("inferred"),
                "--review-output", f("review_connections"), "--report", f("reconstruction_report"),
                "--dxf-units-per-meter", unit_scale,
            ]
            if actual_mode != "lean":
                network += ["--debug-dxf", f("network_debug")]
            py("07", "Reconstruct utility networks", *network)
            overhead: list[str | Path] = [
                "-m", "src.detection.overhead_power_reconstructor", input_dxf,
                "--objects", f("objects"), "--base-utilities", f("reconstructed"),
                "--output", f("reconstructed"), "--review-output", f("overhead_review"),
                "--report", f("overhead_report"), "--dxf-units-per-meter", unit_scale,
            ]
            if actual_mode != "lean":
                overhead += ["--debug-dxf", f("overhead_debug")]
            py("07b", "Reconstruct overhead power hypotheses", *overhead)
            if actual_mode != "lean":
                py("08", "Render source surface diagnostics", "-m", "src.cad_io.surface_inspector",
                   f("surfaces"), f("normalized"), "--dxf-output", f("surface_dxf"),
                   "--png-output", f("surface_png"), "--report", f("surface_report"))
            constraint: list[str | Path] = [
                "-m", "src.geometry.constraint_builder", f("normalized"), f("surfaces"),
                "--output", f("constraints"), "--report", f("constraint_report"),
                "--unit-metadata", f("units"), "--reconstructed-utilities", f("reconstructed"),
            ]
            if corrections:
                constraint += ["--road-corrections", corrections]
            py("09", "Build physical constraints", *constraint)
            py("09b", "Update plant catalog and rules", "scripts/seed.py")
            py("10", "Apply plant rules", "-m", "src.rules.plant_allow_zone",
               f("constraints"), f("normalized"), "--output", f("zones"),
               "--report", f("zone_report"), "--utility-geometries", f("reconstructed"),
               "--dxf-units-per-meter", unit_scale, "--unit-metadata", f("units"))
            py("11", "Verify allowed zones", "scripts/verify_outputs.py",
               f("constraints"), f("zones"), "--constraint-report", f("constraint_report"),
               "--zone-report", f("zone_report"), "--output", f("zone_verification"))
            py("11b", "Write preprocessing cache", *cache_command("write"))
        else:
            units = json.loads(f("units").read_text(encoding="utf-8"))

        py("11c", "Audit existing tree conflicts", "-m", "src.rules.existing_tree_audit",
           f("normalized"), f("constraints"), f("reconstructed"), f("zone_report"),
           "--output", f("existing_tree_audit"),
           "--report", f("existing_tree_report"))
        planting: list[str | Path] = [
            "-m", "src.planting.service", f("zones"), f("zone_report"),
            f("normalized"), f("constraints"), "--utilities", f("reconstructed"),
            "--config", ROOT / "config/planting.json", "--output", f("plan"),
            "--decisions-output", f("decisions"), "--report", f("plan_report"),
            "--explanations-output", f("explanations"), "--layout-trace-output", f("layout_trace"),
        ]
        if request:
            planting += ["--request", request]
        else:
            planting += ["--preset", args.preset]
            if args.tree_spacing_m is not None:
                planting += ["--tree-spacing-m", str(args.tree_spacing_m)]
            if args.tree_max_count is not None:
                planting += ["--tree-max-count", str(args.tree_max_count)]
        if survey:
            planting += ["--existing-shrub-survey", survey]
        if args.diagnostic_rejected_max_count is not None:
            planting += ["--diagnostic-rejected-max-count", str(args.diagnostic_rejected_max_count)]
        py("12", "Generate planting plan", *planting)

        debug: list[str | Path] = [
            "-m", "src.rules.plant_allow_zone_debug", f("zones"), f("constraints"),
            f("normalized"), "--dxf-output", f("debug_dxf"),
            "--legend-output", f("debug_legend"),
            "--utility-geometries", f("reconstructed"), "--planting-plan", f("plan"),
            "--planting-decisions", f("decisions"), "--zone-report", f("zone_report"),
            "--existing-tree-audit", f("existing_tree_audit"),
            "--insunits", str(units["insert_units_code"]),
        ]
        if actual_mode in ("fast", "lean"):
            debug += ["--dxf-only"]
        else:
            debug += ["--png-output", f("debug_png"), "--raw-objects", f("objects")]
        py("12b", "Export standalone CAD diagnostics", *debug)

        export: list[str | Path] = [
            "-m", "src.cad_io.dxf_exporter", input_dxf, f("zones"),
            "--constraint-map", f("constraints"), "--planting-plan", f("plan"),
            "--composition-report", f("plan_report"),
            "--existing-tree-audit", f("existing_tree_audit"),
            "--output", result_dxf, "--strict-output",
        ]
        if actual_mode in ("fast", "lean"):
            export += ["--overlay-only", "--insunits", str(units["insert_units_code"])]
        py("13", "Export result DXF", *export)

        verify: list[str | Path] = [
            "scripts/verify_outputs.py", f("constraints"), f("zones"),
            "--planting-plan", f("plan"), "--constraint-report", f("constraint_report"),
            "--zone-report", f("zone_report"), "--plan-report", f("plan_report"),
            "--output", f("verification"),
        ]
        if actual_mode == "full":
            verify += ["--input-dxf", input_dxf, "--output-dxf", result_dxf]
        py("14", "Verify planting plan and DXF", *verify)

        pdf: list[str | Path] = [
            "scripts/generate_pdf_report.py", "--decisions", f("decisions"),
            "--planting-plan", f("plan"), "--plan-report", f("plan_report"),
            "--zone-report", f("zone_report"), "--verification-report", f("verification"),
            "--existing-tree-audit", f("existing_tree_audit"),
            "--input-dxf", input_dxf, "--output", f("pdf"),
        ]
        if actual_mode == "full":
            pdf += ["--preview", f("debug_png")]
        py("15", "Generate PDF report", *pdf)
        py("16", "Generate planting atlas and area schedule",
           "scripts/generate_planting_atlas.py", "--normalized", f("normalized"),
           "--constraints", f("constraints"), "--zones", f("zones"),
           "--planting-plan", f("plan"), "--existing-tree-audit", f("existing_tree_audit"),
           "--input-dxf", input_dxf, "--output", f("atlas"), "--schedule", f("schedule"))
        success = True
        print(f"Pipeline completed ({actual_mode}).\nFinal DXF: {result_dxf}\n"
              f"Diagnostic DXF: {f('debug_dxf')}\nPDF: {f('pdf')}\n"
              f"Verification: {f('verification')}", flush=True)
    finally:
        status = "passed" if locals().get("success", False) else "failed"
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "mode": locals().get("actual_mode", "unknown"),
            "input_dxf": str(input_dxf),
            "total_elapsed_seconds": round(time.monotonic() - started, 6),
            "stages": stages,
        }
        write_json(output / "pipeline_stage_timings.json", report)
        lines = [
            "# Pipeline stage timings", "", f"- Status: `{status}`",
            f"- Mode: `{report['mode']}`",
            f"- Total: {report['total_elapsed_seconds']:.3f} s", "",
            "| Stage | Status | Seconds |", "|---|---|---:|",
        ]
        lines += [
            f"| {item['id']} {item['name']} | {item['status']} | {item['elapsed_seconds']:.3f} |"
            for item in stages
        ]
        (output / "pipeline_stage_timings.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Pipeline failed: {error}") from error
