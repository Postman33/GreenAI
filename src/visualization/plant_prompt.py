"""Plant-specific instructions for editing a rendered planting view."""

from __future__ import annotations

from collections import Counter
from typing import Any

from scripts.seed import PLANTS


BOTANICAL_APPEARANCE = {
    "дерен белый": "Cornus alba (white dogwood): a dense deciduous mass of oval green leaves and slender red stems; no conifer needles, spherical topiary, or variegated leaves",
    "липа мелколистная": "Tilia cordata (small-leaved lime): deciduous trees with heart-shaped green leaves and naturally rounded crowns",
    "газонная травосмесь": "a low, natural mixed lawn grass surface, not ornamental shrubs",
}


def _normalized(name: str) -> str:
    return name.casefold().replace("ё", "е").strip()


CATALOG = {_normalized(plant.name): plant for plant in PLANTS}


def scene_plant_summary(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    legend = manifest.get("plant_mask_legend")
    if not isinstance(legend, list) or not legend:
        raise ValueError("Scene has no species color mask. Rebuild the Blender gallery with the current renderer.")
    counts = Counter((kind, item.get("species")) for kind, collection in
                     (("tree", manifest.get("proposed_trees", [])),
                      ("shrub", manifest.get("shrubs", []))) for item in collection)
    result = []
    for item in legend:
        kind = item["plant_type"]
        if kind not in {"tree", "shrub", "herbaceous"}:
            continue
        species = item["species"]
        catalog = CATALOG.get(_normalized(species))
        result.append({
            "plant_type": kind,
            "species": species,
            "color": item["color"],
            "count": counts[(kind, species)] if kind in {"tree", "shrub"} else None,
            "appearance": BOTANICAL_APPEARANCE.get(_normalized(species)),
            "hardiness_zone_min": catalog.hardiness_zone_min if catalog else None,
            "hardiness_zone_max": catalog.hardiness_zone_max if catalog else None,
            "climate_suitability": catalog.climate_suitability if catalog else "unverified",
            "catalog_invasive_flag": catalog.is_invasive if catalog else None,
        })
    if not result:
        raise ValueError("Scene has no proposed trees or shrubs for a species-aware photo")
    return result


def after_prompt(view: str, manifest: dict[str, Any], *, paired_before: bool = False) -> str:
    plants = scene_plant_summary(manifest)
    entries = []
    for plant in plants:
        description = plant["appearance"] or f"botanically faithful {plant['species']}"
        climate = (f" catalog hardiness zone {plant['hardiness_zone_min']}–{plant['hardiness_zone_max']}"
                   if plant["climate_suitability"] == "recommended" and
                   plant["hardiness_zone_min"] is not None else "")
        extent = (f"{plant['count']} proposed {plant['plant_type']} plants"
                  if plant["count"] is not None else "proposed herbaceous cover")
        entries.append(f"{plant['color']} = {extent}, "
                       f"species {plant['species']}; {description}{climate}.")
    legend = " ".join(entries)
    continuity = (
        "Reference 3 is the photorealistic BEFORE image of this same camera. Match its "
        "exposure, daylight, sky, paving textures and existing foliage. Keep existing buildings "
        "and street furniture consistent between the pair. Reference 1 remains authoritative "
        "for geometry: do not copy invented objects or geometry errors from reference 3. "
        "Only the proposed planting should change between BEFORE and AFTER. "
        if paired_before else ""
    )
    return (
        f"Create a photorealistic AFTER photograph from exactly the same {view} camera. "
        "Reference 1 is the Blender AFTER view and is authoritative for every surface boundary, "
        "foreground planted bed, plant position, building, road and sidewalk. Do not convert any "
        "green planted area in reference 1 into pavement or road. Reference 2 is the semantic "
        "PLANT MASK of that exact AFTER camera: black means no new "
        f"plant; the bright colors identify the specified proposed species. Mask legend: {legend} "
        + continuity +
        "Every colored shrub area, especially the foreground bed, must remain densely planted. "
        "Use the mask to place only these exact species; convert its bright coding colors to "
        "natural foliage, never show the false colors. The proposed shrubs form a dense, "
        "continuous, visually coherent planted mass with touching mature crowns, not isolated "
        "round balls or patchy mulch. Follow the same species and bed edges; retain gaps around "
        "utility covers and paths. Keep all trees individually located and correctly spaced. "
        "Show attractive but credible Moscow summer planting, natural textures and soft daylight. "
        "Do not invent buildings, extend facades, add floors, or alter building silhouettes. "
        "Do not invent other plant species, extra plantings, roads, paths, people, cars, text or labels."
    )
