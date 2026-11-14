"""Native function tools for implemented, locally verified survival actions."""

from skills.survival import SurvivalSkills


def _position(integer=False):
    return {"type": "array", "items": {"type": "integer" if integer else "number",
                                       "minimum": -30000000, "maximum": 30000000},
            "minItems": 3, "maxItems": 3, "description": "World coordinates [x, y, z]."}


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {
            **properties, "reason": {"type": "string", "maxLength": 1000,
                                      "description": "Brief purpose for this one action."}},
            "required": list(required), "additionalProperties": False}}}


_ITEM = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$", "maxLength": 64}

TOOL_DEFINITIONS = [
    _tool("gather_block", "Gather ONE observed natural log, stone/cobblestone or coal ore. "
          "A usable pickaxe is required for stone/ore. Never mine the player's floor or overhead column. "
          "Actual block removal and inventory gain are verified locally.",
          {"pos": _position(True), "expected_name": {"type": "string", "enum": sorted(SurvivalSkills.GATHERABLE)}},
          ("pos", "expected_name")),
    _tool("craft_item", "Craft an item using inventory materials. count is the desired INCREASE, "
          "not the final inventory total; recipes may produce batches. For 3x3 recipes first place "
          "a crafting table and move within 3 blocks. No materials are granted.",
          {"item": _ITEM, "count": {"type": "integer", "minimum": 1, "maximum": 64, "default": 1}}, ("item",)),
    _tool("place_item", "Place one existing inventory item at an integer block position. "
          "An unobserved workspace may be proposed; the skill verifies clear space and safe support before placing.",
          {"item": {"type": "string", "enum": sorted(SurvivalSkills.PLACEABLE)}, "pos": _position(True)},
          ("item", "pos")),
    _tool("eat_food", "Eat one existing safe food item. Inventory consumption and food increase are verified.",
          {"item": {"type": "string", "enum": sorted(SurvivalSkills.FOODS)}}, ("item",)),
    _tool("move_to", "Move within 32 blocks to a workstation/resource or explore. "
          "One finite move only; reobserve before the next action. Exploration is subject to harness limits.",
          {"pos": _position(), "range": {"type": "number", "minimum": 0.5, "maximum": 4, "default": 2}}, ("pos",)),
    _tool("retreat_from", "Retreat from an observed hostile entity position. "
          "The skill verifies a safe supported corridor; it may refuse if no safe route exists.",
          {"pos": _position(), "distance": {"type": "number", "minimum": 2, "maximum": 12, "default": 8}}, ("pos",)),
    _tool("auto_sleep", "Use the configured safe sleep skill at night or during a storm, "
          "or wait for an existing sleep to finish. No safety options can be disabled.", {}),
    _tool("wait", "Wait briefly for a new observation when no safe useful action is available.",
          {"seconds": {"type": "number", "minimum": 0.25, "maximum": 30, "default": 2}}),
    _tool("goal_complete", "Finish ONLY if current observed inventory/workstations verify "
          "stone pickaxe, stone axe, stone sword, and furnace. Historical claims are insufficient.", {}),
]

TOOL_SCHEMAS = {entry["function"]["name"]: entry["function"]["parameters"] for entry in TOOL_DEFINITIONS}
