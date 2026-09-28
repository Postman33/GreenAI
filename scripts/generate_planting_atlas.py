"""Create an illustrated, area-accounted planting plan from pipeline geometry."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle as PlotCircle
from matplotlib.patches import Polygon as PlotPolygon
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from shapely.affinity import rotate
from shapely.geometry import box, shape
from shapely.ops import unary_union

try:
    from scripts.generate_pdf_report import register_fonts
except ModuleNotFoundError:
    from generate_pdf_report import register_fonts


PAGE = landscape(A4)
PALETTE = {
    "road": "#D6DDE1", "building": "#83909B", "hard": "#E5B9AF",
    "tree_zone": "#B9DDBD", "shrub_zone": "#F5DEA0",
    "tree": "#2D7849", "shrub": "#DA9638", "lawn": "#A9DDA7",
    "water": "#2183B5", "heat": "#BC5D44", "power": "#8464AC",
    "boundary": "#243E55", "existing_tree": "#648266",
    "existing_tree_conflict": "#C56B17",
}


def read_geometries(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            feature = json.loads(line)
            props = feature.get("properties", {})
            key = props.get("plant_type") if props.get("object_type") == "plant_allow_zone" else props.get("object_type")
            if key and feature.get("geometry"):
                result[str(key)] = shape(feature["geometry"])
    return result


def read_plan(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    points, areas = [], []
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            feature = json.loads(line)
            props = feature.get("properties", {})
            item = {"geometry": shape(feature["geometry"]), "properties": props}
            if props.get("object_type") == "proposed_planting" and item["geometry"].geom_type == "Point":
                points.append(item)
            elif props.get("object_type") == "proposed_planting_area":
                areas.append(item)
    return points, areas


def read_existing_tree_conflicts(path: Path) -> list[Any]:
    points: list[Any] = []
    with path.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            if feature.get("properties", {}).get("status") != "conflict":
                continue
            point = shape(feature["geometry"])
            if point.geom_type != "Point":
                raise ValueError(f"{path}:{line_number}: expected tree conflict Point")
            points.append(point)
    return points


def polygons(geometry: Any):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygons(part)


def lines(geometry: Any):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type in {"LineString", "LinearRing"}:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from lines(part)


def points_in(geometry: Any):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "Point":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from points_in(part)


def paint_geometry(ax: Any, geometry: Any, *, face: str | None = None,
                   edge: str | None = None, alpha: float = 1.0, width: float = 0.8) -> None:
    if geometry is None or geometry.is_empty:
        return
    for polygon in polygons(geometry):
        ax.add_patch(PlotPolygon(
            list(polygon.exterior.coords), closed=True,
            facecolor=face or "none", edgecolor=edge or "none",
            linewidth=width, alpha=alpha, zorder=2,
        ))
        if face:
            for interior in polygon.interiors:
                ax.add_patch(PlotPolygon(
                    list(interior.coords), closed=True,
                    facecolor="white", edgecolor=edge or "none", linewidth=width * 0.5,
                    zorder=3,
                ))
    if edge:
        for line in lines(geometry):
            x, y = line.xy
            ax.plot(x, y, color=edge, linewidth=width, alpha=alpha, zorder=4)
    for point in points_in(geometry):
        ax.plot(point.x, point.y, marker="o", color=edge or face or "#555", markersize=2, zorder=4)


def clipped(geometry: Any, viewport: Any) -> Any:
    if geometry is None or geometry.is_empty or not box(*geometry.bounds).intersects(viewport):
        return None
    return geometry.intersection(viewport)


def render_view(path: Path, viewport: Any, kind: str, source: dict[str, Any],
                constraints: dict[str, Any], zones: dict[str, Any],
                points: list[dict[str, Any]], lawn: Any, *, label: str = "",
                existing_tree_conflicts: list[Any] | None = None,
                dxf_units_per_meter: float = 1.0) -> None:
    fig, ax = plt.subplots(figsize=(4.3, 4.3), dpi=150)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#FAFCFC")
    paint_geometry(ax, clipped(source.get("work_boundary"), viewport), edge=PALETTE["boundary"], width=1.1)
    paint_geometry(ax, clipped(constraints.get("road_area"), viewport), face=PALETTE["road"])
    paint_geometry(ax, clipped(source.get("building"), viewport), face=PALETTE["building"])
    if kind == "context":
        for source_key, color in (("road_edge", "#65727A"), ("water_pipe", PALETTE["water"]),
                                  ("heat_pipe", PALETTE["heat"]), ("power_cable", PALETTE["power"])):
            paint_geometry(ax, clipped(source.get(source_key), viewport), edge=color, width=0.55)
        paint_geometry(ax, clipped(source.get("utility_well"), viewport), edge="#684E66")
        for tree in points_in(clipped(source.get("existing_tree"), viewport)):
            ax.add_patch(PlotCircle((tree.x, tree.y), 1.0, fill=False,
                                    edgecolor=PALETTE["existing_tree"], linewidth=0.6, zorder=5))
        for tree in existing_tree_conflicts or []:
            if viewport.covers(tree):
                ax.add_patch(PlotCircle(
                    (tree.x, tree.y), 1.35 * dxf_units_per_meter, fill=False,
                    edgecolor=PALETTE["existing_tree_conflict"], linewidth=1.3, zorder=9,
                ))
    elif kind == "constraints":
        paint_geometry(ax, clipped(constraints.get("hard_surface_area"), viewport), face=PALETTE["hard"], alpha=0.65)
        paint_geometry(ax, clipped(zones.get("shrub"), viewport), face=PALETTE["shrub_zone"], alpha=0.7)
        paint_geometry(ax, clipped(zones.get("tree"), viewport), face=PALETTE["tree_zone"], alpha=0.75)
        paint_geometry(ax, clipped(source.get("water_pipe"), viewport), edge=PALETTE["water"], width=0.5)
        paint_geometry(ax, clipped(source.get("heat_pipe"), viewport), edge=PALETTE["heat"], width=0.5)
    elif kind == "planting":
        paint_geometry(ax, clipped(lawn, viewport), face=PALETTE["lawn"], alpha=0.8)
        for item in points:
            point = item["geometry"]
            props = item["properties"]
            radius = float(props.get("footprint_radius_m") or 0) * float(props.get("dxf_units_per_meter") or 1)
            if props.get("plant_type") == "tree":
                ax.add_patch(PlotCircle((point.x, point.y), radius, fill=False,
                                        edgecolor=PALETTE["tree"], linewidth=1.0, zorder=7))
                ax.plot(point.x, point.y, marker="o", markersize=2, color=PALETTE["tree"], zorder=8)
            elif props.get("plant_type") == "shrub":
                ax.add_patch(PlotCircle((point.x, point.y), max(radius, 0.2),
                                        facecolor=PALETTE["shrub"], edgecolor="none", alpha=0.65, zorder=6))
    x0, y0, x1, y1 = viewport.bounds
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(color="#E7ECEF", linewidth=0.4)
    ax.tick_params(labelsize=6, colors="#627482", length=2)
    ax.ticklabel_format(useOffset=False, style="plain")
    if label:
        ax.text(0.02, 0.98, label, transform=ax.transAxes, va="top", ha="left",
                fontsize=8, fontweight="bold", color="#223A50",
                bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "edgecolor": "#CFD9DD", "alpha": 0.94})
    fig.subplots_adjust(left=0.16, right=0.98, bottom=0.13, top=0.98)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def make_tiles(work: Any, plan_points: list[dict[str, Any]], lawn: Any,
               size_dxf: float) -> list[dict[str, Any]]:
    x0, y0, x1, y1 = work.bounds
    nx, ny = math.ceil((x1 - x0) / size_dxf), math.ceil((y1 - y0) / size_dxf)
    point_grid: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for item in plan_points:
        point = item["geometry"]
        i = min(nx - 1, max(0, math.floor((point.x - x0) / size_dxf)))
        j = min(ny - 1, max(0, math.floor((point.y - y0) / size_dxf)))
        point_grid[(i, j)].append(item)
    tiles = []
    for j in range(ny - 1, -1, -1):
        for i in range(nx):
            bounds = box(x0 + i * size_dxf, y0 + j * size_dxf,
                         x0 + (i + 1) * size_dxf,
                         y0 + (j + 1) * size_dxf)
            work_piece = work.intersection(bounds)
            if work_piece.is_empty or work_piece.area < 1e-7:
                continue
            lawn_piece = clipped(lawn, bounds)
            if not point_grid[(i, j)] and (lawn_piece is None or lawn_piece.area < 1e-7):
                continue
            tiles.append({"id": f"У-{len(tiles)+1:02d}", "col": i, "row": j,
                          "viewport": bounds, "work": work_piece,
                          "points": point_grid[(i, j)], "lawn": lawn_piece})
    return tiles


def schedule(tiles: list[dict[str, Any]], plan_points: list[dict[str, Any]],
             plan_areas: list[dict[str, Any]], constraints: dict[str, Any],
             units_per_metre: float) -> tuple[list[dict[str, Any]], Any]:
    lawn = unary_union([item["geometry"] for item in plan_areas]) if plan_areas else None
    footprint: dict[str, Any] = {}
    for plant_type in ("tree", "shrub"):
        discs = [
            item["geometry"].buffer(
                float(item["properties"].get("footprint_radius_m") or 0)
                * float(item["properties"].get("dxf_units_per_meter") or units_per_metre),
                quad_segs=12,
            )
            for item in plan_points
            if item["properties"].get("plant_type") == plant_type
        ]
        footprint[plant_type] = unary_union(discs) if discs else None
    factor = units_per_metre ** 2
    rows = []
    for tile in tiles:
        viewport = tile["viewport"]
        counts = Counter(item["properties"].get("plant_type") for item in tile["points"])
        species: dict[str, Counter[str]] = defaultdict(Counter)
        for item in tile["points"]:
            props = item["properties"]
            species[str(props.get("plant_type"))][str(props.get("species") or "Без вида")] += 1
        lawn_species: dict[str, float] = defaultdict(float)
        for item in plan_areas:
            geom = item["geometry"]
            if box(*geom.bounds).intersects(viewport):
                area = geom.intersection(viewport).area / factor
                if area > 1e-6:
                    lawn_species[str(item["properties"].get("species") or "Травянистые")] += area
        base = clipped(constraints.get("base_allowed_area"), viewport)
        tree_piece = clipped(footprint["tree"], viewport)
        shrub_piece = clipped(footprint["shrub"], viewport)
        row = {
            "id": tile["id"], "grid_column": tile["col"], "grid_row": tile["row"],
            "bounds_dxf": [round(v, 3) for v in viewport.bounds],
            "work_area_m2": round(tile["work"].area / factor, 2),
            "plantable_area_m2": round((base.area if base is not None else 0) / factor, 2),
            "tree_count": counts["tree"], "shrub_count": counts["shrub"],
            "tree_crown_projection_m2": round((tree_piece.area if tree_piece is not None else 0) / factor, 2),
            "shrub_projection_m2": round((shrub_piece.area if shrub_piece is not None else 0) / factor, 2),
            "herbaceous_area_m2": round((tile["lawn"].area if tile["lawn"] is not None else 0) / factor, 2),
            "species": {key: dict(value) for key, value in species.items()},
            "herbaceous_species_area_m2": {key: round(value, 2) for key, value in lawn_species.items()},
            "manual_review_tree_count": sum(item["properties"].get("status") == "manual_review" and item["properties"].get("plant_type") == "tree" for item in tile["points"]),
        }
        rows.append(row)
    return rows, lawn


def draw_page(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#B6C9CE"))
    canvas.line(13 * mm, PAGE[1] - 11 * mm, PAGE[0] - 13 * mm, PAGE[1] - 11 * mm)
    canvas.setFont("GreenAI", 7)
    canvas.setFillColor(colors.HexColor("#526579"))
    canvas.drawString(13 * mm, 7 * mm, "GreenAI  |  Схематический план посадок")
    canvas.drawRightString(PAGE[0] - 13 * mm, 7 * mm, f"Страница {doc.page}")
    canvas.restoreState()


def build_atlas(normalized_path: Path, constraints_path: Path, zones_path: Path,
                plan_path: Path, output_path: Path, schedule_path: Path,
                *, input_dxf: Path | None = None, tile_size_m: float = 100.0,
                preview_directory: Path | None = None,
                existing_tree_audit_path: Path | None = None) -> Path:
    if tile_size_m <= 0:
        raise ValueError("tile_size_m must be positive")
    font, bold = register_fonts()
    source = read_geometries(normalized_path)
    constraints = read_geometries(constraints_path)
    zones = read_geometries(zones_path)
    points, areas = read_plan(plan_path)
    existing_tree_conflicts = (
        read_existing_tree_conflicts(existing_tree_audit_path)
        if existing_tree_audit_path is not None else []
    )
    work = source.get("work_boundary")
    if work is None or work.is_empty:
        raise ValueError("A work_boundary is required for the planting atlas")
    units = float(next((item["properties"].get("dxf_units_per_meter") for item in points + areas
                        if item["properties"].get("dxf_units_per_meter")), 1.0))
    tiles = make_tiles(work, points, unary_union([x["geometry"] for x in areas]) if areas else None,
                       tile_size_m * units)
    rows, lawn = schedule(tiles, points, areas, constraints, units)
    for plant_type in ("tree", "shrub"):
        expected = sum(item["properties"].get("plant_type") == plant_type for item in points)
        actual = sum(row[f"{plant_type}_count"] for row in rows)
        if actual != expected:
            raise ValueError(f"{plant_type} count is not fully covered by atlas sheets: {actual}/{expected}")
    lawn_area = (lawn.area if lawn is not None else 0) / units ** 2
    atlas_lawn_area = sum(row["herbaceous_area_m2"] for row in rows)
    if abs(atlas_lawn_area - lawn_area) > max(0.05, len(rows) * 0.005 + 0.01):
        raise ValueError("Herbaceous planting area is not fully covered by atlas sheets")
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_dxf": str(input_dxf) if input_dxf else None,
        "coordinate_reference": "local_dxf_coordinates", "dxf_units_per_meter": units,
        "tile_size_m": tile_size_m, "sections": rows,
        "totals": {
            "tree_count": len([x for x in points if x["properties"].get("plant_type") == "tree"]),
            "shrub_count": len([x for x in points if x["properties"].get("plant_type") == "shrub"]),
            "tree_crown_projection_m2": round(sum(row["tree_crown_projection_m2"] for row in rows), 2),
            "shrub_projection_m2": round(sum(row["shrub_projection_m2"] for row in rows), 2),
            "herbaceous_area_m2": round(lawn_area, 2),
            "existing_tree_conflict_count": len(existing_tree_conflicts),
        },
        "area_method": "Herbaceous area is exact GeoJSON polygon area; tree/shrub projection is union of configured circular footprints. Projections may overlap herbaceous area and must not be added together.",
        "view_method": "Context is redrawn from normalized geometries extracted from the source DXF; original CAD annotations and unsupported block graphics are not shown.",
    }
    schedule_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image_dir = preview_directory or output_path.parent / "planting_atlas_views"
    image_dir.mkdir(parents=True, exist_ok=True)
    title = ParagraphStyle("AtlasTitle", fontName=bold, fontSize=19, leading=23,
                           textColor=colors.HexColor("#173D57"), spaceAfter=5 * mm)
    heading = ParagraphStyle("AtlasHeading", fontName=bold, fontSize=12, leading=15,
                             textColor=colors.HexColor("#176B6A"), spaceAfter=2 * mm)
    body = ParagraphStyle("AtlasBody", fontName=font, fontSize=8.2, leading=11,
                          textColor=colors.HexColor("#283E4B"))
    small = ParagraphStyle("AtlasSmall", parent=body, fontSize=7, leading=9)
    header = ParagraphStyle("AtlasHeader", parent=small, fontName=bold, textColor=colors.white)
    doc = SimpleDocTemplate(str(output_path), pagesize=PAGE,
                            leftMargin=13 * mm, rightMargin=13 * mm,
                            topMargin=16 * mm, bottomMargin=12 * mm,
                            title="GreenAI - Схематический план посадок", author="GreenAI")
    story: list[Any] = []
    story.append(Paragraph("План посадок по участкам", title))
    source_name = escape(input_dxf.name) if input_dxf else "не указан"
    story.append(Paragraph(
        f"Чертёж: <b>{source_name}</b> &nbsp;|&nbsp; "
        f"Дата: {datetime.now().astimezone().strftime('%d.%m.%Y %H:%M')} &nbsp;|&nbsp; "
        f"Локальные координаты DXF, {units:g} ед./м", body))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        f"Листы У-01… делят границы работ на квадраты по {tile_size_m:g} × {tile_size_m:g} м. "
        "Это сетка для чтения плана, а не кадастровые участки. Топооснова перерисована "
        "из извлечённой геометрии DXF; исходные подписи и неподдерживаемые CAD-блоки здесь не показаны.", body))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("Общая ведомость", heading))
    totals = payload["totals"]
    overview = [
        ["Участков", "Деревьев", "Кустарников", "Травянистые посадки"],
        [str(len(rows)), str(totals["tree_count"]), str(totals["shrub_count"]),
         f'{totals["herbaceous_area_m2"]:,.1f} м²'],
    ]
    table = Table([[Paragraph(escape(x), header if index == 0 else body) for x in row]
                   for index, row in enumerate(overview)], colWidths=[42 * mm, 48 * mm, 48 * mm, 100 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176B6A")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B6C9CE")),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(table)
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        f"Проекция крон деревьев: <b>{totals['tree_crown_projection_m2']:,.1f} м²</b>; "
        f"проекция кустов: <b>{totals['shrub_projection_m2']:,.1f} м²</b>.", body))
    if existing_tree_audit_path is not None:
        story.append(Paragraph(
            f"Потенциальные конфликты существующих деревьев с проектными отступами: "
            f"<b>{len(existing_tree_conflicts)}</b>. Оранжевые кольца на схемах топоосновы "
            "показывают эти деревья; подробности по ID приведены в основном отчёте и диагностическом DXF. "
            "Требуется натурная проверка, это не решение об удалении.", body))
    story.append(Paragraph(
        "Площадь травянистых посадок вычислена по полигонам. Для деревьев и кустарников "
        "ниже дана площадь проекции заданного габарита. Эти площади могут перекрываться "
        "между собой и с травянистым покровом, поэтому их нельзя складывать как разные участки земли.", body))
    story.append(Spacer(1, 4 * mm))

    # The index shows the exact sheet boundaries in the source coordinate system.
    overview_path = image_dir / "overview.png"
    fig, ax = plt.subplots(figsize=(9.5, 4.0), dpi=160)
    overview_origin = work.centroid
    overview_work = rotate(work, 90, origin=overview_origin)
    paint_geometry(ax, overview_work, edge=PALETTE["boundary"], width=0.8)
    road = constraints.get("road_area")
    paint_geometry(ax, rotate(road, 90, origin=overview_origin) if road is not None else None,
                   face=PALETTE["road"])
    paint_geometry(ax, rotate(lawn, 90, origin=overview_origin) if lawn is not None else None,
                   face=PALETTE["lawn"], alpha=0.85)
    for tile in tiles:
        viewport = rotate(tile["viewport"], 90, origin=overview_origin)
        ax.add_patch(PlotPolygon(list(viewport.exterior.coords), facecolor="none",
                                 edgecolor="#587C8B", linewidth=0.45))
        anchor = rotate(tile["work"].representative_point(), 90, origin=overview_origin)
        ax.text(anchor.x, anchor.y, tile["id"], fontsize=5.5, ha="center", va="center",
                color="#173D57", fontweight="bold",
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8, "pad": 0.5})
    bx0, by0, bx1, by1 = overview_work.bounds
    ax.set_xlim(bx0 - 15 * units, bx1 + 15 * units)
    ax.set_ylim(by0 - 15 * units, by1 + 15 * units)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    fig.subplots_adjust(left=0.01, right=0.99, bottom=0.02, top=0.98)
    fig.savefig(overview_path, dpi=160, facecolor="white")
    plt.close(fig)
    img = Image(str(overview_path))
    ratio = min(230 * mm / img.imageWidth, 105 * mm / img.imageHeight)
    img.drawWidth, img.drawHeight = img.imageWidth * ratio, img.imageHeight * ratio
    story.append(img)
    story.append(Paragraph("Обзорная схема повернута на 90° для размещения на листе. Номера У-01… соответствуют подробным страницам.", small))
    story.append(PageBreak())

    for tile, row in zip(tiles, rows):
        viewport = tile["viewport"]
        story.append(Paragraph(f'{escape(row["id"])} — фрагмент плана', title))
        x0, y0, x1, y1 = viewport.bounds
        story.append(Paragraph(
            f"Координаты: X {x0:.1f}–{x1:.1f}, Y {y0:.1f}–{y1:.1f}. "
            f"Площадь в границах работ: <b>{row['work_area_m2']:,.1f} м²</b>; "
            f"пригодная территория: <b>{row['plantable_area_m2']:,.1f} м²</b>.", body))
        story.append(Spacer(1, 3 * mm))
        image_cells = []
        for kind, label in (("context", "1. Топооснова"),
                            ("constraints", "2. Зоны и ограничения"),
                            ("planting", "3. План посадок")):
            path = image_dir / f'{row["id"]}_{kind}.png'
            render_view(path, viewport, kind, source, constraints, zones,
                        tile["points"], lawn, label=label,
                        existing_tree_conflicts=existing_tree_conflicts,
                        dxf_units_per_meter=units)
            img = Image(str(path), width=83 * mm, height=83 * mm)
            image_cells.append(img)
        image_table = Table([image_cells], colWidths=[89 * mm] * 3)
        image_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                         ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                         ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
        story.append(image_table)
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(
            "Обозначения: серая заливка — дорога; серые контуры — здания; "
            "зелёные круги — деревья; охристые пятна — кустарники; светло-зелёная площадь — травянистые посадки. "
            "Оранжевые кольца на топооснове — существующие деревья с потенциальными конфликтами. "
            "На среднем виде светло-зелёным показана зона деревьев, жёлтым — зона кустарников.", small))
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("Что сажаем на этом участке", heading))
        species_text = []
        for plant_type, label in (("tree", "Деревья"), ("shrub", "Кустарники")):
            species = row["species"].get(plant_type, {})
            species_text.append(f"{label}: " + (
                ", ".join(f"{escape(name)} — {count} шт." for name, count in species.items())
                if species else "не предусмотрены"))
        herb_species = row["herbaceous_species_area_m2"]
        species_text.append("Травянистые: " + (
            ", ".join(f"{escape(name)} — {area:,.1f} м²" for name, area in herb_species.items())
            if herb_species else "не предусмотрены"))
        for item in species_text:
            story.append(Paragraph(item, body))
        story.append(Spacer(1, 3 * mm))
        metrics = [
            ["Деревья", "Проекция крон", "Кустарники", "Проекция кустов", "Травянистые"],
            [f'{row["tree_count"]} шт.', f'{row["tree_crown_projection_m2"]:,.1f} м²',
             f'{row["shrub_count"]} шт.', f'{row["shrub_projection_m2"]:,.1f} м²',
             f'{row["herbaceous_area_m2"]:,.1f} м²'],
        ]
        metrics_table = Table([[Paragraph(escape(v), header if i == 0 else body) for v in values]
                               for i, values in enumerate(metrics)], colWidths=[45 * mm, 55 * mm, 48 * mm, 59 * mm, 55 * mm])
        metrics_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176B6A")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B6C9CE")),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(metrics_table)
        if row["manual_review_tree_count"]:
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(
                f"Точек деревьев с незавершённой проверкой исходных данных: "
                f"{row['manual_review_tree_count']}. Подробные причины указаны в основном отчёте.", small))
        story.append(PageBreak())
    if story and isinstance(story[-1], PageBreak):
        story.pop()
    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    if output_path.stat().st_size == 0:
        raise RuntimeError("Planting atlas PDF is empty")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an illustrated planting plan PDF.")
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--zones", type=Path, required=True)
    parser.add_argument("--planting-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--input-dxf", type=Path)
    parser.add_argument("--tile-size-m", type=float, default=100.0)
    parser.add_argument("--preview-directory", type=Path)
    parser.add_argument("--existing-tree-audit", type=Path)
    args = parser.parse_args()
    path = build_atlas(args.normalized, args.constraints, args.zones, args.planting_plan,
                       args.output, args.schedule, input_dxf=args.input_dxf,
                       tile_size_m=args.tile_size_m, preview_directory=args.preview_directory,
                       existing_tree_audit_path=args.existing_tree_audit)
    print(f"Planting atlas: {path}")
    print(f"Area schedule: {args.schedule}")


if __name__ == "__main__":
    main()
