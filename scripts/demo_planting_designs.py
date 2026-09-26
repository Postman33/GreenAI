"""Generate a small, reproducible DXF/PNG comparison without a database.

The geometry and catalogue are synthetic test inputs, not a surveyed street.
Run from any directory; the actual planting service and DXF exporter are used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, PathPatch
from matplotlib.path import Path as MplPath
from shapely import affinity
from shapely.geometry import LineString, Point, box, mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from src.cad_io.dxf_exporter import export_zones
from src.planting.placement_generator import polygon_parts
from src.planting.service import plan


STYLES = ("alley", "hedge", "shrub_mass", "free_group", "mixed_flowerbed")
TITLES = ("Аллея · два ряда", "Живая изгородь · полоса 2 м", "Массив кустарников", "Свободные группы деревьев", "Цветник · доли площади 40 / 40 / 20")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path, values):
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")


def feature(kind, geometry, **properties):
    return {"type": "Feature", "properties": {"object_type": kind, **properties}, "geometry": mapping(geometry)}


def draw_polygon(ax, polygon, color, offset, alpha=1.0):
    polygon = orient(affinity.translate(polygon, yoff=-offset))
    vertices, codes = [], []
    for ring in (polygon.exterior, *polygon.interiors):
        coords = list(ring.coords)
        vertices.extend(coords)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(coords) - 2) + [MplPath.CLOSEPOLY])
    ax.add_patch(PathPatch(MplPath(vertices, codes), facecolor=color, edgecolor="#405766", linewidth=.65, alpha=alpha))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/design_modes_demo")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "config/planting.json").read_text(encoding="utf-8-sig"))
    config["diagnosticRejectedMaxCount"] = 0
    profiles = {p["plantType"]: p for p in config["plantingProfiles"]}
    mixture = config["plantingDesignDefaults"]["mixed_flowerbed"]["composition"]
    catalog = {kind: [{"id": i, "name": item["species"]}] for i, (kind, item) in enumerate(profiles.items())}
    catalog["herbaceous"] += [{"id": 100 + i, "name": part["species"]} for i, part in enumerate(mixture)]
    areas, selections = [], []
    for i, style in enumerate(STYLES):
        y = i * 45
        # A central obstacle demonstrates that lines and masses retain holes.
        area = box(0, y, 90, y + 30).difference(box(40, y + 10, 48, y + 20))
        areas.append(area)
        kind = "tree" if style in {"alley", "free_group"} else "herbaceous" if style == "mixed_flowerbed" else "shrub"
        selection = {
            "id": style, "plant_type": kind, "species": profiles[kind]["species"],
            "design_style": style, "area": mapping(area),
        }
        if style in {"alley", "hedge"}:
            selection["guide"] = mapping(LineString([(0, y + 15), (90, y + 15)]))
        if style == "alley":
            selection["row_count"] = 2
        elif style == "hedge":
            selection["band_width_m"] = 2
        elif style == "free_group":
            selection["seed"] = 7
        elif style == "mixed_flowerbed":
            selection["composition"] = mixture
            selection["species"] = mixture[0]["species"]
        selections.append(selection)
        if style != "mixed_flowerbed":
            selections.append({"id": f"lawn_{style}", "plant_type": "herbaceous", "species": profiles["herbaceous"]["species"], "area": mapping(area)})
    base = unary_union(areas)
    write_json(output / "config.json", config)
    write_json(output / "request.json", {"selections": selections})
    write_json(output / "zone_report.json", {"dxf_units_per_meter": 1, "plant_catalog": catalog, "plant_types": {"tree": {"rules": []}, "shrub": {"rules": []}}, "note": "Synthetic design demo; no surveyed utility rules"})
    write_jsonl(output / "zones.geojsonl", [feature("plant_allow_zone", base, plant_type=kind) for kind in ("tree", "shrub")])
    write_jsonl(output / "constraints.geojsonl", [feature("base_allowed_area", base)])
    write_jsonl(output / "normalized.geojsonl", [])
    report = plan(
        output / "zones.geojsonl", output / "zone_report.json", output / "normalized.geojsonl",
        output / "constraints.geojsonl", output / "unused_utilities.geojsonl", output / "config.json",
        output / "plan.geojsonl", output / "report.json", output / "decisions.geojsonl",
        output / "request.json", output / "explanations.md",
    )
    export_zones(output / "unused.dxf", output / "zones.geojsonl", output / "design_modes.dxf", .5,
                 planting_plan_path=output / "plan.geojsonl", overlay_only=True, insunits=6)
    document = ezdxf.readfile(output / "design_modes.dxf")
    document.layers.add("DEMO_LABELS", color=7)
    for i, style in enumerate(STYLES):
        document.modelspace().add_text(f"{i + 1}. {style} (synthetic demo)", dxfattribs={"layer": "DEMO_LABELS", "height": 2, "insert": (0, i * 45 + 32)})
    document.saveas(output / "design_modes.dxf")
    if document.audit().has_errors:
        raise RuntimeError("Demo DXF failed ezdxf audit")
    features = [json.loads(line) for line in (output / "plan.geojsonl").read_text(encoding="utf-8").splitlines()]
    flower_colors = {part["species"]: color for part, color in zip(mixture, ("#e8b75a", "#b294cd", "#84afa5"))}
    fig, axes = plt.subplots(5, 1, figsize=(12, 17), facecolor="#f3f6f8")
    fig.suptitle("Пять режимов посадки\nСинтетический участок 90 × 30 м; вырез — препятствие", fontsize=18, y=.995, color="#163446")
    for i, (style, ax) in enumerate(zip(STYLES, axes)):
        ax.set_facecolor("#dfe6eb")
        draw_polygon(ax, areas[i], "#edf1ed", i * 45)
        for f in sorted(features, key=lambda f: f["geometry"]["type"] == "Point"):
            props = f["properties"]
            if props["request_id"] not in {style, f"lawn_{style}"}:
                continue
            geometry = shape(f["geometry"])
            if isinstance(geometry, Point):
                ax.add_patch(Circle((geometry.x, geometry.y - i * 45), props["footprint_radius_m"], facecolor="#3d9569", edgecolor="#165339", linewidth=1))
            else:
                color = flower_colors[props["species"]] if style == "mixed_flowerbed" else "#75a552" if props["plant_type"] == "shrub" else "#dce8cb"
                for polygon in polygon_parts(geometry):
                    draw_polygon(ax, polygon, color, i * 45)
        summary = report["summary"][style]
        amount = f"{summary['accepted_count']} деревьев" if "accepted_count" in summary else f"{summary['accepted_area_in_dxf_square_units']:.0f} м²"
        ax.set_title(f"{i + 1}. {TITLES[i]}  ·  {amount}", loc="left", fontsize=13, pad=10, color="#163446")
        ax.set_xlim(-1, 91)
        ax.set_ylim(-1, 31)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
    fig.tight_layout(rect=(0, .025, 1, .96))
    fig.text(.5, .012, "Цветник: цвет обозначает вид; доли относятся к площади. Ассортимент в примере демонстрационный.", ha="center", fontsize=10)
    fig.savefig(output / "preview.png", dpi=140)
    plt.close(fig)
    print(f"Preview: {output / 'preview.png'}")
    print(f"DXF: {output / 'design_modes.dxf'}")


if __name__ == "__main__":
    main()
