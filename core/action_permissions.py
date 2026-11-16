"""Explicitly supported advisory overrides; no arbitrary action dispatch."""

FORCEABLE_ACTIONS = frozenset({
    "gather_block", "gather_resource", "harvest_crop", "gather_seeds", "clear_foliage",
    "craft_item", "place_item", "place_bed", "place_door", "position_for_task",
    "plan_shelter", "inspect_shelter", "build_shelter_step",
})
CONTROL_ACTIONS = frozenset({"report_blocked", "force_attempt"})
