"""Resolve the DXF drawing scale from $INSUNITS or an explicit override.

The output is a small JSON document used by every distance-based pipeline
stage.  A unitless drawing is rejected unless the caller explicitly confirms
how many drawing units represent one metre.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from ezdxf.filemanagement import dxf_file_info
from ezdxf.units import M, conversion_factor, unit_name


def resolve_units(path: Path, explicit_units_per_meter: float | None = None) -> dict[str, Any]:
    info = dxf_file_info(path)
    insert_units = int(info.insert_units)
    header_units_per_meter: float | None = None
    warnings: list[str] = []

    if insert_units != 0:
        try:
            header_units_per_meter = float(conversion_factor(M, insert_units))
        except (TypeError, ValueError):
            warnings.append(
                f"DXF $INSUNITS={insert_units} is not supported for linear distance conversion."
            )

    if explicit_units_per_meter is not None:
        if not math.isfinite(explicit_units_per_meter) or explicit_units_per_meter <= 0:
            raise ValueError("explicit_units_per_meter must be finite and greater than zero")
        resolved = float(explicit_units_per_meter)
        source = "explicit_parameter"
        if (
            header_units_per_meter is not None
            and not math.isclose(resolved, header_units_per_meter, rel_tol=1e-9, abs_tol=1e-12)
        ):
            warnings.append(
                "Explicit scale differs from DXF $INSUNITS: "
                f"{resolved:g} versus {header_units_per_meter:g} drawing units per metre. "
                "The explicit value takes precedence."
            )
    elif header_units_per_meter is not None:
        resolved = header_units_per_meter
        source = "dxf_header_insunits"
    else:
        raise ValueError(
            "DXF does not declare usable linear units in $INSUNITS. "
            "Pass --dxf-units-per-meter explicitly."
        )

    return {
        "input_dxf": str(path.resolve()),
        "dxf_version": str(info.version),
        "dxf_encoding": str(info.encoding),
        "insert_units_code": insert_units,
        "insert_units_name": unit_name(insert_units),
        "header_dxf_units_per_meter": header_units_per_meter,
        "dxf_units_per_meter": resolved,
        "unit_scale_confirmed": True,
        "unit_scale_source": source,
        "units_confirmed_as_metres": math.isclose(
            resolved, 1.0, rel_tol=1e-9, abs_tol=1e-12
        ),
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dxf", type=Path)
    parser.add_argument("--dxf-units-per-meter", type=float)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = resolve_units(args.input_dxf, args.dxf_units_per_meter)
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"DXF unit detection error: {error}") from error
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"DXF unit scale: {report['dxf_units_per_meter']:g} unit(s) per metre")
    print(f"Unit source: {report['unit_scale_source']}")
    print(f"$INSUNITS: {report['insert_units_code']} ({report['insert_units_name']})")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")


if __name__ == "__main__":
    main()
