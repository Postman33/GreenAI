"""Blender-side renderer for a prepared Sylvitect-core scene manifest.

Run only through Blender:
    blender -b --python src/visualization/blender_render.py -- --scene scene.json --output renders
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def material(name, color, roughness=0.7, metallic=0.0):
    value = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    value.diffuse_color = (*color, 1.0)
    value.use_nodes = True
    node = value.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = (*color, 1.0)
    node.inputs["Roughness"].default_value = roughness
    node.inputs["Metallic"].default_value = metallic
    return value


def collection(name):
    result = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(result)
    return result


def move_to_collection(obj, target):
    for source in list(obj.users_collection):
        source.objects.unlink(obj)
    target.objects.link(obj)


def mesh_from_triangles(name, triangles, mat, target):
    vertices = [tuple(vertex) for triangle in triangles for vertex in triangle]
    faces = [(index, index + 1, index + 2) for index in range(0, len(vertices), 3)]
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    target.objects.link(obj)
    return obj


def building_object(name, exterior, height, mat, target):
    ring = exterior[:-1] if len(exterior) > 1 and exterior[0] == exterior[-1] else exterior
    if len(ring) < 3:
        return None
    vertices = [(x, y, 0.1) for x, y in ring] + [(x, y, height) for x, y in ring]
    count = len(ring)
    faces = [tuple(range(count - 1, -1, -1)), tuple(range(count, count * 2))]
    for index in range(count):
        nxt = (index + 1) % count
        faces.append((index, nxt, count + nxt, count + index))
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    target.objects.link(obj)
    bevel = obj.modifiers.new("Soft facade edges", "BEVEL")
    bevel.width = 0.08
    bevel.segments = 2
    return obj


def create_tree(name, data, trunk_mat, leaf_mat, target, proposed):
    x, y = data["position"]
    height = float(data["height"])
    radius = float(data["crown_radius"])
    bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=max(0.12, radius * 0.09), depth=height * 0.48,
                                       location=(x, y, height * 0.24 + 0.1))
    trunk = bpy.context.object
    trunk.name = name + "_trunk"
    trunk.data.materials.append(trunk_mat)
    move_to_collection(trunk, target)
    offsets = ((0.0, 0.0, 0.0), (0.38, 0.12, -0.12), (-0.28, -0.22, -0.08))
    for index, (ox, oy, oz) in enumerate(offsets):
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=1.0,
                                             location=(x + ox * radius, y + oy * radius,
                                                       height * (0.66 + oz)))
        crown = bpy.context.object
        crown.name = f"{name}_crown_{index}"
        crown.scale = (radius * (0.82 if index else 1.0), radius * 0.82, height * 0.25)
        crown.data.materials.append(leaf_mat)
        if proposed:
            crown["plant_mask_color"] = data.get("mask_color", "#00BFFF")
        move_to_collection(crown, target)
    return proposed


def create_shrub(name, data, mat, target):
    x, y = data["position"]
    radius = float(data["radius"])
    height = float(data["height"])
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=1.0, location=(x, y, height * 0.52 + 0.12))
    obj = bpy.context.object
    obj.name = name
    obj.scale = (radius, radius * 0.88, height * 0.55)
    obj.data.materials.append(mat)
    if "mask_color" in data:
        obj["plant_mask_color"] = data["mask_color"]
    move_to_collection(obj, target)


def mask_material(color):
    """Unlit semantic color, independent of sun and scene materials."""
    value = bpy.data.materials.new("Plant mask " + color)
    value.use_nodes = True
    nodes = value.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else
              ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    emission.inputs["Color"].default_value = (*linear, 1.0)
    emission.inputs["Strength"].default_value = 1.0
    value.node_tree.links.new(emission.outputs[0], output.inputs["Surface"])
    return value


def render_plant_masks(scene, manifest, output_dir, cameras, proposed):
    black = mask_material("#000000")
    colors = {entry["color"] for entry in manifest.get("plant_mask_legend", [])}
    colors.update(obj.get("plant_mask_color") for obj in proposed.objects
                  if obj.get("plant_mask_color"))
    swatches = {color: mask_material(color) for color in colors}
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            color = obj.get("plant_mask_color")
            obj.data.materials.clear()
            obj.data.materials.append(swatches.get(color, black))
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0, 0, 0, 1)
    scene.view_settings.view_transform = "Standard"
    try:
        scene.view_settings.look = "None"
    except TypeError:
        pass
    set_proposed_visible(proposed, True)
    for camera in cameras:
        scene.camera = camera
        scene.render.filepath = str(output_dir / f"{camera.name}_plant_mask.png")
        bpy.ops.render.render(write_still=True)


def point_camera(camera, position, target):
    camera.location = position
    direction = Vector(target) - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def setup_world():
    world = bpy.context.scene.world or bpy.data.worlds.new("Sylvitect-core World")
    bpy.context.scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.55, 0.72, 0.90, 1.0)
    background.inputs["Strength"].default_value = 0.32
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 45))
    sun = bpy.context.object
    sun.name = "Sun"
    sun.rotation_euler = (math.radians(28), math.radians(-18), math.radians(135))
    sun.data.energy = 3.2
    sun.data.angle = math.radians(4.0)
    bpy.ops.object.light_add(type="AREA", location=(15, -20, 28))
    area = bpy.context.object
    area.data.energy = 900
    area.data.shape = "DISK"
    area.data.size = 18
    point_camera(area, area.location, (0, 0, 0))


def configure_render(scene, quality, width, height):
    scene.render.resolution_x = int(width)
    scene.render.resolution_y = int(height)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"):
        try:
            scene.render.engine = engine
            break
        except TypeError:
            continue
    scene.render.image_settings.color_mode = "RGB"
    for look in ("AgX - Medium High Contrast", "AgX - Medium High Contrast Look", "Medium High Contrast"):
        try:
            scene.view_settings.look = look
            break
        except TypeError:
            continue


def set_proposed_visible(target, visible):
    target.hide_render = not visible
    target.hide_viewport = not visible


def render_scene(manifest_path: Path, output_dir: Path, quality: str,
                 camera_names: set[str] | None = None) -> None:
    print("Sylvitect-core: loading scene manifest", flush=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    configure_render(scene, quality, manifest["render"]["width"], manifest["render"]["height"])
    setup_world()
    context = collection("CONTEXT")
    proposed = collection("PROPOSED_PLANTING")
    materials = {
        "asphalt": material("Asphalt", (0.055, 0.065, 0.072), 0.92),
        "paving": material("Paving", (0.48, 0.43, 0.36), 0.86),
        "concrete": material("Concrete", (0.34, 0.35, 0.34), 0.9),
        "soil_grass": material("Existing ground", (0.18, 0.25, 0.10), 0.92),
        "lawn": material("Proposed lawn", (0.12, 0.46, 0.055), 0.95),
        "mulch": material("Shrub mulch", (0.19, 0.085, 0.025), 1.0),
        "metal": material("Well metal", (0.12, 0.13, 0.13), 0.48, 0.55),
        "building": material("Facade", (0.52, 0.37, 0.27), 0.82),
        "trunk": material("Tree bark", (0.16, 0.065, 0.025), 0.96),
        "existing_leaf": material("Existing leaves", (0.055, 0.25, 0.045), 0.9),
        "proposed_leaf": material("Proposed leaves", (0.10, 0.46, 0.055), 0.88),
        "shrub": material("Shrub leaves", (0.10, 0.33, 0.035), 0.93),
    }
    print("Sylvitect-core: building surfaces and vegetation", flush=True)
    radius = float(manifest["focus_radius_m"])
    ground = mesh_from_triangles("Ground", [[[-radius, -radius, 0], [radius, -radius, 0], [radius, radius, 0]],
                                               [[-radius, -radius, 0], [radius, radius, 0], [-radius, radius, 0]]],
                                 materials["soil_grass"], context)
    ground.name = "Base terrain"
    for surface in manifest["surfaces"]:
        target = proposed if surface.get("proposed") else context
        obj = mesh_from_triangles(surface["name"], surface["triangles"],
                                  materials[surface["material"]], target)
        if "mask_color" in surface:
            obj["plant_mask_color"] = surface["mask_color"]
    for index, building in enumerate(manifest["buildings"], start=1):
        building_object(f"Building_{index:03d}", building["exterior"], building["height"],
                        materials["building"], context)
    for index, tree in enumerate(manifest["existing_trees"], start=1):
        create_tree(f"ExistingTree_{index:03d}", tree, materials["trunk"], materials["existing_leaf"], context, False)
    for index, shrub in enumerate(manifest["existing_belt_shrubs"], start=1):
        create_shrub(f"ExistingShrub_{index:03d}", shrub, materials["existing_leaf"], context)
    for index, tree in enumerate(manifest["proposed_trees"], start=1):
        create_tree(f"ProposedTree_{index:03d}", tree, materials["trunk"], materials["proposed_leaf"], proposed, True)
    for index, shrub in enumerate(manifest["shrubs"], start=1):
        create_shrub(f"ProposedShrub_{index:04d}", shrub, materials["shrub"], proposed)

    output_dir.mkdir(parents=True, exist_ok=True)
    rendered_cameras = []
    for camera_data in manifest["cameras"]:
        if camera_names is not None and camera_data["name"] not in camera_names:
            continue
        camera_object = bpy.data.objects.new(camera_data["name"], bpy.data.cameras.new(camera_data["name"] + "Data"))
        scene.collection.objects.link(camera_object)
        point_camera(camera_object, camera_data["position"], camera_data["target"])
        if camera_data["kind"] == "orthographic":
            camera_object.data.type = "ORTHO"
            camera_object.data.ortho_scale = camera_data["ortho_scale"]
        else:
            camera_object.data.lens = camera_data["lens_mm"]
        scene.camera = camera_object
        rendered_cameras.append(camera_object)
        states = (True,) if camera_data["name"] == "top" else (False, True)
        for visible in states:
            set_proposed_visible(proposed, visible)
            suffix = "after" if visible else "before"
            scene.render.filepath = str(output_dir / f"{camera_data['name']}_{suffix}.png")
            print(f"Sylvitect-core: rendering {camera_data['name']}_{suffix}.png", flush=True)
            bpy.ops.render.render(write_still=True)
    set_proposed_visible(proposed, True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_dir / "sylvitect_core_scene.blend"))
    render_plant_masks(scene, manifest, output_dir, rendered_cameras, proposed)
    (output_dir / "render_manifest.json").write_text(
        json.dumps({"scene": str(manifest_path), "quality": quality,
                    "images": sorted(path.name for path in output_dir.glob("*.png"))},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quality", choices=("draft", "final"), default="draft")
    parser.add_argument("--views", default="overview,pedestrian,top",
                        help="Comma-separated camera names")
    args = parser.parse_args(argv)
    camera_names = {name.strip() for name in args.views.split(",") if name.strip()}
    if not camera_names or not camera_names <= {"overview", "pedestrian", "top"}:
        parser.error("--views must contain overview, pedestrian, or top")
    render_scene(args.scene.resolve(), args.output.resolve(), args.quality, camera_names)


if __name__ == "__main__":
    main()

