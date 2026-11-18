"""Persist an actually started shelter as intent, never as building authority."""

import copy
import json
from dataclasses import asdict, is_dataclass

from core.domain import parse_pos
from core.structure_spec import SHELTER_5X5_SPEC, SHELTER_BUILDING_BLOCKS, transform_rel_pos


ORIENTATIONS = {"north", "east", "south", "west"}
SHELL = tuple(cell for cell in SHELTER_5X5_SPEC.cells
              if cell.stage in {"lower_walls", "upper_walls", "roof"})


def _position(value):
    return parse_pos(value, integer=True)


def project_anchor(value, *, scope=None, dimension=None):
    """Validate a scoped construction hint; callers still need fresh geometry."""
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("source"), str) or value["source"] not in {"verified_build", "verified_build_event"}
            or not isinstance(value.get("scope"), dict)
            or scope is not None and value["scope"] != scope
            or not isinstance(value.get("dimension"), str)
            or dimension is not None and value["dimension"] != dimension
            or not isinstance(value.get("orientation"), str) or value["orientation"] not in ORIENTATIONS
            or not isinstance(value.get("material"), str) or value["material"] not in SHELTER_BUILDING_BLOCKS
            or _position(value.get("pos")) is None
            or type(value.get("missing_blocks_hint")) is not int
            or not 0 <= value["missing_blocks_hint"] <= len(SHELL)
            or type(value.get("door_needed_hint")) is not bool
            or type(value.get("complete", False)) is not bool):
        return None
    return copy.deepcopy(value)


def active_project(value, *, scope=None, dimension=None):
    project = project_anchor(value, scope=scope, dimension=dimension)
    return project if project and project.get("complete") is not True else None


def same_site(project, value):
    return (isinstance(value, dict) and value.get("pos") == project["pos"]
            and value.get("orientation", "north") == project["orientation"]
            and value.get("dimension", project["dimension"]) == project["dimension"])


def completion_observed(project, data):
    """A complete closed shell releases the project lock, never creates safety rights."""
    if (not same_site(project, data) or data.get("fresh") is not True
            or data.get("dimension") != project["dimension"]):
        return False
    geometry, door = data.get("geometry"), data.get("door")
    return (data.get("shelter_verified") is True and data.get("structure_complete") is True
            and isinstance(geometry, dict) and all(geometry.get(key) is True for key in
                ("floor_verified", "walls_complete", "roof_complete", "exit_verified"))
            and isinstance(door, dict) and door.get("frame_verified") is True and door.get("open") is False
            and data.get("hazards") == [] and data.get("occupied_blueprint_cells") == [])


def verified_build_anchor(decision, result, scope):
    """Only a confirmed one-block placement can turn a survey into a project."""
    dec = asdict(decision) if is_dataclass(decision) else decision
    res = asdict(result) if is_dataclass(result) else result
    if not isinstance(dec, dict) or not isinstance(res, dict):
        return None
    args, data = dec.get("args"), res.get("data")
    if (dec.get("name") != "build_shelter_step" or res.get("success") is not True
            or res.get("error_code") is not None or not isinstance(args, dict) or not isinstance(data, dict)
            or data.get("executed") is not True or data.get("action_state") == "unknown"
            or data.get("step_complete") is not True or type(data.get("used")) is not int or data["used"] != 1
            or data.get("fresh") is not True or data.get("placed") != args.get("material")
            or not isinstance(args.get("material"), str) or args["material"] not in SHELTER_BUILDING_BLOCKS
            or _position(args.get("pos")) is None or data.get("pos") != args["pos"]
            or not isinstance(args.get("orientation", "north"), str) or args.get("orientation", "north") not in ORIENTATIONS
            or data.get("orientation", "north") != args.get("orientation", "north")
            or not isinstance(data.get("dimension"), str)):
        return None
    geometry = data.get("geometry") or {}
    door = data.get("door", {})
    if (not isinstance(geometry, dict) or geometry.get("floor_verified") is not True
            or geometry.get("exit_verified") is not True or data.get("hazards") != []
            or data.get("occupied_blueprint_cells") != [] or not isinstance(door, dict)):
        return None
    cells = [transform_rel_pos(cell.rel_pos, args["pos"], args.get("orientation", "north"), integer=True) for cell in SHELL]
    if data.get("placed_pos") not in cells:
        return None
    candidate = {"version": 1, "scope": copy.deepcopy(scope), "pos": list(args["pos"]),
        "orientation": args.get("orientation", "north"), "dimension": data["dimension"],
        "material": args["material"], "source": "verified_build",
        "missing_blocks_hint": data.get("missing_blocks"),
        "door_needed_hint": door.get("frame_verified") is not True,
        "requires_revalidation": True, "complete": False}
    candidate["complete"] = completion_observed(candidate, data)
    return project_anchor(candidate, scope=scope)


def recover_project(journal):
    """Read the existing scoped event tail, with strict evidence and size limits.

    Historical surveys and task summaries cannot create this anchor. The event
    log is not rewritten; the harness records and checkpoints any migration.
    """
    if journal.state.get("scope") != journal.scope:
        return None
    path = getattr(journal, "events_path", None)
    if path is None or not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 8 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read().splitlines()[-5000:]
    except OSError:
        return None
    completions = []
    for result in (journal.state.get("last_result"), (journal.state.get("context") or {}).get("last_result")):
        if (isinstance(result, dict) and result.get("success") is True and not result.get("error_code")
                and isinstance(result.get("data"), dict) and result["data"].get("action_state") != "unknown"):
            completions.append(result["data"])
    for line in reversed(lines):
        try:
            event = json.loads(line)
            data = event.get("data") or {}
            if (event.get("event") == "SHELTER_PROJECT_COMPLETED" and isinstance(data, dict)
                    and isinstance(data.get("shelter"), dict) and len(completions) < 32):
                completions.append(data["shelter"])
            if event.get("event") != "STEP_RESULT" or not isinstance(data, dict):
                continue
            result = data.get("result") or {}
            detail = result.get("data") or {}
            if (isinstance(result, dict) and result.get("success") is True and not result.get("error_code")
                    and isinstance(detail, dict) and detail.get("action_state") != "unknown"
                    and detail.get("shelter_verified") is True and len(completions) < 32):
                completions.append(detail)
            candidate = verified_build_anchor(data.get("decision"), data.get("result"), journal.scope)
            if candidate:
                candidate["complete"] = candidate["complete"] or any(completion_observed(candidate, row) for row in completions)
                candidate.update(source="verified_build_event", recovered_from="STEP_RESULT",
                                 evidence_timestamp=event.get("timestamp"))
                return candidate
        except (ValueError, TypeError, AttributeError):
            continue
    return None
