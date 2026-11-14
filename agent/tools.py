"""Native function tools for implemented, locally verified survival actions."""

from skills.survival import SurvivalSkills
import copy


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


def tools_for_objective(objective="starter"):
    """Expose only the selected runtime's executable skills, never future plans."""
    tools = copy.deepcopy(TOOL_DEFINITIONS)
    if objective != "daily":
        return tools
    from skills.daily_survival import DailySurvivalSkills as Daily
    tools = [tool for tool in tools if tool["function"]["name"] != "goal_complete"]
    for tool in tools:
        function = tool["function"]
        if function["name"] in {"place_item", "eat_food"}:
            values = Daily.PLACEABLE if function["name"] == "place_item" else Daily.FOODS
            function["parameters"]["properties"]["item"]["enum"] = sorted(values)
    pos = {"pos": _position(True)}
    count = {"type": "integer", "minimum": 1, "maximum": 64}
    small_count = {**count, "maximum": 16}
    entity_id = {"type": "integer", "minimum": 0}
    def add(name, description, properties, required=None):
        tools.append(_tool(name, description, properties, tuple(properties) if required is None else required))
    add("gather_resource", "Mine ONE currently observed iron ore with a stone-or-better pickaxe; block removal and raw iron gain are verified.",
        {**pos, "expected_name": {"type": "string", "enum": sorted(Daily.IRON)}})
    add("harvest_crop", "Harvest ONE observed mature crop; maturity, removal and produce gain are checked. Replant when seeds are available.",
        {**pos, "expected_name": {"type": "string", "enum": sorted(Daily.CROPS)}})
    add("gather_seeds", "Break ONE currently observed short_grass or tall_grass plant within checked reach to try for wheat seeds. Drops are random: plant removal with gained=0 is not seed acquisition. Never use on crops or replay an unknown dig.",
        {**pos, "expected_name": {"type": "string", "enum": ["short_grass", "tall_grass"]}})
    add("clear_foliage", "Clear ONE freshly observed visible leaf or vine blocking nearby work. Only one of the 8 horizontally adjacent cells at feet/head height, within 3 blocks, in safe daylight. Never the player's own column or floor. No navigation or drop collection; reobserve after removal, unknown dig results must never be replayed.",
        {**pos, "expected_name": {"type": "string", "enum": sorted(Daily.FOLIAGE)}})
    add("plant_crop", "Plant an existing seed/produce on the observed farmland pos (the soil, not the crop cell). Verify the new crop and item consumption.",
        {**pos, "item": {"type": "string", "enum": sorted(Daily.SEEDS)}})
    add("till_soil", "Use an existing hoe on checked dirt/grass near water; verify farmland. Does not grant tools or seeds.", pos)
    add("hunt_animal", "Attempt a short bounded hunt of an observed cow/pig/sheep/chicken. No players, pets or villagers. Health and threat gates apply; only actual drops gained count as harvest.",
        {"entity_id": entity_id, "expected_name": {"type": "string", "enum": sorted(Daily.ANIMALS)}})
    add("collect_drops", "Approach ONE currently observed item entity and verify expected item gain. Used for natural drops and safe death recovery; despawned drops are not success.",
        {"entity_id": entity_id, "expected_item": _ITEM})
    add("equip_armor", "Equip existing armor in its correct slot and verify the equipped armor inventory.",
        {"item": _ITEM, "slot": {"type": "string", "enum": sorted(Daily.ARMOR)}})
    add("inspect_container", "Read a nearby chest/barrel/furnace window. It may approach and open/close a GUI. Use current slots before transferring or refilling; never treat old slots as current.", pos)
    add("smelt_start", "Load a verified furnace with existing inputs and fuel. Success means loading only, NOT finished products. Inspect again later; never replay a partial or unknown transfer.",
        {**pos, "input_item": {"type": "string", "enum": sorted(Daily.SMELTABLE)}, "count": small_count,
         "fuel_item": {"type": "string", "enum": sorted(Daily.FUELS)}, "fuel_count": small_count})
    add("smelt_collect", "Collect a current furnace output; verify output transfer and inventory gain. Do not collect a product that has not finished smelting.",
        {**pos, "item": _ITEM, "count": small_count})
    add("store_items", "Transfer existing items into an observed chest/barrel, checking both inventories. Preserve food and essential tools for current work.",
        {**pos, "item": _ITEM, "count": count})
    add("take_items", "Withdraw currently available chest/barrel items; verify container decrease and inventory increase.",
        {**pos, "item": _ITEM, "count": count})
    add("place_bed", "Place an inventory bed only with checked two-block space. Actual foot/head/facing and item consumption determine success; desired facing is not supported.",
        {**pos, "item": {"type": "string", "enum": sorted(Daily.BEDS)}})
    add("place_door", "Place an inventory wooden door in checked lower/upper cells; verify both halves and closed state.",
        {**pos, "item": {"type": "string", "enum": sorted(Daily.DOORS)}})
    add("set_door", "Set a nearby wooden door explicitly open or closed; fresh matching halves and final state are verified. Already correct means no toggle. No implicit approach; opening near hostiles is refused.",
        {**pos, "open": {"type": "boolean"}})
    add("position_for_task", "Move to a precise feet-cell centre within 3 blocks on the same level, after a fresh full-body supported corridor check. Intended for bed placement or nearby work stances; refuses doors, leaves, unknown geometry and hazards. Reobserve after moving.",
        {"pos": _position()})
    add("gather_materials", "Gather a small local batch, checking fresh resources, life and threats before every child action. Stop at the first failure or unknown result. count means desired net inventory gain; partial is not completion.",
        {"resource": {"type": "string", "enum": ["logs", "stone", "cobblestone", "coal"]},
         "count": {"type": "integer", "minimum": 1, "maximum": 8},
         "radius": {"type": "integer", "minimum": 1, "maximum": 8, "default": 8}}, ("resource", "count"))
    materials = {"type": "string", "enum": ["cobblestone", "oak_planks", "birch_planks", "spruce_planks"]}
    add("plan_shelter", "Read and validate an empty 5x5 natural ground site centered at integer feet pos. No blocks removed or placed; proposes a fixed small shelter layout.",
        {**pos, "material": materials})
    add("inspect_shelter", "Recheck a 5x5 shelter centered at feet pos: floor, walls, roof, door and facilities. Geometric enclosure is not proof of light level or absence of every threat.", pos)
    add("build_shelter_step", "Place at most ONE missing wall/roof block of a freshly checked shelter layout. Returns progress or required safe positioning; does not claim the entire shelter complete.",
        {**pos, "material": materials})
    add("build_shelter_batch", "Build at most 8 shelter blocks, rechecking each child action. Stops for safe repositioning, shortage, danger or unknown writes; never automatically walks or removes obstacles. Inspect current house completion separately.",
        {**pos, "material": materials,
         "max_blocks": {"type": "integer", "minimum": 1, "maximum": 8, "default": 8}}, ("pos", "material"))
    return tools


