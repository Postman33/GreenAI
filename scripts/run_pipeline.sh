#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_DXF="${1:-/data/input.dxf}"
OUTPUT_DIR="${2:-/data/output}"
REQUEST_FILE="${3:-}"
DXF_UNITS_PER_METER="${DXF_UNITS_PER_METER:-}"
DATABASE_URL="${DATABASE_URL:-postgresql://admin:admin@postgis:5432/admin}"
EXTRACTOR="${DXF_EXTRACTOR:-/usr/local/bin/dxf_extract_go}"
MODEL_DIR="${UTILITY_DETECTOR_MODEL:-$ROOT/models/utility_detector/latest}"

if [[ ! -f "$INPUT_DXF" ]]; then
  echo "Input DXF does not exist: $INPUT_DXF" >&2
  exit 2
fi
if [[ -n "$REQUEST_FILE" && ! -f "$REQUEST_FILE" ]]; then
  echo "Planting request does not exist: $REQUEST_FILE" >&2
  exit 2
fi
mkdir -p "$OUTPUT_DIR"

objects="$OUTPUT_DIR/extracted_objects.jsonl"
surfaces="$OUTPUT_DIR/surface_candidates_raw.jsonl"
normalized="$OUTPUT_DIR/normalized_objects.geojsonl"
normalization_report="$OUTPUT_DIR/normalization_report.json"
unit_report="$OUTPUT_DIR/dxf_units_report.json"
cleaned="$OUTPUT_DIR/cleaned_utilities.geojsonl"
review_utilities="$OUTPUT_DIR/review_utility_graphics.geojsonl"
rejected_utilities="$OUTPUT_DIR/rejected_utility_graphics.geojsonl"
cleaning_report="$OUTPUT_DIR/utility_cleaning_report.json"
reconstructed="$OUTPUT_DIR/reconstructed_utilities.geojsonl"
inferred_connections="$OUTPUT_DIR/inferred_utility_connections.geojsonl"
review_connections="$OUTPUT_DIR/review_utility_connections.geojsonl"
reconstruction_report="$OUTPUT_DIR/network_reconstruction_report.json"
constraints="$OUTPUT_DIR/constraint_map.geojsonl"
constraint_report="$OUTPUT_DIR/constraint_report.json"
zones="$OUTPUT_DIR/plant_allow_zones.geojsonl"
zone_report="$OUTPUT_DIR/plant_allow_zones_report.json"
plan="$OUTPUT_DIR/planting_plan.geojsonl"
decisions="$OUTPUT_DIR/planting_decisions.geojsonl"
plan_report="$OUTPUT_DIR/planting_plan_report.json"
planting_explanations="$OUTPUT_DIR/planting_explanations.md"
result="$OUTPUT_DIR/result_with_planting_plan.dxf"
verification="$OUTPUT_DIR/verification_report.json"

cd "$ROOT"
echo "[1/12] Detecting and confirming DXF units"
unit_args=(scripts/detect_dxf_units.py "$INPUT_DXF" --output "$unit_report")
if [[ -n "$DXF_UNITS_PER_METER" ]]; then
  unit_args+=(--dxf-units-per-meter "$DXF_UNITS_PER_METER")
fi
python "${unit_args[@]}"
DXF_UNITS_PER_METER="$(python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["dxf_units_per_meter"])' "$unit_report")"

echo "[2/12] Extracting semantic DXF objects"
"$EXTRACTOR" --config src/core/config.yaml --output "$objects" "$INPUT_DXF"

echo "[3/12] Extracting surface candidates"
"$EXTRACTOR" --config src/core/surface_inspector_config.yaml --output "$surfaces" "$INPUT_DXF"

echo "[4/12] Normalizing CAD geometry"
python src/normalizer.py "$objects" --output "$normalized" --report "$normalization_report"

echo "[5/12] Cleaning engineering utilities with ONNX models"
python -m src.detection.utilities.detector predict "$objects" \
  --model "$MODEL_DIR" \
  --output "$cleaned" \
  --review-output "$review_utilities" \
  --rejected-output "$rejected_utilities" \
  --report "$cleaning_report"

echo "[6/12] Reconstructing utility gaps"
python src/network_reconstructor.py "$cleaned" \
  --output "$reconstructed" \
  --inferred-output "$inferred_connections" \
  --review-output "$review_connections" \
  --report "$reconstruction_report" \
  --dxf-units-per-meter "$DXF_UNITS_PER_METER"

echo "[7/12] Building physical constraints"
python src/constraint_builder.py "$normalized" "$surfaces" \
  --output "$constraints" --report "$constraint_report" \
  --unit-metadata "$unit_report"

echo "[8/12] Applying normative plant rules"
DATABASE_URL="$DATABASE_URL" python src/plant_allow_zone.py "$constraints" "$normalized" \
  --output "$zones" \
  --report "$zone_report" \
  --utility-geometries "$reconstructed" \
  --dxf-units-per-meter "$DXF_UNITS_PER_METER" \
  --unit-metadata "$unit_report"

echo "[9/12] Verifying calculated allow zones"
python scripts/verify_outputs.py "$constraints" "$zones" \
  --zone-report "$zone_report" \
  --output "$OUTPUT_DIR/zone_verification_report.json"

echo "[10/12] Generating concrete planting plan"
planting_args=(
  src/planting_service.py "$zones" "$zone_report" "$normalized" "$constraints"
  --utilities "$reconstructed"
  --config nanocad-plugin/config/greenai.plugin.json
  --output "$plan"
  --decisions-output "$decisions"
  --report "$plan_report"
  --explanations-output "$planting_explanations"
)
if [[ -n "$REQUEST_FILE" ]]; then
  planting_args+=(--request "$REQUEST_FILE")
else
  planting_args+=(--preset "${PLANTING_PRESET:-dense_mixed}")
fi
python "${planting_args[@]}"

echo "[11/12] Exporting dedicated result layers to DXF"
python src/dxf_exporter.py "$INPUT_DXF" "$zones" \
  --constraint-map "$constraints" \
  --planting-plan "$plan" \
  --output "$result" \
  --strict-output

echo "[12/12] Verifying plan and source-DXF preservation"
python scripts/verify_outputs.py "$constraints" "$zones" \
  --planting-plan "$plan" \
  --zone-report "$zone_report" \
  --plan-report "$plan_report" \
  --input-dxf "$INPUT_DXF" \
  --output-dxf "$result" \
  --output "$verification"

echo "Pipeline completed"
echo "DXF: $result"
echo "Plant explanations: $plan"
echo "Readable planting passports: $planting_explanations"
echo "Point decisions: $decisions"
echo "Verification: $verification"

