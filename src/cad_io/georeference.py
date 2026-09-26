"""Convert local DXF coordinates to WGS84 using a saved georeference.

The georeference JSON contains an affine transform from local drawing metres to
EPSG:3857.  EPSG:3857 <-> WGS84 conversion is implemented directly, so this
module has no GIS dependency.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


EARTH_RADIUS_M = 6_378_137.0
WEB_MERCATOR_LIMIT_DEG = 85.0511287798066


def load_georeference(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        config = json.load(source)

    transform = config.get("local_to_epsg3857", {})
    matrix = transform.get("matrix")
    offset = transform.get("offset")
    if (
        not isinstance(matrix, list)
        or len(matrix) != 2
        or any(not isinstance(row, list) or len(row) != 2 for row in matrix)
        or not isinstance(offset, list)
        or len(offset) != 2
    ):
        raise ValueError("Invalid local_to_epsg3857 affine transform")
    return config


def local_to_epsg3857(
    x: float,
    y: float,
    config: dict[str, Any],
) -> tuple[float, float]:
    transform = config["local_to_epsg3857"]
    matrix = transform["matrix"]
    offset = transform["offset"]
    return (
        matrix[0][0] * x + matrix[0][1] * y + offset[0],
        matrix[1][0] * x + matrix[1][1] * y + offset[1],
    )


def epsg3857_to_local(
    x: float,
    y: float,
    config: dict[str, Any],
) -> tuple[float, float]:
    transform = config["local_to_epsg3857"]
    matrix = transform["matrix"]
    offset = transform["offset"]
    a, b = matrix[0]
    c, d = matrix[1]
    determinant = a * d - b * c
    if abs(determinant) < 1e-15:
        raise ValueError("Georeference affine matrix is singular")
    shifted_x = x - offset[0]
    shifted_y = y - offset[1]
    return (
        (d * shifted_x - b * shifted_y) / determinant,
        (-c * shifted_x + a * shifted_y) / determinant,
    )


def epsg3857_to_wgs84(x: float, y: float) -> tuple[float, float]:
    longitude = math.degrees(x / EARTH_RADIUS_M)
    latitude = math.degrees(
        2.0 * math.atan(math.exp(y / EARTH_RADIUS_M)) - math.pi / 2.0
    )
    return longitude, latitude


def wgs84_to_epsg3857(longitude: float, latitude: float) -> tuple[float, float]:
    latitude = max(-WEB_MERCATOR_LIMIT_DEG, min(WEB_MERCATOR_LIMIT_DEG, latitude))
    return (
        EARTH_RADIUS_M * math.radians(longitude),
        EARTH_RADIUS_M
        * math.log(math.tan(math.pi / 4.0 + math.radians(latitude) / 2.0)),
    )


def local_to_wgs84(
    x: float,
    y: float,
    config: dict[str, Any],
) -> tuple[float, float]:
    return epsg3857_to_wgs84(*local_to_epsg3857(x, y, config))


def wgs84_to_local(
    longitude: float,
    latitude: float,
    config: dict[str, Any],
) -> tuple[float, float]:
    return epsg3857_to_local(
        *wgs84_to_epsg3857(longitude, latitude),
        config,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert coordinates using a saved DXF georeference",
    )
    parser.add_argument("config", type=Path, help="Georeference JSON")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--local",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Convert local DXF X/Y to WGS84",
    )
    group.add_argument(
        "--wgs84",
        nargs=2,
        type=float,
        metavar=("LONGITUDE", "LATITUDE"),
        help="Convert WGS84 longitude/latitude to local DXF X/Y",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_georeference(args.config)
    if args.local:
        longitude, latitude = local_to_wgs84(*args.local, config)
        result = {
            "input_crs": "local_dxf",
            "output_crs": "EPSG:4326",
            "longitude": longitude,
            "latitude": latitude,
        }
    else:
        x, y = wgs84_to_local(*args.wgs84, config)
        result = {
            "input_crs": "EPSG:4326",
            "output_crs": "local_dxf",
            "x": x,
            "y": y,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
