"""Rebuild Songuess's original, reusable sleeve and vinyl assets with Blender.

Run from the repository root with `just blender-build`.
Coordinates in helpers use the web convention: X right, Y up, Z toward viewer.
"""

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "blender"
sys.path.insert(0, str(HERE))
from carousel_clearance import forward_clearance

OUTPUT = ROOT / "frontend/public/models/songuess"
OUTPUT.mkdir(parents=True, exist_ok=True)
(HERE / "previews").mkdir(exist_ok=True)


def xyz(x, y, z):
    return (x, -z, y)


def material(name, color, roughness=0.5, metallic=0.0):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Metallic"].default_value = metallic
    return mat


def mesh(name, vertices, faces, mat, parent, smooth=False, uv=None):
    data = bpy.data.meshes.new(name)
    data.from_pydata([xyz(*v) for v in vertices], [], faces)
    data.update()
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent
    obj.data.materials.append(mat)
    if smooth:
        for face in data.polygons:
            face.use_smooth = True
    if uv:
        layer = data.uv_layers.new(name="ArtworkUV")
        for polygon in data.polygons:
            for loop in polygon.loop_indices:
                layer.data[loop].uv = uv[data.loops[loop].vertex_index]
    return obj


def box(name, center, size, mat, parent, bevel=0.006):
    bpy.ops.mesh.primitive_cube_add(size=1, location=xyz(*center))
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = (size[0], size[2], size[1])
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    obj.parent = parent
    if bevel:
        modifier = obj.modifiers.new("Soft folded edge", "BEVEL")
        modifier.width = bevel
        modifier.segments = 2
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    return obj


def root(name):
    obj = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(obj)
    obj.empty_display_type = "PLAIN_AXES"
    return obj


def lathe(name, profile, mat, parent, segments=96):
    vertices = []
    for radius, z in profile:
        vertices.extend(
            (
                radius * math.cos(i * math.tau / segments),
                radius * math.sin(i * math.tau / segments),
                z,
            )
            for i in range(segments)
        )
    faces = []
    for ring in range(len(profile) - 1):
        for i in range(segments):
            j = (i + 1) % segments
            a, b = ring * segments + i, ring * segments + j
            faces.append((a, b, b + segments, a + segments))
    return mesh(
        name,
        vertices,
        faces,
        mat,
        parent,
        smooth=False,
        uv=[(0.5 + x / 1.88, 0.5 + y / 1.88) for x, y, _ in vertices],
    )


def export(obj, name):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    for child in obj.children_recursive:
        child.select_set(True)
    bpy.ops.export_scene.gltf(
        filepath=str(OUTPUT / name),
        export_format="GLB",
        use_selection=True,
        export_yup=True,
        export_animations=False,
        export_cameras=False,
        export_lights=False,
        export_extras=True,
    )


bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.name = "Shared assets"
paper = material("Warm cardboard edges", (0.52, 0.47, 0.37), 0.84)
jacket = material("Jacket charcoal", (0.032, 0.038, 0.031), 0.66)
ink = material("Vinyl polymer", (0.008, 0.008, 0.007), 0.24, 0.0)
ink.node_tree.nodes.get("Principled BSDF").inputs["Coat Weight"].default_value = 0.36
label = material("VinylLabel", (0.52, 0.048, 0.035), 0.76)
label_line = material("Label pressed ring", (0.43, 0.039, 0.029), 0.8)
art = material("SleeveArtwork", (1, 1, 1), 0.57)

# Original placeholder artwork; no catalog images or large embedded textures.
image = bpy.data.images.new("Songuess placeholder pressing", width=256, height=256)
pixels = []
for y in range(256):
    for x in range(256):
        distance = math.hypot(x - 128, y - 150)
        color = (0.94, 0.90, 0.81)
        if distance < 84:
            color = (0.045, 0.052, 0.046)
            if 25 < distance < 81 and int(distance / 4) % 2 == 0:
                color = (0.12, 0.13, 0.115)
            if distance < 24:
                color = (1.0, 0.21, 0.11)
        if 28 < x < 228 and (25 < y < 29 or 38 < y < 41):
            color = (0.045, 0.052, 0.046)
        pixels.extend((*color, 1))
image.pixels = pixels
image.filepath_raw = str(HERE / "placeholder.png")
image.file_format = "PNG"
image.save()
image.pack()
node = art.node_tree.nodes.new("ShaderNodeTexImage")
node.image = image
art.node_tree.links.new(
    node.outputs["Color"],
    art.node_tree.nodes.get("Principled BSDF").inputs["Base Color"],
)

