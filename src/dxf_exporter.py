"""Add calculated planting zones to a copy of the original DXF."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import ezdxf
from ezdxf.colors import float2transparency
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, shape
from shapely.ops import unary_union
from shapely.validation import make_valid


Polygonal = Polygon | MultiPolygon
ZONE_LAYERS = {
    "tree": ("GREEN_AI_ZONE_TREE", 3),
    "shrub": ("GREEN_AI_ZONE_SHRUB", 2),
    "herbaceous": ("GREEN_AI_ZONE_HERBACEOUS", 4),
    "groundcover": ("GREEN_AI_ZONE_GROUNDCOVER", 6),
}
ROAD_LAYER = ("GREEN_AI_RECONSTRUCTED_ROAD", 8)
PLANTING_LAYERS = {
    "tree": ("GREEN_AI_PLANT_TREE", 3),
    "shrub": ("GREEN_AI_PLANT_SHRUB", 2),
    "herbaceous": ("GREEN_AI_HERBACEOUS", 94),
}
GREEN_AI_APPID = "GREEN_AI"


CHECK_TITLES = {
    "ALLOWED_ZONE": "Точка находится вне итоговой допустимой зоны",
    "PLANT_FOOTPRINT_INSIDE_ZONE": "Крона растения не помещается в допустимой зоне",
    "NEW_PLANT_SPACING": "Недостаточное расстояние до новой посадки",
    "EXISTING_TREE_CLEARANCE": "Недостаточное расстояние до существующего дерева",
    "EXISTING_TREE_BELT_CLEARANCE": "Недостаточное расстояние до существующей древесной полосы",
}

TARGET_TITLES = {
    "building": "здания",
    "sidewalk": "тротуара",
    "road_edge": "проезжей части",
    "water_pipe": "водопровода",
    "gas_pipe": "газопровода",
    "heat_pipe": "теплосети",
    "sewer_pipe": "канализации",
    "storm_drain": "ливневой канализации",
    "power_cable": "силового кабеля",
    "telecom_cable": "кабеля связи",
    "overhead_power_line": "воздушной ЛЭП",
    "existing_tree": "существующего дерева",
    "existing_tree_belt": "существующей древесной полосы",
    "tree": "другой новой посадки",
    "shrub": "другой новой посадки",
}


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _check_title(check: dict[str, Any]) -> str:
    code = str(check.get("code") or "CHECK")
    if code in CHECK_TITLES:
        return CHECK_TITLES[code]
    target = str(check.get("target") or "ограничения")
    target_title = TARGET_TITLES.get(target, target.replace("_", " "))
    return f"Нарушен требуемый отступ от {target_title}"


def _check_advice(check: dict[str, Any], deficit: float | None) -> str:
    code = str(check.get("code") or "")
    target = str(check.get("target") or "")
    amount = f" минимум на {deficit:.2f} м" if deficit is not None and deficit > 0 else ""
    if code == "ALLOWED_ZONE":
        return "Выберите точку внутри зелёной допустимой зоны; конкретное ограничение указано ниже."
    if code == "PLANT_FOOTPRINT_INSIDE_ZONE":
        return (
            f"Сдвиньте центр растения внутрь допустимой зоны{amount} либо выберите "
            "растение с меньшим радиусом кроны."
        )
    if code == "NEW_PLANT_SPACING":
        return f"Разнесите центры новых посадок{amount}."
    if code in {"EXISTING_TREE_CLEARANCE", "EXISTING_TREE_BELT_CLEARANCE"}:
        return f"Сдвиньте точку дальше от существующей растительности{amount}."
    target_title = TARGET_TITLES.get(target, target.replace("_", " "))
    return f"Сдвиньте точку дальше от {target_title}{amount}."


def build_failure_details(properties: dict[str, Any]) -> list[dict[str, str]]:
    """Return complete, human-readable explanations for failed checks."""
    result: list[dict[str, str]] = []
    failed = [
        check for check in properties.get("checks", []) if check.get("status") == "failed"
    ]
    # ALLOWED_ZONE is the aggregate result of the concrete checks below it.
    # Showing it as a second cause makes the passport look contradictory.
    if any(str(check.get("code")) != "ALLOWED_ZONE" for check in failed):
        failed = [check for check in failed if str(check.get("code")) != "ALLOWED_ZONE"]
    for check in failed:
        actual = _finite_number(check.get("actual_distance_m"))
        required = _finite_number(check.get("required_distance_m"))
        deficit = (
            max(0.0, required - actual)
            if actual is not None and required is not None
            else None
        )
        if actual is not None and required is not None:
            metric = (
                f"Фактически {actual:.2f} м; требуется {required:.2f} м; "
                f"не хватает {deficit:.2f} м."
            )
        elif required is not None:
            metric = f"Требуемое расстояние или радиус: {required:.2f} м."
        elif actual is not None:
            metric = f"Измеренное расстояние: {actual:.2f} м."
        else:
            metric = "Численное расстояние для этой проверки не определено."
        result.append(
            {
                "code": str(check.get("code") or "CHECK"),
                "title": _check_title(check),
                "metric": metric,
                "detail": str(check.get("explanation") or "").strip(),
                "advice": _check_advice(check, deficit),
                "norm": str(check.get("norm_reference") or "").strip(),
            }
        )
    return result


def polygon_parts(geometry: Any) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def load_plant_zones(path: Path) -> dict[str, Polygonal]:
    """Read and merge plant_allow_zone features by plant class."""
    grouped: dict[str, list[Polygon]] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid plant-zone GeoJSON"
                ) from error
            properties = feature.get("properties", {})
            if properties.get("object_type") != "plant_allow_zone":
                continue
            plant_type = properties.get("plant_type")
            if not plant_type:
                raise ValueError(f"Line {line_number}: plant_type is missing")
            geometry = make_valid(shape(feature["geometry"]))
            grouped.setdefault(plant_type, []).extend(polygon_parts(geometry))

    if not grouped:
        raise ValueError("No plant_allow_zone features were found")
    return {
        plant_type: unary_union(parts)
        for plant_type, parts in grouped.items()
    }


def load_constraint_geometry(path: Path, object_type: str) -> Polygonal:
    """Read and merge one polygonal object type from a GeoJSONL map."""
    parts: list[Polygon] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid constraint GeoJSON"
                ) from error
            if feature.get("properties", {}).get("object_type") != object_type:
                continue
            parts.extend(polygon_parts(make_valid(shape(feature["geometry"]))))
    if not parts:
        raise ValueError(f"No {object_type} geometry found in {path}")
    return unary_union(parts)


def load_planting_plan(path: Path) -> list[dict[str, Any]]:
    """Read concrete point and area planting features."""
    features: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature = json.loads(line)
                object_type = feature.get("properties", {}).get("object_type")
                if object_type not in {"proposed_planting", "proposed_planting_area"}:
                    continue
                feature["_geometry"] = make_valid(shape(feature["geometry"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Line {line_number}: invalid planting-plan feature") from error
            features.append(feature)
    if not features:
        raise ValueError(f"No proposed planting features were found in {path}")
    return features


def ensure_layer(document: ezdxf.document.Drawing, name: str, color: int) -> None:
    if name in document.layers:
        layer = document.layers.get(name)
        layer.color = color
        layer.on()
        layer.thaw()
    else:
        document.layers.add(name, color=color, lineweight=50)


def remove_previous_entities(modelspace, layer_name: str) -> int:
    """Make repeated export to an already generated DXF idempotent."""
    removed = 0
    for entity in list(modelspace.query(f'*[layer=="{layer_name}"]')):
        modelspace.delete_entity(entity)
        removed += 1
    return removed


def ring_vertices(ring) -> list[tuple[float, float]]:
    coordinates = [(float(x), float(y)) for x, y, *_ in ring.coords]
    if len(coordinates) > 1 and coordinates[0] == coordinates[-1]:
        coordinates.pop()
    return coordinates


def add_zone_polygon(
    modelspace,
    polygon: Polygon,
    layer_name: str,
    color: int,
    transparency: float,
    properties: dict[str, Any] | None = None,
) -> int:
    exterior = ring_vertices(polygon.exterior)
    if len(exterior) < 3:
        return 0

    hatch = modelspace.add_hatch(
        color=color,
        dxfattribs={
            "layer": layer_name,
            "transparency": float2transparency(transparency),
        },
    )
    hatch.set_solid_fill(color=color)
    hatch.paths.add_polyline_path(exterior, is_closed=True, flags=1)
    for interior in polygon.interiors:
        hole = ring_vertices(interior)
        if len(hole) >= 3:
            hatch.paths.add_polyline_path(hole, is_closed=True, flags=0)

    if properties is not None:
        attach_planting_metadata(hatch, properties)

    outline = modelspace.add_lwpolyline(
        exterior,
        close=True,
        dxfattribs={"layer": layer_name, "color": color, "lineweight": 50},
    )
    if properties is not None:
        attach_planting_metadata(outline, properties)
    for interior in polygon.interiors:
        hole = ring_vertices(interior)
        if len(hole) >= 3:
            modelspace.add_lwpolyline(
                hole,
                close=True,
                dxfattribs={
                    "layer": layer_name,
                    "color": color,
                    "lineweight": 50,
                },
            )
    return 1


def attach_planting_metadata(entity: Any, properties: dict[str, Any]) -> None:
    """Attach identity and complete rejection diagnostics for GREENAI_INSPECT."""
    failed_checks = ",".join(str(value) for value in properties.get("failed_checks", []))
    rejection_reasons = properties.get("rejection_reasons", [])
    specific_reasons = [
        str(check.get("explanation") or "")
        for check in properties.get("checks", [])
        if check.get("status") == "failed" and check.get("code") != "ALLOWED_ZONE"
    ]
    primary_reason = "; ".join(value for value in specific_reasons if value)
    if not primary_reason and rejection_reasons:
        primary_reason = str(rejection_reasons[0])
    values = [
        (1000, f"id={properties.get('planting_id', '')}"),
        (1000, f"type={properties.get('plant_type', '')}"),
        (1000, f"species={properties.get('species', '')}"[:250]),
        (1000, f"status={properties.get('status', '')}"),
        (1000, f"zone={properties.get('zone_handle', '')}"),
        (1000, f"failed={failed_checks}"[:250]),
        (1000, f"reason={primary_reason}"[:250]),
    ]
    for index, failure in enumerate(build_failure_details(properties), start=1):
        for key in ("code", "title", "metric", "detail", "advice", "norm"):
            value = failure.get(key, "")
            if value:
                values.append((1000, f"{key}_{index}={value}"[:250]))
    manual_checks = [
        str(value) for value in properties.get("manual_review_checks", []) if value
    ]
    if manual_checks:
        values.append((1000, f"manual={','.join(manual_checks)}"[:250]))
    entity.set_xdata(GREEN_AI_APPID, values)


def add_point_planting(
    modelspace: Any,
    point: Point,
    properties: dict[str, Any],
    layer_name: str,
    color: int,
) -> None:
    units_per_meter = float(properties.get("dxf_units_per_meter", 1.0))
    radius = max(0.05, float(properties.get("symbol_radius_m", 0.25)) * units_per_meter)
    circle = modelspace.add_circle(
        (float(point.x), float(point.y)),
        radius,
        dxfattribs={"layer": layer_name, "color": color, "lineweight": 50},
    )
    attach_planting_metadata(circle, properties)


def export_zones(
    input_dxf: Path,
    zones_path: Path,
    output_dxf: Path,
    transparency: float,
    constraint_map_path: Path | None = None,
    planting_plan_path: Path | None = None,
    allow_version_fallback: bool = True,
    show_analysis_layers: bool | None = None,
) -> None:
    if input_dxf.resolve() == output_dxf.resolve():
        raise ValueError("Output DXF must differ from the original input DXF")
    zones = load_plant_zones(zones_path)
    document = ezdxf.readfile(input_dxf)
    modelspace = document.modelspace()
    original_entity_count = len(modelspace)
    if show_analysis_layers is None:
        # A zone-only/debug export is meant for inspecting the calculation.
        # A final planting plan should open as a readable design, while the
        # supporting zones remain available in the layer manager.
        show_analysis_layers = planting_plan_path is None
    exported: dict[str, dict[str, Any]] = {}
    if GREEN_AI_APPID not in document.appids:
        document.appids.add(GREEN_AI_APPID)

    if constraint_map_path is not None:
        road_area = load_constraint_geometry(constraint_map_path, "road_area")
        road_layer_name, road_color = ROAD_LAYER
        ensure_layer(document, road_layer_name, road_color)
        removed = remove_previous_entities(modelspace, road_layer_name)
        road_polygon_count = sum(
            add_zone_polygon(
                modelspace,
                polygon,
                road_layer_name,
                road_color,
                min(0.82, max(transparency, 0.72)),
            )
            for polygon in polygon_parts(road_area)
        )
        exported["reconstructed_road"] = {
            "layer": road_layer_name,
            "polygons": road_polygon_count,
            "area_in_dxf_square_units": road_area.area,
            "previous_entities_removed": removed,
        }

    # Add larger shrub zones first so tree zones remain visible above them.
    order = ["shrub", "tree", "herbaceous", "groundcover"]
    order.extend(sorted(set(zones) - set(order)))
    for plant_type in order:
        geometry = zones.get(plant_type)
        if geometry is None or geometry.is_empty:
            continue
        layer_name, color = ZONE_LAYERS.get(
            plant_type,
            (f"GREEN_AI_ZONE_{plant_type.upper()}", 3),
        )
        ensure_layer(document, layer_name, color)
        removed = remove_previous_entities(modelspace, layer_name)
        polygon_count = sum(
            add_zone_polygon(
                modelspace,
                polygon,
                layer_name,
                color,
                transparency,
            )
            for polygon in polygon_parts(geometry)
        )
        exported[plant_type] = {
            "layer": layer_name,
            "polygons": polygon_count,
            "area_in_dxf_square_units": geometry.area,
            "previous_entities_removed": removed,
        }

    if planting_plan_path is not None:
        plan_features = load_planting_plan(planting_plan_path)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for feature in plan_features:
            grouped.setdefault(feature["properties"]["plant_type"], []).append(feature)
        for plant_type, features in grouped.items():
            layer_name, color = PLANTING_LAYERS.get(
                plant_type,
                (f"GREEN_AI_PLANT_{plant_type.upper()}", 3),
            )
            ensure_layer(document, layer_name, color)
            removed = remove_previous_entities(modelspace, layer_name)
            point_count = 0
            polygon_count = 0
            area = 0.0
            for feature in features:
                geometry = feature["_geometry"]
                properties = feature["properties"]
                if properties["object_type"] == "proposed_planting":
                    if not isinstance(geometry, Point):
                        raise ValueError(f"{feature.get('id')}: point planting is not a Point")
                    add_point_planting(modelspace, geometry, properties, layer_name, color)
                    point_count += 1
                else:
                    for polygon in polygon_parts(geometry):
                        polygon_count += add_zone_polygon(
                            modelspace,
                            polygon,
                            layer_name,
                            color,
                            min(0.82, max(transparency, 0.72)),
                            properties,
                        )
                        area += polygon.area
            exported[f"planting_{plant_type}"] = {
                "layer": layer_name,
                "points": point_count,
                "polygons": polygon_count,
                "area_in_dxf_square_units": area,
                "previous_entities_removed": removed,
            }

    if planting_plan_path is not None and not show_analysis_layers:
        analysis_layer_names = {ROAD_LAYER[0]}
        analysis_layer_names.update(layer_name for layer_name, _color in ZONE_LAYERS.values())
        for layer_name in analysis_layer_names:
            if layer_name in document.layers:
                document.layers.get(layer_name).off()

    candidates = [output_dxf]
    if allow_version_fallback:
        candidates.extend(
            output_dxf.with_name(f"{output_dxf.stem}_v{version}{output_dxf.suffix}")
            for version in range(2, 100)
        )
    actual_output = output_dxf
    last_error: PermissionError | None = None
    for candidate in candidates:
        try:
            document.saveas(candidate)
            actual_output = candidate
            if candidate != output_dxf:
                print(
                    f"WARNING: {output_dxf} is locked; "
                    f"result DXF written to {candidate}"
                )
            break
        except PermissionError as error:
            last_error = error
    else:
        assert last_error is not None
        raise last_error
    print(f"Original DXF: {input_dxf}")
    print(f"Plant zones: {zones_path}")
    if planting_plan_path is not None:
        print(f"Planting plan: {planting_plan_path}")
    if constraint_map_path is not None:
        print(f"Constraint map: {constraint_map_path}")
    print(f"Output DXF: {actual_output}")
    print(f"Original modelspace entities: {original_entity_count}")
    for plant_type, result in exported.items():
        details = []
        if "points" in result:
            details.append(f"{result['points']} point(s)")
        details.append(f"{result.get('polygons', 0)} polygon(s)")
        details.append(f"area {result['area_in_dxf_square_units']:.3f}")
        print(f"  {plant_type}: {result['layer']} | " + " | ".join(details))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add plant allow-zone layers to a copy of the original DXF."
    )
    parser.add_argument("input_dxf", type=Path)
    parser.add_argument("plant_allow_zones_geojsonl", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("result_with_plant_allow_zones.dxf"),
    )
    parser.add_argument(
        "--constraint-map",
        type=Path,
        help="Optional constraint GeoJSONL; adds reconstructed road layer.",
    )
    parser.add_argument(
        "--planting-plan",
        type=Path,
        help="Optional concrete planting-plan GeoJSONL; writes tree/shrub/coverage layers.",
    )
    parser.add_argument(
        "--strict-output",
        action="store_true",
        help="Fail when --output is locked instead of silently writing a versioned file.",
    )
    parser.add_argument(
        "--show-analysis-layers",
        action="store_true",
        help=(
            "Keep reconstructed roads and allow-zone layers visible in a final plan. "
            "By default they remain in the DXF but open switched off."
        ),
    )
    parser.add_argument(
        "--transparency",
        type=float,
        default=0.65,
        help="Zone fill transparency from 0 (opaque) to 1 (invisible).",
    )
    args = parser.parse_args()
    if not 0 <= args.transparency < 1:
        raise SystemExit("--transparency must be between 0 inclusive and 1 exclusive")
    try:
        export_zones(
            args.input_dxf,
            args.plant_allow_zones_geojsonl,
            args.output,
            args.transparency,
            args.constraint_map,
            args.planting_plan,
            not args.strict_output,
            True if args.show_analysis_layers else None,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"DXF export error: {error}") from error


if __name__ == "__main__":
    main()
