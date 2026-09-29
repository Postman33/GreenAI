"""Read-only readiness check for an existing full Sylvitect-core demo run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "result_with_planting_plan.dxf",
    "planting_diagnostics.dxf",
    "sylvitect_planting_report.pdf",
    "planting_plan_atlas.pdf",
    "planting_explanations.md",
    "planting_plan_report.json",
    "verification_report.json",
    "pipeline_run_parameters.json",
    "pipeline_stage_timings.json",
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check(directory: Path) -> list[str]:
    problems = [f"Нет файла: {name}" for name in REQUIRED
                if not (directory / name).is_file() or (directory / name).stat().st_size == 0]
    if problems:
        return problems

    verification = read_json(directory / "verification_report.json")
    plan = read_json(directory / "planting_plan_report.json")
    run = read_json(directory / "pipeline_run_parameters.json")
    counts = plan.get("summary", {})
    points = verification.get("planting_plan_checks", {})
    dxf = verification.get("dxf_checks", {})
    road = verification.get("road_quality", {})

    trees = counts.get("auto_tree", {}).get("accepted_count", 0)
    shrubs = counts.get("auto_shrub", {}).get("accepted_count", 0)
    areas = counts.get("auto_herbaceous", {}).get("accepted_area_count", 0)
    total = trees + shrubs + areas
    print(f"Результат: {directory}")
    print(f"Посадки: {trees} деревьев, {shrubs} кустарников, {areas} участков покрытия; всего {total}.")
    print(f"Проверка: {verification.get('status')}; режим: {run.get('actual_pipeline_mode')}.")
    print(f"Исходные сущности DXF: {dxf.get('preserved_source_entity_count')} из {dxf.get('input_modelspace_entity_count')}.")
    print(f"Дорога: {road.get('status')}; требуется визуальное подтверждение: {road.get('requires_visual_confirmation')}.")

    if verification.get("status") != "passed" or plan.get("status") != "passed":
        problems.append("Итоговый статус плана или проверки не passed.")
    if run.get("actual_pipeline_mode") != "full":
        problems.append("Этот каталог не содержит подтверждённый полный прогон.")
    if total != points.get("unique_placement_id_count"):
        problems.append("Сумма посадок не совпадает с независимой проверкой.")
    if trees + shrubs != points.get("point_placement_count") or areas != points.get("area_placement_count"):
        problems.append("Число точек или площадей не совпадает с проверкой.")
    if dxf.get("preserved_source_entity_count") != dxf.get("input_modelspace_entity_count"):
        problems.append("Не все исходные сущности DXF сохранены.")
    for field in ("point_failures", "area_failures", "missing_explanation_count",
                  "missing_npa_reference_count", "measured_distance_failure_count",
                  "same_type_spacing_failure_count", "cross_type_spacing_failure_count"):
        if points.get(field) != 0:
            problems.append(f"Проверка {field}: {points.get(field)!r}, ожидалось 0.")
    for field in ("missing_source_entity_count", "changed_source_content_count",
                  "missing_source_handle_count"):
        if dxf.get(field) != 0:
            problems.append(f"DXF {field}: {dxf.get(field)!r}, ожидалось 0.")
    for layer in ("SYLVITECT_PLANT_TREE", "SYLVITECT_PLANT_SHRUB", "SYLVITECT_HERBACEOUS"):
        state = dxf.get("sylvitect_layer_states", {}).get(layer)
        if state is None or state.get("is_off") or state.get("is_frozen"):
            problems.append(f"Слой результата недоступен: {layer}.")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", nargs="?", type=Path,
                        default=ROOT / "output/latest_demo")
    args = parser.parse_args()
    directory = args.output_directory.resolve()
    if not directory.is_dir():
        parser.error(f"Каталог результата не найден: {directory}")
    problems = check(directory)
    if problems:
        print("НЕ ГОТОВО:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Готово к показу отчётов. Открытие DXF и дороги проверьте в CAD отдельно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
