"""Choose compatible planting schemes for all beds of one plant type.

Geometry and regulatory eligibility are calculated upstream. CP-SAT only
chooses among those schemes; the selected points are checked again by the
ordinary planting decision path before export.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from ortools.sat.python import cp_model
from shapely import STRtree
from shapely.geometry import Point

from ..domain.models import PlantingProfile
from .placement_generator import required_spacing


def optimize_layouts(
    options: list[dict[str, Any]],
    profile: PlantingProfile,
    max_count: int,
    *,
    time_limit_s: float = 10.0,
) -> tuple[list[int], dict[str, Any]]:
    """Select at most one whole scheme per bed, with inter-bed spacing.

    Each option has ``bed``, ``points``, ``score`` and ``style``. Whole-scheme
    variables preserve aligned rows and connected shrub masses rather than
    allowing a point solver to make visually broken fragments.
    """
    if max_count < 0 or not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError("Invalid layout optimization limit")
    if not options:
        return [], {"solver": "cp_sat", "status": "NO_CANDIDATES", "option_count": 0,
                    "bed_count": 0, "selected_bed_count": 0,
                    "unselected_bed_count": 0, "selected_count": 0,
                    "conflict_count": 0, "max_count": max_count,
                    "capacity_reached": False}
    model = cp_model.CpModel()
    selected = [model.new_bool_var(f"scheme_{i}") for i in range(len(options))]
    by_bed: dict[int, list[int]] = defaultdict(list)
    counts: list[int] = []
    weights: list[int] = []
    for i, option in enumerate(options):
        by_bed[int(option["bed"])].append(i)
        count = len(option["points"])
        counts.append(count)
        # The landscape score accounts for rows, connected masses and site fit;
        # the extra count term prevents attractive but nearly empty plans.
        weights.append(max(1, round(float(option["score"]) * 100) + 100 * count))
    for indices in by_bed.values():
        model.add_at_most_one(selected[i] for i in indices)
    model.add(sum(counts[i] * selected[i] for i in range(len(options))) <= max_count)

    # STRtree narrows conflict discovery to neighbouring points. A conflict
    # between two whole schemes is one binary constraint, even if many of
    # their individual plants overlap.
    points: list[Point] = []
    owners: list[int] = []
    for option_index, option in enumerate(options):
        for x, y in option["points"]:
            points.append(Point(x, y))
            owners.append(option_index)
    conflicts: set[tuple[int, int]] = set()
    spacing = required_spacing(profile, profile)
    if points and spacing > 0:
        tree = STRtree(points)
        for left, point in enumerate(points):
            left_owner = owners[left]
            for raw_right in tree.query(point.buffer(spacing).envelope):
                right = int(raw_right)
                if right <= left:
                    continue
                right_owner = owners[right]
                if (left_owner == right_owner
                        or options[left_owner]["bed"] == options[right_owner]["bed"]):
                    continue
                if point.distance(points[right]) + 1e-9 < spacing:
                    conflicts.add(tuple(sorted((left_owner, right_owner))))
    for left, right in sorted(conflicts):
        model.add(selected[left] + selected[right] <= 1)
    model.maximize(sum(weights[i] * selected[i] for i in range(len(options))))

    # A legal initial plan gives the solver a useful incumbent when the time
    # limit is reached, and also provides a deterministic emergency result.
    initial: set[int] = set()
    initial_count = 0
    for index in sorted(range(len(options)), key=lambda i: (-weights[i], i)):
        if (initial_count + counts[index] > max_count
                or any(other in initial for other in by_bed[int(options[index]["bed"])])):
            continue
        if any((left == index and right in initial) or (right == index and left in initial)
               for left, right in conflicts):
            continue
        initial.add(index)
        initial_count += counts[index]
    for index, variable in enumerate(selected):
        model.add_hint(variable, int(index in initial))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 2026
    status = solver.solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        chosen = [i for i, variable in enumerate(selected) if solver.value(variable)]
    elif status == cp_model.UNKNOWN:
        chosen = sorted(initial)
    else:
        raise ValueError(f"CP-SAT layout model failed: {solver.status_name(status)}")
    return chosen, {
        "solver": "cp_sat",
        "status": solver.status_name(status),
        "option_count": len(options),
        "bed_count": len(by_bed),
        "selected_bed_count": len(chosen),
        "unselected_bed_count": len(by_bed) - len(chosen),
        "conflict_count": len(conflicts),
        "selected_count": sum(counts[i] for i in chosen),
        "max_count": max_count,
        "capacity_reached": sum(counts[i] for i in chosen) >= max_count,
        "selected_option_indices": chosen,
        "time_limit_s": time_limit_s,
        "objective": sum(weights[i] for i in chosen),
        "best_bound": (solver.best_objective_bound
                       if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None),
        "solve_time_s": solver.wall_time,
    }