sleeve = root("Sleeve")
sleeve["asset_role"] = (
    "Reusable jacket; replace SleeveArtwork base-color texture at runtime"
)
box("SleeveFrontPanel", (0, 0, 0.032), (2, 2, 0.012), jacket, sleeve)
box("SleeveBackPanel", (0, 0, -0.032), (2, 2, 0.012), jacket, sleeve)
box("SleeveSpine", (-0.992, 0, 0), (0.016, 1.98, 0.064), paper, sleeve, 0.003)
box("SleeveTopFold", (0, 0.994, 0), (1.98, 0.012, 0.064), paper, sleeve, 0.003)
box("SleeveBottomFold", (0, -0.994, 0), (1.98, 0.012, 0.064), paper, sleeve, 0.003)
mesh(
    "SleeveArtwork",
    [
        (-0.975, -0.975, 0.0384),
        (0.975, -0.975, 0.0384),
        (0.975, 0.975, 0.0384),
        (-0.975, 0.975, 0.0384),
    ],
    [(0, 1, 2, 3)],
    art,
    sleeve,
    uv=[(0, 0), (1, 0), (1, 1), (0, 1)],
)
# Two distinct paper edges frame the open mouth; leave the cavity unobstructed.
for side, depth in [("Front", 0.032), ("Back", -0.032)]:
    box(
        f"SleeveMouth{side}",
        (0.996, 0, depth),
        (0.008, 1.98, 0.012),
        paper,
        sleeve,
        0.002,
    )
export(sleeve, "sleeve.glb")

vinyl = root("Vinyl")
vinyl["asset_role"] = (
    "Reusable 12-inch record; rotate around local Z in web coordinates"
)
# An original, small PBR texture gives the pressing broad directional sheen.
# RGB carries subtle polymer variation; alpha stores the roughness, not opacity.
# It moves with the record while the studio environment supplies live reflections.
sheen = bpy.data.images.new(
    "Vinyl directional sheen", width=256, height=256, alpha=True
)
sheen_pixels = []
for y in range(256):
    for x in range(256):
        px, py = (x + 0.5 - 128) / 128, (y + 0.5 - 128) / 128
        radius = math.hypot(px, py)
        angle = math.atan2(py, px)
        lobe = max(0, math.cos(2 * (angle - 0.68))) ** 6
        broad = max(0, math.cos(2 * (angle + 0.2))) ** 12
        inner = max(0, min((radius - 0.36) / 0.12, 1))
        outer = max(0, min((1 - radius) / 0.06, 1))
        playing_surface = (
            inner * inner * (3 - 2 * inner) * outer * outer * (3 - 2 * outer)
        )
        value = 0.065 + playing_surface * (0.30 * lobe + 0.065 * broad)
        roughness = 0.24 - playing_surface * 0.12 * lobe
        sheen_pixels.extend((value, value, value * 0.97, roughness))
sheen.pixels = sheen_pixels
sheen.alpha_mode = "CHANNEL_PACKED"
sheen.filepath_raw = str(HERE / "vinyl-sheen.png")
sheen.file_format = "PNG"
sheen.save()
sheen.pack()
texture = ink.node_tree.nodes.new("ShaderNodeTexImage")
texture.image = sheen
shader = ink.node_tree.nodes.get("Principled BSDF")
ink.node_tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])
ink.node_tree.links.new(texture.outputs["Alpha"], shader.inputs["Roughness"])

# Flat playing bands with fine incised grooves, rather than raised ribbing.
profile = [
    (0.012, -0.012),
    (0.934, -0.012),
    (0.94, -0.006),
    (0.94, 0.006),
    (0.934, 0.012),
]
# Closely spaced lead-in grooves at the rim.
for radius in (0.929, 0.923, 0.917):
    profile.extend(
        [(radius, 0.012), (radius - 0.0009, 0.0114), (radius - 0.0018, 0.012)]
    )
# Wider smooth bands reflect the reference's quieter surface.
for i in range(17):
    radius = 0.895 - i * 0.031
    profile.extend(
        [(radius, 0.012), (radius - 0.0011, 0.0113), (radius - 0.0022, 0.012)]
    )
