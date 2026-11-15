"""Single source of truth for skill metadata, objective membership, and runner dispatch.

All starter, daily, and competition tool names, their target-position safety check
requirements, and their execution runner slots are registered here so that
agent/tools.py, agent/daily_survival.py, core/competition_tasks.py, and
core/survival_harness.py stay synchronized.
"""

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet


@dataclass(frozen=True)
class SkillSpec:
    name: str
    objectives: FrozenSet[str]
    runner_slot: str
    checks_destination: bool = False


_STARTER_DAILY_COMP = frozenset({"starter", "daily", "competition"})
_STARTER_ONLY = frozenset({"starter"})
_DAILY_COMP = frozenset({"daily", "competition"})
_COMP_ONLY = frozenset({"competition"})


SKILL_SPECS = (
    # Core starter + daily + competition skills
    SkillSpec("gather_block", _STARTER_DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("craft_item", _STARTER_DAILY_COMP, "skills", checks_destination=False),
    SkillSpec("place_item", _STARTER_DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("eat_food", _STARTER_DAILY_COMP, "skills", checks_destination=False),
    SkillSpec("move_to", _STARTER_DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("retreat_from", _STARTER_DAILY_COMP, "skills", checks_destination=False),
    SkillSpec("auto_sleep", _STARTER_DAILY_COMP, "sleep", checks_destination=False),
    SkillSpec("wait", _STARTER_DAILY_COMP, "meta", checks_destination=False),
    # Starter-only terminal signal
    SkillSpec("goal_complete", _STARTER_ONLY, "meta", checks_destination=False),
    # Daily + competition world/container/farming skills
    SkillSpec("gather_resource", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("harvest_crop", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("gather_seeds", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("clear_foliage", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("plant_crop", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("till_soil", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("hunt_animal", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("equip_armor", _DAILY_COMP, "skills", checks_destination=False),
    SkillSpec("smelt_start", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("smelt_collect", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("inspect_container", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("store_items", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("take_items", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("collect_drops", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("place_bed", _DAILY_COMP, "skills", checks_destination=True),
    SkillSpec("place_door", _DAILY_COMP, "skills", checks_destination=True),
    # Daily + competition shelter, batch, door, and positioning skills
    SkillSpec("plan_shelter", _DAILY_COMP, "shelter_skills", checks_destination=False),
    SkillSpec("inspect_shelter", _DAILY_COMP, "shelter_skills", checks_destination=False),
    SkillSpec("build_shelter_step", _DAILY_COMP, "shelter_skills", checks_destination=True),
    SkillSpec("gather_materials", _DAILY_COMP, "batch_skills", checks_destination=False),
    SkillSpec("build_shelter_batch", _DAILY_COMP, "batch_skills", checks_destination=True),
    SkillSpec("set_door", _DAILY_COMP, "door_skills", checks_destination=True),
    SkillSpec("position_for_task", _DAILY_COMP, "positioning_skills", checks_destination=True),
    # Competition task lifecycle tools
    SkillSpec("select_task", _COMP_ONLY, "competition", checks_destination=False),
    SkillSpec("update_progress", _COMP_ONLY, "competition", checks_destination=False),
    SkillSpec("finish_task", _COMP_ONLY, "competition", checks_destination=False),
    SkillSpec("force_exit_task", _COMP_ONLY, "competition", checks_destination=False),
    SkillSpec("end_competition", _COMP_ONLY, "competition", checks_destination=False),
)

SKILL_REGISTRY: Dict[str, SkillSpec] = {spec.name: spec for spec in SKILL_SPECS}

_OBJECTIVE_SKILLS: Dict[str, FrozenSet[str]] = {
    objective: frozenset(spec.name for spec in SKILL_SPECS if objective in spec.objectives)
    for objective in ("starter", "daily", "competition")
}

_STARTER_DESTINATION_SKILLS: FrozenSet[str] = frozenset(
    spec.name for spec in SKILL_SPECS if "starter" in spec.objectives and spec.checks_destination
)
_DAILY_DESTINATION_SKILLS: FrozenSet[str] = frozenset(
    spec.name for spec in SKILL_SPECS if ("starter" in spec.objectives or "daily" in spec.objectives) and spec.checks_destination
)


def skills_for_objective(objective: str) -> FrozenSet[str]:
    """Return the frozenset of registered skill/tool names for an objective."""
    if objective not in _OBJECTIVE_SKILLS:
        raise ValueError(f"Unsupported survival objective: {objective}")
    return _OBJECTIVE_SKILLS[objective]


def destination_checked_skills(*, daily: bool = False) -> FrozenSet[str]:
    """Return skill names whose target pos/entity_id must pass local hazard/hostile checks."""
    return _DAILY_DESTINATION_SKILLS if daily else _STARTER_DESTINATION_SKILLS


def resolve_skill_runner(harness: Any, skill_name: str) -> Any:
    """Resolve the skill runner instance on SurvivalHarness for a given skill name."""
    spec = SKILL_REGISTRY.get(skill_name)
    if spec is None or not getattr(harness, "daily", False):
        return harness.skills
    slot = spec.runner_slot
    if slot in {"shelter_skills", "batch_skills", "door_skills", "positioning_skills"}:
        runner = getattr(harness, slot, None)
        if runner is not None:
            return runner
    return harness.skills
