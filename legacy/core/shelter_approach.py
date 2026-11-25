"""Use a freshly inspected empty entrance; never route through the house walls."""

import math

from core.domain import parse_pos
from core.structure_spec import SHELTER_5X5_SPEC, transform_rel_pos
from skills.positioning import PositioningSkills


def entrance_geometry(shelter):
    """Return observed entrance geometry, not a promise that its route is safe."""
    if not isinstance(shelter, dict):
        return None
    anchor = parse_pos(shelter.get("pos"), integer=True)
    orientation = shelter.get("orientation", "north")
    geometry, door = shelter.get("geometry"), shelter.get("door")
    if (anchor is None or not isinstance(orientation, str) or orientation not in {"north", "east", "south", "west"}
            or shelter.get("fresh") is not True or shelter.get("inside") is not False
            or type(shelter.get("missing_blocks")) is not int or not 0 <= shelter["missing_blocks"] < 55
            or shelter.get("hazards") != [] or shelter.get("occupied_blueprint_cells") != []
            or not isinstance(geometry, dict) or geometry.get("floor_verified") is not True
            or geometry.get("exit_verified") is not True or not isinstance(door, dict)
            or door.get("frame_verified") is not False or door.get("reason") != "door_missing"):
        return None
    doorway = transform_rel_pos((0, 0, 2), anchor, orientation, integer=True)
    if door.get("pos") != doorway:
        return None
    outside = transform_rel_pos(SHELTER_5X5_SPEC.anchors["doorway_outside"].rel_pos,
                                anchor, orientation, integer=False)
    inside = transform_rel_pos(SHELTER_5X5_SPEC.anchors["doorway_inside"].rel_pos,
                               anchor, orientation, integer=False)
    return {"anchor": anchor, "doorway": doorway, "outside": outside, "inside": inside}


def avoids_shell(origin, target, entrance):
    """Conservative negative filter: the only permitted wall column is the opening.

    This deliberately excludes unbuilt wall cells too. It grants no movement
    authority; fresh candidate support and the skill's complete sweep still win.
    """
    origin, target = parse_pos(origin), parse_pos(target)
    if origin is None or target is None:
        return False
    x, y, z = entrance["anchor"]
    if min(origin[1], target[1]) > y + 2 or max(origin[1], target[1]) + 2 < y:
        return True
    door_x, _, door_z = entrance["doorway"]
    for cx, cz in PositioningSkills._columns(origin, target):
        boundary = (x - 2 <= cx <= x + 2 and z - 2 <= cz <= z + 2
                    and (cx in (x - 2, x + 2) or cz in (z - 2, z + 2)))
        if (boundary and (cx, cz) != (door_x, door_z)
                and not PositioningSkills.leaves_start_margin(origin, target, (cx, cz))):
            return False
    return True


def entrance_waypoint(origin, entrance):
    """Align outside before crossing the narrow doorway, including rounded poses."""
    inside, outside = entrance["inside"], entrance["outside"]
    if math.dist(origin, inside) <= 3 and avoids_shell(origin, inside, entrance):
        return list(inside)
    return list(outside)