profile.extend([(0.365, 0.012), (0.342, 0.0126), (0.012, 0.013), (0.012, -0.012)])
lathe("VinylGrooves", profile, ink, vinyl, segments=160)
lathe("VinylLabel", [(0.312, 0.014), (0.012, 0.014)], label, vinyl, segments=160)
# A faint pressed center ring and a real spindle hole.
lathe(
    "VinylLabelPressRing",
    [(0.106, 0.0142), (0.104, 0.0142)],
    label_line,
    vinyl,
    segments=160,
)
lathe("VinylSpindleRim", [(0.017, 0.0143), (0.012, 0.0143)], ink, vinyl, segments=160)
# Printed label lettering is geometry, so it rotates with the record itself.
# Neutral pressing details avoid revealing the song before the round ends.
printed_ink = material("Label printed ink", (0.035, 0.013, 0.009), 0.85)
for name, body, y, size in [
    ("Brand", "SONGUESS", 0.20, 0.040),
    ("Speed", "33 1/3 RPM", 0.155, 0.019),
    ("Side", "SIDE A", -0.19, 0.033),
    ("Format", "STEREO", -0.235, 0.018),
]:
    lettering = bpy.data.curves.new(f"VinylLabel{name}", type="FONT")
    lettering.body = body
    lettering.size = size
    lettering.align_x = "CENTER"
    obj = bpy.data.objects.new(f"VinylLabel{name}", lettering)
    bpy.context.collection.objects.link(obj)
    obj.parent = vinyl
    obj.location = xyz(0, y, 0.0147)
    obj.rotation_euler = (math.pi / 2, 0, 0)
    lettering.materials.append(printed_ink)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.convert(target="MESH")
lathe(
    "VinylLabelPrint",
    [(0.275, 0.0143), (0.2735, 0.0143)],
    printed_ink,
    vinyl,
    segments=160,
)
# Asymmetric wear clusters remain readable outside the central play control.
scuff = material("Outer groove wear", (0.22, 0.23, 0.21), 0.58)
faint_scuff = material("Fine groove wear", (0.105, 0.115, 0.10), 0.66)
for index, (radius, start, sweep, thickness) in enumerate(
    [
        (0.85, 18, 24, 0.007),
        (0.80, 24, 29, 0.005),
        (0.75, 17, 18, 0.006),
        (0.69, 31, 21, 0.0045),
        (0.57, 27, 12, 0.004),
        (0.89, 137, 13, 0.006),
        (0.83, 132, 19, 0.0045),
        (0.77, 144, 11, 0.005),
        (0.64, 138, 16, 0.004),
        (0.87, 253, 9, 0.007),
        (0.81, 245, 22, 0.005),
        (0.72, 250, 14, 0.006),
        (0.61, 260, 11, 0.0045),
    ]
):
    vertices = []
    segments = 24
    for step in range(segments + 1):
        t = step / segments
        angle = math.radians(start + sweep * t)
        center = radius + 0.005 * math.sin(t * math.pi)
        half_width = thickness * math.sin(t * math.pi) / 2
        for r in (center - half_width, center + half_width):
            vertices.append((r * math.cos(angle), r * math.sin(angle), 0.0132))
    faces = [(i * 2, i * 2 + 1, i * 2 + 3, i * 2 + 2) for i in range(segments)]
    mesh(
        f"VinylOuterScuff{index}",
        vertices,
        faces,
        faint_scuff if index % 3 == 1 else scuff,
        vinyl,
    )
export(vinyl, "vinyl.glb")

# An editable, lit reveal reference scene. Assets retain their local origin in exports.
preview = bpy.data.scenes.new("Reveal reference")
bpy.context.window.scene = preview
# Put both objects in one assembly instead of estimating their world depths.
# At the final pose the record's local depth is zero, inside the jacket cavity.
assembly = root("RevealAssembly")
assembly.rotation_euler = (0.018, 0, -0.1)
reference_objects = {}
for original in (sleeve, vinyl):
    duplicate = original.copy()
    duplicate.name = original.name + "Reveal"
    preview.collection.objects.link(duplicate)
    duplicate.parent = assembly
    reference_objects[original.name] = duplicate
    for child in original.children_recursive:
        copied = child.copy()
        preview.collection.objects.link(copied)
        copied.parent = duplicate

