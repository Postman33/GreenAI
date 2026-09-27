"""Build GitHub Mermaid blocks and PDF vectors from one graph specification."""

from __future__ import annotations

import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

from submission_diagram_specs import DIAGRAMS


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "diagrams"
DOCS = (ROOT / "docs" / "architecture.md", ROOT / "docs" / "for_organizers.md")
PALETTE = {
    "source": ("#f1f5f9", "#64748b"),
    "process": ("#eaf3fa", "#256b9a"),
    "model": ("#f1ecfa", "#7657a5"),
    "data": ("#fff4df", "#a66b13"),
    "output": ("#e7f5ed", "#23805a"),
    "warning": ("#fdf0ed", "#b45346"),
}


def mermaid(spec: dict, name: str) -> str:
    lines = [f"%% diagram:{name}", f"flowchart {spec['direction']}"]
    for group in spec["groups"]:
        lines.append(f"    subgraph {group['key']}[\"{group['label']}\"]")
        lines.append("        direction TB")
        for item in spec["nodes"]:
            if item["parent"] != group["key"]:
                continue
            label = item["label"].replace("\n", "<br/>")
            if item["kind"] == "data":
                shape = f"[(\"{label}\")]"
            else:
                shape = f"[\"{label}\"]"
            lines.append(f"        {item['key']}{shape}")
        lines.append("    end")
    lines.append("")
    for item in spec["edges"]:
        label = item["label"]
        connector = f"-->|{label}|" if label else "-->"
        lines.append(f"    {item['source']} {connector} {item['target']}")
    lines.append("")
    for kind, (fill, stroke) in PALETTE.items():
        lines.append(f"    classDef {kind} fill:{fill},stroke:{stroke},stroke-width:1.5px,color:#142b43")
    for item in spec["nodes"]:
        lines.append(f"    class {item['key']} {item['kind']}")
    for group in spec["groups"]:
        lines.append(f"    style {group['key']} fill:#f8fafc,stroke:#cbd5e1,stroke-width:1px")
    return "\n".join(lines)


def validate(spec: dict, name: str) -> None:
    groups = {item["key"]: item for item in spec["groups"]}
    nodes = {item["key"]: item for item in spec["nodes"]}
    if len(groups) != len(spec["groups"]) or len(nodes) != len(spec["nodes"]):
        raise ValueError(f"{name}: duplicate group or node identifier")
    if groups.keys() & nodes.keys():
        raise ValueError(f"{name}: group and node identifiers must differ")
    for item in spec["nodes"]:
        parent = groups[item["parent"]]
        if not (parent["x"] <= item["x"]
                and item["x"] + item["w"] <= parent["x"] + parent["w"]
                and parent["y"] <= item["y"]
                and item["y"] + item["h"] <= parent["y"] + parent["h"]):
            raise ValueError(f"{name}: {item['key']} is outside its group")
    for item in spec["edges"]:
        if item["source"] not in nodes or item["target"] not in nodes:
            raise ValueError(f"{name}: edge references an unknown node")


def center(item: dict) -> tuple[float, float]:
    return item["x"] + item["w"] / 2, item["y"] + item["h"] / 2


def edge_points(source: dict, target: dict, via: list) -> list[tuple[float, float]]:
    sx, sy = center(source)
    tx, ty = center(target)
    dx, dy = tx - sx, ty - sy
    if via:
        if abs(dx) >= abs(dy):
            start = (source["x"] + (source["w"] if dx > 0 else 0), sy)
            end = (target["x"] + (0 if dx > 0 else target["w"]), ty)
        else:
            start = (sx, source["y"] + (source["h"] if dy > 0 else 0))
            end = (tx, target["y"] + (0 if dy > 0 else target["h"]))
        return [start, *via, end]
    if abs(dx) >= abs(dy) * 0.85:
        start = (source["x"] + (source["w"] if dx > 0 else 0), sy)
        end = (target["x"] + (0 if dx > 0 else target["w"]), ty)
        mid_x = (start[0] + end[0]) / 2
        return [start, (mid_x, sy), (mid_x, ty), end]
    start = (sx, source["y"] + (source["h"] if dy > 0 else 0))
    end = (tx, target["y"] + (0 if dy > 0 else target["h"]))
    mid_y = (start[1] + end[1]) / 2
    return [start, (sx, mid_y), (tx, mid_y), end]


def draw_edge(ax, item: dict, nodes: dict) -> None:
    points = edge_points(nodes[item["source"]], nodes[item["target"]], item["via"])
    for left, right in zip(points[:-2], points[1:-1]):
        ax.plot((left[0], right[0]), (left[1], right[1]),
            color="#718096", lw=1.45, solid_capstyle="round", zorder=2)
    ax.add_patch(FancyArrowPatch(points[-2], points[-1], arrowstyle="-|>",
        mutation_scale=12, color="#718096", lw=1.45, zorder=2))


def render_static(name: str, spec: dict) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none"})
    fig, ax = plt.subplots(figsize=(16, 7.25), dpi=180)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.set_xlim(0, 1200)
    ax.set_ylim(555, 0)
    ax.axis("off")
    for item in spec["groups"]:
        ax.add_patch(FancyBboxPatch((item["x"], item["y"]), item["w"], item["h"],
            boxstyle="round,pad=0,rounding_size=5", facecolor="#f8fafc",
            edgecolor="#cbd5e1", lw=1.3, zorder=1))
        ax.text(item["x"] + 15, item["y"] + 29, item["label"],
            fontsize=12, fontweight="bold", color="#334155", zorder=3)
        ax.plot((item["x"] + 14, item["x"] + item["w"] - 14),
            (item["y"] + 42, item["y"] + 42), color="#dce4ec", lw=1, zorder=2)
    nodes = {item["key"]: item for item in spec["nodes"]}
    for item in spec["edges"]:
        draw_edge(ax, item, nodes)
    for item in spec["nodes"]:
        fill, stroke = PALETTE[item["kind"]]
        ax.add_patch(FancyBboxPatch((item["x"], item["y"]), item["w"], item["h"],
            boxstyle="round,pad=0,rounding_size=3", facecolor=fill,
            edgecolor=stroke, lw=1.6, zorder=4))
        ax.text(*center(item), item["label"], ha="center", va="center",
            fontsize=10.2, fontweight="semibold", linespacing=1.2,
            color="#142b43", zorder=5)
    ax.plot((20, 1175), (510, 510), color="#cbd5e1", lw=1)
    ax.text(25, 532, spec["note"], fontsize=9.6, color="#475569", va="center")
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.svg", bbox_inches="tight", pad_inches=.08)
    fig.savefig(OUT / f"{name}.png", dpi=200, bbox_inches="tight", pad_inches=.08)
    plt.close(fig)


def sync_markdown(name: str, source: str) -> None:
    start = f"<!-- diagram:{name}:start -->"
    end = f"<!-- diagram:{name}:end -->"
    replacement = f"{start}\n```mermaid\n{source}\n```\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        updated, count = pattern.subn(lambda _: replacement, text)
        if count:
            path.write_text(updated, encoding="utf-8")


def main() -> None:
    for name, spec in DIAGRAMS.items():
        validate(spec, name)
        source = mermaid(spec, name)
        render_static(name, spec)
        sync_markdown(name, source)
        print(f"{name}: Mermaid + SVG + PNG")


if __name__ == "__main__":
    main()
