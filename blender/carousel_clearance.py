"""Physical jacket clearance, with conservative oriented boxes and paper margin."""

from mathutils import Vector


def forward_clearance(a, b, margin=0):
    axes_a = [a.matrix_world.to_3x3().col[i].normalized() for i in range(3)]
    axes_b = [b.matrix_world.to_3x3().col[i].normalized() for i in range(3)]
    axes = axes_a + axes_b
    for x in axes_a:
        for y in axes_b:
            cross = x.cross(y)
            if cross.length_squared > 1e-10:
                axes.append(cross.normalized())
    delta = a.matrix_world.translation - b.matrix_world.translation
    # Blender coordinates: thickness is Y, cover width/height are X/Z.
    half = (1, 0.04, 1)
    forward = Vector((0, -1, 0))
    exit_distance = float("inf")
    for axis in axes:
        radius = (
            sum(
                size
                * (
                    a.scale[i] * abs(axis.dot(axes_a[i]))
                    + b.scale[i] * abs(axis.dot(axes_b[i]))
                )
                for i, size in enumerate(half)
            )
            + margin
        )
        distance = delta.dot(axis)
        if abs(distance) >= radius:
            return 0
        direction = axis.dot(forward)
        if abs(direction) > 1e-8:
            sign = 1 if direction > 0 else -1
            exit_distance = min(
                exit_distance, (radius - sign * distance) / abs(direction)
            )
    return exit_distance + 0.0001
