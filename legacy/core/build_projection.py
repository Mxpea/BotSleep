"""Load local survival projections and map their block palette into the world."""

from collections import Counter
from dataclasses import dataclass
import hashlib
from itertools import product
import json
from pathlib import Path
import re

from core.domain import WOOD_SPECIES
from core.structure_spec import transform_rel_pos


DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "projections"
MAX_CELLS = 768


def wood_material(item):
    """Return the species and role of a supported timber block."""
    if isinstance(item, str):
        species, _, role = item.rpartition("_")
        if species in WOOD_SPECIES and role in {"log", "planks", "door"}:
            return species, role
    return None


@dataclass(frozen=True)
class ProjectedBlock:
    pos: tuple[int, int, int]
    item: str
    stage: str

    def with_wood(self, species):
        material = wood_material(self.item)
        if species is None or material is None or material[0] == species:
            return self
        return ProjectedBlock(self.pos, f"{species}_{material[1]}", self.stage)


@dataclass(frozen=True)
class BuildProjection:
    name: str
    digest: str
    blocks: tuple[ProjectedBlock, ...]

    @property
    def materials(self) -> dict[str, int]:
        return dict(Counter(block.item for block in self.blocks))

    @property
    def uses_wood(self) -> bool:
        return any(wood_material(block.item) is not None for block in self.blocks)

    def preferred_wood(self, counts, nearby):
        """Prefer enough stock for the whole project, then held and nearby timber."""
        required = Counter(material[1] for block in self.blocks if (material := wood_material(block.item)))
        if not required:
            return None
        trunks = {species: set() for species in WOOD_SPECIES}
        for row in nearby:
            material = wood_material(row.get("name"))
            if material is not None and material[1] == "log":
                x, _, z = row["pos"]
                trunks[material[0]].add((x, z))

        def score(species):
            logs = counts.get(f"{species}_log", 0)
            planks = counts.get(f"{species}_planks", 0)
            doors = counts.get(f"{species}_door", 0)
            ready = all(counts.get(f"{species}_{role}", 0) >= amount for role, amount in required.items())
            timber = logs + planks / 4 + 3 * len(trunks[species])
            usable = ready or bool(logs or trunks[species] or (not required["log"] and planks))
            return usable, ready, timber, doors

        choice = max(WOOD_SPECIES, key=score)
        return choice if score(choice)[0] else None

    def at(self, anchor, orientation, wood_species=None) -> tuple[ProjectedBlock, ...]:
        return tuple(ProjectedBlock(tuple(transform_rel_pos(block.pos, anchor, orientation, integer=True)),
                                    block.item, block.stage).with_wood(wood_species) for block in self.blocks)


def _point(value):
    if (not isinstance(value, list) or len(value) != 3
            or any(type(component) is not int or not -48 <= component <= 48 for component in value)):
        raise ValueError("Projection positions must be three integers within 48 blocks of the anchor")
    return tuple(value)


def load_projection(name: str) -> BuildProjection:
    """Expand ordered local blocks and inclusive boxes in a versioned JSON preset."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
        raise ValueError("Invalid projection preset name")
    path = DIRECTORY / f"{name}.json"
    with path.open("rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("Projection preset is too large")
    data = json.loads(raw)
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(data.get("stages"), list)):
        raise ValueError("Expected a version 1 projection with ordered stages")
    blocks = {}
    for stage in data["stages"]:
        if (not isinstance(stage, dict) or not isinstance(stage.get("name"), str)
                or not isinstance(stage.get("blocks"), list)):
            raise ValueError("Invalid projection stage")
        for row in stage["blocks"]:
            if not isinstance(row, dict):
                raise ValueError("Invalid projected block")
            item = row.get("item")
            if not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item) or item in {
                    "air", "water", "lava", "cave_air", "void_air"}:
                raise ValueError("Projection requires a placeable item name")
            low = _point(row.get("at") if "at" in row else row.get("from"))
            high = low if "at" in row else _point(row.get("to"))
            if any(a > b for a, b in zip(low, high)):
                raise ValueError("Projection box is inverted")
            exclusions = row.get("except", [])
            if not isinstance(exclusions, list):
                raise ValueError("Projection exclusions must be a list of coordinates")
            skip = {_point(pos) for pos in exclusions}
            positions = product(*(range(a, b + 1) for a, b in zip(low, high)))
            for pos in positions:
                if pos in skip:
                    continue
                if pos in blocks or len(blocks) >= MAX_CELLS:
                    raise ValueError(f"Projection has a duplicate cell or exceeds {MAX_CELLS} blocks: {pos}")
                blocks[pos] = ProjectedBlock(pos, item, stage["name"])
    if not blocks:
        raise ValueError("Projection contains no blocks")
    return BuildProjection(name, hashlib.sha256(raw).hexdigest()[:16], tuple(blocks.values()))
