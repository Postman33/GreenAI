"""Render accepted building polygons over the original building linework."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import shape


def read_geometry(path: Path, object_type: str):
    with path.open(encoding="utf-8") as source:
        for line in source:
            feature = json.loads(line)
            if feature.get("properties", {}).get("object_type") == object_type:
                return shape(feature["geometry"])
    raise ValueError(f"No {object_type!r} feature in {path}")


def polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def line_parts(geometry):
    if geometry.geom_type == "LineString":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from line_parts(part)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--bounds", nargs=4, type=float, metavar=("MIN_X", "MIN_Y", "MAX_X", "MAX_Y"))
    args = parser.parse_args()

    buildings = read_geometry(args.input, "building")
    source = read_geometry(args.input, "building_linework")

    figure, axis = plt.subplots(figsize=(24, 18), dpi=150)
    for polygon in polygon_parts(buildings):
        x, y = polygon.exterior.xy
        axis.fill(x, y, color="#f28c18", alpha=0.82, zorder=1)
        for ring in polygon.interiors:
            x, y = ring.xy
            axis.fill(x, y, color="#252525", zorder=2)
    for line in line_parts(source):
        x, y = line.xy
        axis.plot(x, y, color="#ff2525", linewidth=0.45, zorder=3)

    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        rejected = report["object_types"]["building"]["endpoint_chain_closure"]["rejected_chains"]
        for index, item in enumerate(rejected):
            x, y = item["start"]
            axis.text(
                x,
                y,
                str(index),
                color="#6ec6ff",
                fontsize=7,
                zorder=4,
                clip_on=True,
            )

    if args.bounds:
        min_x, min_y, max_x, max_y = args.bounds
        axis.set_xlim(min_x, max_x)
        axis.set_ylim(min_y, max_y)

    axis.set_aspect("equal", adjustable="box")
    axis.set_facecolor("#252525")
    figure.patch.set_facecolor("#252525")
    axis.set_axis_off()
    figure.tight_layout(pad=0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, facecolor=figure.get_facecolor(), bbox_inches="tight", pad_inches=0)
    plt.close(figure)


if __name__ == "__main__":
    main()
