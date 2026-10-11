"""Check the saved reveal assembly and clearance throughout record insertion."""

import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from carousel_clearance import forward_clearance

scene = bpy.data.scenes["Reveal reference"]
assert bpy.context.scene == scene, "Source must open in the final reveal scene"
assert scene.frame_current == scene.frame_end == 52, (
    "Source must open at the final pose"
)
sleeve = scene.objects["SleeveReveal"]
vinyl = scene.objects["VinylReveal"]
assert sleeve.parent == vinyl.parent == scene.objects["RevealAssembly"]

for frame in range(30, 53):
    scene.frame_set(frame)
    inverse = sleeve.matrix_world.inverted()
    for obj in vinyl.children:
        if obj.type != "MESH":
            continue
        for vertex in obj.data.vertices:
            position = inverse @ obj.matrix_world @ vertex.co
            if -1 <= position.x <= 1 and -1 <= position.z <= 1:
                assert abs(position.y) < 0.026, (frame, obj.name, position.y)

scene.frame_set(52)
assert abs(vinyl.location.y) < 1e-6 and abs(sleeve.location.y) < 1e-6
assert "Carousel reference" in bpy.data.scenes
print(
    "Saved reveal and insertion clearance verified; disc is inside both jacket panels."
)

carousel = bpy.data.scenes["Carousel reference"]
bpy.context.window.scene = carousel
jackets = [obj for obj in carousel.objects if obj.name.startswith("SleeveCarousel_")]
assert len(jackets) == 7
for step in range(241):
    frame = 1 + step / 8
    carousel.frame_set(int(frame), subframe=frame % 1)
    for index, jacket in enumerate(jackets):
        for other in jackets[index + 1 :]:
            assert forward_clearance(jacket, other) == 0, (
                "Intersecting carousel jackets",
                frame,
                jacket.name,
                other.name,
            )
print("All seven carousel jackets clear one another at every frame and eighth-frame.")