preview.render.fps = 30
preview.frame_end = 52
for frame in range(1, 53):
    progress = (frame - 1) / 51
    flourish = min(progress / 0.55, 1)
    arrival = 1 - (1 - flourish) ** 3
    insertion = max((progress - 0.55) / 0.45, 0)
    tucked = 1 - (1 - insertion) ** 3
    sleeve_ref = reference_objects["Sleeve"]
    vinyl_ref = reference_objects["Vinyl"]
    sleeve_ref.location = xyz(
        -0.5 - (1 - arrival) * 3, math.sin(flourish * math.pi) * 0.24, 0
    )
    sleeve_ref.rotation_euler = (
        0,
        math.sin(flourish * math.pi) * 0.2,
        -math.tau * (1 - arrival),
    )
    vinyl_ref.location = xyz((0.5 + 1.08 * (1 - tucked)) * arrival, 0, 0)
    vinyl_ref.rotation_euler = (0, progress * 1.7 * math.tau / 4, 0)
    for obj in (sleeve_ref, vinyl_ref):
        obj.keyframe_insert(data_path="location", frame=frame)
        obj.keyframe_insert(data_path="rotation_euler", frame=frame)
preview.frame_set(52)
preview.world = bpy.data.worlds.new("Warm studio")
preview.world.use_nodes = True
preview.world.node_tree.nodes.get("Background").inputs["Color"].default_value = (
    0.14,
    0.14,
    0.12,
    1,
)
preview.world.node_tree.nodes.get("Background").inputs["Strength"].default_value = 0.5
for name, position, energy, size, color in [
    ("Softbox warm", (-3, 4, 5), 450, 4, (1, 0.89, 0.73)),
    ("Softbox rim", (3, 1, 3), 300, 2, (0.76, 0.86, 1)),
]:
    data = bpy.data.lights.new(name, "AREA")
    data.energy, data.shape, data.size, data.color = energy, "DISK", size, color
    obj = bpy.data.objects.new(name, data)
    preview.collection.objects.link(obj)
    obj.location = xyz(*position)
    obj.rotation_euler = (
        (Vector(xyz(0, 0, 0)) - obj.location).to_track_quat("-Z", "Y").to_euler()
    )