# Standalone calculator metadata; no survival harness dispatcher is bound yet.
# Keep it separate so a model cannot select an unavailable game-loop action.
STRONGHOLD_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'calculate_stronghold',
              'description': 'Rank Java 1.19+ stronghold chunks from saved independent eye-of-ender '
                             'coordinate rays. Pure calculation: does not throw eyes or move. Probabilities '
                             'depend on measurement uncertainty; candidates are not confirmed locations.',
              'parameters': {'type': 'object',
                             'properties': {'throws': {'type': 'array',
                                                       'minItems': 1,
                                                       'maxItems': 32,
                                                       'items': {'type': 'object',
                                                                 'properties': {'x': {'type': 'number'},
                                                                                'z': {'type': 'number'},
                                                                                'bearing_deg': {'type': 'number',
                                                                                                'description': 'Vanilla '
                                                                                                               'yaw '
                                                                                                               'degrees: '
                                                                                                               'south '
                                                                                                               '0, '
                                                                                                               'west '
                                                                                                               '90, '
                                                                                                               'north '
                                                                                                               '180, '
                                                                                                               'east '
                                                                                                               '-90.'},
                                                                                'sigma_deg': {'type': 'number',
                                                                                              'minimum': 0.001,
                                                                                              'maximum': 5}},
                                                                 'required': ['x',
                                                                              'z',
                                                                              'bearing_deg',
                                                                              'sigma_deg'],
                                                                 'additionalProperties': False}},
                                            'top_k': {'type': 'integer',
                                                      'minimum': 1,
                                                      'maximum': 100,
                                                      'default': 5}},
                             'required': ['throws']}}}