camera_data = bpy.data.cameras.new("Reveal camera")
camera = bpy.data.objects.new("Reveal camera", camera_data)
preview.collection.objects.link(camera)
camera.location = xyz(0, 0.1, 7)
camera.rotation_euler = (
    (Vector(xyz(0, 0, 0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
)
camera_data.type = "ORTHO"
camera_data.ortho_scale = 3.6
preview.camera = camera
for frame in range(1, 53):
    progress = (frame - 1) / 51
    insertion = max((progress - 0.55) / 0.45, 0)
    tucked = 1 - (1 - insertion) ** 3
    camera_data.ortho_scale = 3.6 / (0.56 + 0.44 * tucked)
    camera_data.keyframe_insert(data_path="ortho_scale", frame=frame)
preview.render.engine = "CYCLES"
preview.cycles.samples = 24
preview.render.resolution_x = 900
preview.render.resolution_y = 700
preview.render.resolution_percentage = 100
preview.render.film_transparent = True
preview.render.image_settings.file_format = "PNG"
preview.render.filepath = str(HERE / "previews/shared-assets.png")
preview.view_settings.view_transform = "AgX"
# Independent four-second loop study shares the exported record geometry.
spin = bpy.data.scenes.new("Spin reference")
spin.world = preview.world
spin.render.fps = 30
spin.frame_end = 54
spinner = vinyl.copy()
spinner.name = "VinylSpin"
spin.collection.objects.link(spinner)
for child in vinyl.children_recursive:
    copied = child.copy()
    spin.collection.objects.link(copied)
    copied.parent = spinner
for obj in preview.objects:
    if obj.type in {"CAMERA", "LIGHT"}:
        copied = obj.copy()
        spin.collection.objects.link(copied)
        if obj.type == "CAMERA":
            spin.camera = copied
# Blender Y corresponds to negative web Z: clockwise viewed from the front.
spinner.rotation_euler = (0, 0, -0.07)
spinner.keyframe_insert(data_path="rotation_euler", index=1, frame=1)
spinner.rotation_euler.y = math.tau
spinner.keyframe_insert(data_path="rotation_euler", index=1, frame=55)
action = spinner.animation_data.action
for layer in action.layers:
    for strip in layer.strips:
        for channelbag in strip.channelbags:
            for curve in channelbag.fcurves:
                for keyframe in curve.keyframe_points:
                    keyframe.interpolation = "LINEAR"
                curve.modifiers.new("CYCLES")
# Counter-rotate only the sheen UVs; physical groove wear follows the record.
for obj in spinner.children:
    if obj.type != "MESH":
        continue
    for slot in obj.material_slots:
        if slot.material != ink:
            continue
        reflective = ink.copy()
        slot.link = "OBJECT"
        slot.material = reflective
        nodes = reflective.node_tree.nodes
        links = reflective.node_tree.links
        tex = next(node for node in nodes if node.type == "TEX_IMAGE")
        uv = nodes.new("ShaderNodeTexCoord")
        subtract = nodes.new("ShaderNodeVectorMath")
        subtract.operation = "SUBTRACT"
        subtract.inputs[1].default_value = (0.5, 0.5, 0)
        mapping = nodes.new("ShaderNodeMapping")
        add = nodes.new("ShaderNodeVectorMath")
        add.operation = "ADD"
        add.inputs[1].default_value = (0.5, 0.5, 0)
        links.new(uv.outputs["UV"], subtract.inputs[0])
        links.new(subtract.outputs["Vector"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], add.inputs[0])
        links.new(add.outputs["Vector"], tex.inputs["Vector"])
        driver = mapping.inputs["Rotation"].driver_add("default_value", 2).driver
        variable = driver.variables.new()
        variable.name = "angle"
        variable.targets[0].id = spinner
        variable.targets[0].data_path = "rotation_euler[1]"
        driver.expression = "-angle"
spin.frame_set(1)
# A handling study matching the browser: return, lift, pull, wrist turn, settle.
carousel = bpy.data.scenes.new("Carousel reference")
carousel.world = preview.world
carousel.render.fps = 30
carousel.frame_end = 31
pixel = 2 / 172.8


def smooth(value):
    value = max(0, min(value, 1))
    return value * value * (3 - 2 * value)


def mix(start, end, progress):
    return start + (end - start) * progress


def shelf_pose(offset):
    depth = abs(offset)
    side = 1 if offset > 0 else -1 if offset < 0 else 0
    folded = min(depth, 1)
    return (
        side * (76.8 * folded + max(depth - 1, 0) * 33.6) * pixel,
        (20 * (1 - folded) - 7.2 * folded) * pixel,
        (110 * (1 - folded) - depth * 160) * pixel,
        0.045 * (1 - folded),
        folded * 1.19 - 0.12 * (1 - folded),
        1.2 + (0.86 - 1.2) * folded,
    )


for index in range(-3, 4):
    jacket_ref = sleeve.copy()
    jacket_ref.name = f"SleeveCarousel_{index:+d}"
    carousel.collection.objects.link(jacket_ref)
    for child in sleeve.children_recursive:
        copied = child.copy()
        carousel.collection.objects.link(copied)
        copied.parent = jacket_ref
    inspecting = index == 1
    returning = index == 0
    duration = 0.55 if inspecting else 0.39 if returning else 0.45
    delay = 0.425 if inspecting else 0
    start = shelf_pose(index)
    end = shelf_pose(index - 1)
    direction = 1 if index >= 0 else -1
    for step in range(121):
        frame = 1 + step / 4
        t = max(0, min(((frame - 1) / 30 - delay) / duration, 1))
        travel = smooth(t / 0.35) if inspecting else smooth(t)
        turn = (
            smooth((t - 0.45) / 0.35)
            if inspecting
            else smooth(t / 0.45)
            if returning
            else smooth(t)
        )
        lift = smooth(t / 0.3) if inspecting else smooth(t)
        grip = math.sin(math.pi * t)
        settle = math.sin(math.pi * (t - 0.7) / 0.3) if inspecting and t > 0.7 else 0
        x = mix(start[0], end[0], travel)
        y = mix(start[1], end[1], lift) + grip * 16 * pixel * (
            0.5 if inspecting else 0.18 if returning else 0
        )
        z = mix(
            start[2],
            end[2],
            smooth(t / 0.35) if inspecting else smooth((t - 0.5) / 0.5),
        )
        z += grip * 22 * pixel if inspecting else 0
        rx = mix(start[3], end[3], smooth(t)) + (grip * 0.07 if inspecting else 0)
        ry = mix(start[4], end[4], turn) - settle * 0.12
        rz = -direction * grip * (0.045 if inspecting else 0.025 if returning else 0)
        scale = mix(start[5], end[5], travel) + (grip * 0.036 if inspecting else 0)
        jacket_ref.location = xyz(x, y, z)
        jacket_ref.rotation_euler = (rx, -rz, ry)
        jacket_ref.scale = (scale, scale, scale)
        for path in ("location", "rotation_euler", "scale"):
            jacket_ref.keyframe_insert(data_path=path, frame=frame)
# Resolve each frame with the same oriented-volume clearance as the browser.
bpy.context.window.scene = carousel
jackets = [carousel.objects[f"SleeveCarousel_{i:+d}"] for i in range(-3, 4)]
ordered = sorted(
    jackets,
    key=lambda obj: (
        abs(int(obj.name.split("_")[1]) - 1)
        if int(obj.name.split("_")[1]) not in (0, 1)
        else -1
        if int(obj.name.split("_")[1]) == 0
        else -2
    ),
    reverse=True,
)
for step in range(121):
    frame = 1 + step / 4
    carousel.frame_set(int(frame), subframe=frame % 1)
    placed = []
    for jacket in ordered:
        for _ in range(32):
            shift = 0
            for other in placed:
                clearance = forward_clearance(jacket, other, margin=2 * pixel)
                jacket.location.y -= clearance
                shift += clearance
                bpy.context.view_layer.update()
            if shift == 0:
                break
        placed.append(jacket)
        jacket.keyframe_insert(data_path="location", frame=frame)
# Linear subframes avoid spline overshoot between the checked poses.
for jacket in jackets:
    for layer in jacket.animation_data.action.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                for curve in channelbag.fcurves:
                    for key in curve.keyframe_points:
                        key.interpolation = "LINEAR"
for obj in preview.objects:
    if obj.type in {"CAMERA", "LIGHT"}:
        copied = obj.copy()
        carousel.collection.objects.link(copied)
        if obj.type == "CAMERA":
            copied.data = obj.data.copy()
            copied.data.animation_data_clear()
            copied.data.ortho_scale = 6.4
            carousel.camera = copied
carousel.frame_set(31)
# An unrotated assembly makes panel/record clearance easy to inspect from the side.
inspection = bpy.data.scenes.new("Sleeve fit inspection")
bpy.context.window.scene = inspection
inspection.world = preview.world
fit_assembly = root("FitAssembly")
for original, x in [(sleeve, -0.5), (vinyl, 0.5)]:
    duplicate = original.copy()
    duplicate.name = original.name + "Fit"
    inspection.collection.objects.link(duplicate)
    duplicate.parent = fit_assembly
    duplicate.location = xyz(x, 0, 0)
    for child in original.children_recursive:
        copied = child.copy()
        inspection.collection.objects.link(copied)
        copied.parent = duplicate
# Save the evaluated final reveal explicitly, after creating all other scenes.
bpy.context.window.scene = preview
preview.frame_set(52)
bpy.context.view_layer.update()
bpy.ops.object.select_all(action="DESELECT")
assembly.select_set(True)
bpy.context.view_layer.objects.active = assembly
for screen in bpy.data.screens:
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.region_3d.view_perspective = "CAMERA"
bpy.ops.wm.save_as_mainfile(filepath=str(HERE / "songuess-assets.blend"), compress=True)
bpy.ops.render.render(write_still=True)

# A standalone front view makes the pressing's surface and label easy to review.
bpy.context.window.scene = spin
spin.frame_set(1)
spin.camera.data = spin.camera.data.copy()
spin.camera.data.animation_data_clear()
spin.camera.data.type = "ORTHO"
spin.camera.data.ortho_scale = 2.1
spin.camera.location = xyz(0, 0, 7)
spin.camera.rotation_euler = (
    (Vector((0, 0, 0)) - spin.camera.location).to_track_quat("-Z", "Y").to_euler()
)
spin.render.engine = "CYCLES"
spin.cycles.samples = 24
spin.render.resolution_x = spin.render.resolution_y = 640
spin.render.resolution_percentage = 100
spin.render.film_transparent = True
spin.render.filepath = str(HERE / "previews" / "vinyl.png")
bpy.ops.render.render(write_still=True)
bpy.context.window.scene = preview

report = {}
for obj, filename in [(sleeve, "sleeve.glb"), (vinyl, "vinyl.glb")]:
    report[filename] = {
        "bytes": (OUTPUT / filename).stat().st_size,
        "triangles": sum(
            len(p.vertices) - 2
            for child in obj.children_recursive
            if child.type == "MESH"
            for p in child.data.polygons
        ),
        "front_axis": "+Z",
        "up_axis": "+Y",
        "root": obj.name,
    }
(HERE / "asset-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
