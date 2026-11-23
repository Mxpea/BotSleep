"""Native function tools for implemented, locally verified survival actions."""

import copy

from core.domain import SCOUT_DEFAULT_DISTANCE, SCOUT_MAX_DISTANCE, SCOUT_MIN_DISTANCE
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
SURVIVAL_TASK_NAMES = ("gather", "craft", "smelt", "food", "starter_tools", "iron_tools", "iron_armor", "scout",
                       "nether_portal", "shelter", "bed", "storage", "farm", "recover_drops", "maintenance")
SURVIVAL_TASK_RESOURCES = ("logs", "cobblestone", "coal", "raw_iron", "diamond")

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

PROCEDURAL_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "pillar_up",
            "description": "Jump and place solid blocks underneath to tower up vertically. Useful to evade ground monsters (zombies, creepers, spiders).",
            "parameters": {
                "type": "object",
                "properties": {
                    "height": {
                        "type": "integer",
                        "description": "Number of blocks to pillar up (default: 3, max: 10).",
                        "default": 3,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Explanation of why the bot is pillaring up.",
                    },
                },
                "required": ["height"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chop_tree",
            "description": "Locate nearest trees and cleanly chop down all wood logs until target count or time limit is reached.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_count": {
                        "type": "integer",
                        "description": "Target number of wood logs to harvest (default: 5).",
                        "default": 5,
                    },
                    "max_duration_seconds": {
                        "type": "number",
                        "description": "Maximum work time in seconds before stopping (default: 60.0).",
                        "default": 60.0,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "radar_sweep",
            "description": "Trigger a 3D block radar scan around the bot to discover POIs (beds, chests, crafting tables, minerals, hazards).",
            "parameters": {
                "type": "object",
                "properties": {
                    "radius": {
                        "type": "integer",
                        "description": "Radar radius in blocks (1-32, default: 24).",
                        "default": 24,
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional specific categories to scan: 'bed', 'crafting', 'container', 'mineral', 'liquid_and_hazard'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cast_nether_portal",
            "description": "Speedrun bucket-cast a Nether Portal at a lava lake with 10 obsidian blocks, light it with flint and steel, and verify activation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "axis": {
                        "type": "string",
                        "enum": ["x", "z"],
                        "description": "Portal frame alignment axis ('x' for east-west, 'z' for north-south).",
                        "default": "x",
                    },
                },
            },
        },
    },
]

TOOL_SCHEMAS = {entry["function"]["name"]: entry["function"]["parameters"] for entry in TOOL_DEFINITIONS}


def tools_for_objective(objective="starter"):
    """Expose only the selected runtime's executable skills, never future plans."""
    tools = copy.deepcopy(TOOL_DEFINITIONS)
    if objective == "competition":
        tools = tools_for_objective("daily") + competition_tools()
        # Preserve full implemented capability while trimming prose-only schema
        # text so the entire rule catalogue fits the bounded small-model input.
        for tool in tools:
            function = tool["function"]
            function["description"] = function.get("description", "")[:140]
            parameters = function.get("parameters", {})
            # This identical prose is stated once in the competition prompt.
            # Preserve the reason field/type/limit and every executable constraint.
            parameters.get("properties", {}).get("reason", {}).pop("description", None)
            for schema in parameters.get("properties", {}).values():
                # Repeated coordinate prose is stated once in the competition
                # prompt; item types, coordinate bounds and arity remain intact.
                if isinstance(schema, dict) and schema.get("description") == "World coordinates [x, y, z].":
                    schema.pop("description")
                if isinstance(schema, dict) and isinstance(schema.get("description"), str):
                    schema["description"] = schema["description"][:90]
                items = schema.get("items") if isinstance(schema, dict) else None
                if isinstance(items, dict) and isinstance(items.get("description"), str):
                    items["description"] = items["description"][:90]
        return tools
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
    add("run_survival_task", "Choose one bounded goal. The executor finds prerequisites and verifies each step locally. Shelter supplies its own wood, workstation, walls, roof and door. Scout takes distance in blocks. Gather takes resource and count. The harness handles movement, mining, pickup, and inventory checks. Craft/smelt take the output item. The final summary reports completion, interrupted work or an exhausted budget.",
        {"task": {"type": "string", "enum": list(SURVIVAL_TASK_NAMES)},
         "resource": {"type": "string", "enum": list(SURVIVAL_TASK_RESOURCES)},
         "item": {**_ITEM, "description": "Desired craft/smelt output; bone_meal uses held bones only."},
         "count": {"type": "integer", "minimum": 1, "maximum": 64, "default": 8,
                   "description": "Desired new output for gather/craft/smelt; current food reserve target for food."},
         "distance": {"type": "integer", "minimum": SCOUT_MIN_DISTANCE, "maximum": SCOUT_MAX_DISTANCE,
                      "default": SCOUT_DEFAULT_DISTANCE, "description": "Requested horizontal waypoint distance in blocks. Scout can stop early."},
         "radius": {"type": "integer", "minimum": 4, "maximum": 64, "default": 32}}, ("task",))
    add("fight_hostile", "Perform one bounded defensive step against an observed skeleton, zombie or creeper. The skill selects shield/no-shield handling and rechecks life, target and terrain. A hit, block or missing target is not a verified kill; never chase unknown enemies.",
        {"entity_id": entity_id, "expected_name": {"type": "string", "enum": ["skeleton", "zombie", "creeper"]}})
    add("collect_liquid", "Collect one freshly verified water/lava source into an existing empty bucket from safe reachable ground. Source removal and filled-bucket inventory gain must both be verified; never enter the liquid or replay an unknown result.",
        {**pos, "expected_name": {"type": "string", "enum": ["water", "lava"]}})
    add("hunt_with_lava", "Use an existing lava bucket only on one observed passive animal in a verified single-cell nonflammable containment pit. No players, enemies or flammables may be nearby. Recollect and verify the source immediately; success requires actual inventory gain. Never use on open terrain.",
        {"entity_id": entity_id, "expected_name": {"type": "string", "enum": ["pig", "cow", "sheep", "chicken"]}})
    add("gather_resource", "Mine one observed iron ore with a stone-or-better pickaxe or diamond ore with an iron-or-better pickaxe. Verify block removal and the matching inventory gain.",
        {**pos, "expected_name": {"type": "string", "enum": sorted(Daily.IRON | Daily.DIAMOND)}})
    add("harvest_crop", "Harvest ONE observed mature crop; maturity, removal and produce gain are checked. Replant when seeds are available.",
        {**pos, "expected_name": {"type": "string", "enum": sorted(Daily.CROPS)}})
    add("gather_seeds", "Break ONE currently observed short_grass or tall_grass plant within checked reach to try for wheat seeds. Drops are random: plant removal with gained=0 is not seed acquisition. Never use on crops or replay an unknown dig.",
        {**pos, "expected_name": {"type": "string", "enum": ["short_grass", "tall_grass"]}})
    add("clear_foliage", "Clear ONE freshly observed visible leaf or vine blocking nearby work. Only one of the 8 horizontally adjacent cells at feet/head height, within 3 blocks, in safe daylight. Never the player's own column or floor. No navigation or drop collection; reobserve after removal, unknown dig results must never be replayed.",
        {**pos, "expected_name": {"type": "string", "enum": sorted(Daily.FOLIAGE)}})
    add("plant_crop", "Plant an existing seed/produce on the observed farmland pos (the soil, not the crop cell). Verify the new crop and item consumption.",
        {**pos, "item": {"type": "string", "enum": sorted(Daily.SEEDS)}})
    add("till_soil", "Use an existing hoe on checked dirt/grass near water; verify farmland. Does not grant tools or seeds.", pos)
    add("hunt_animal", "Strike one observed cow/pig/sheep/chicken only within 2.8 blocks from a verified stationary stance; position safely first. Never chase. Target disappearance is not a kill; only raw/cooked inventory gain counts. On hunt_unverified, reobserve current drops before collect_drops.",
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
    from core.structure_spec import SHELTER_BUILDING_BLOCKS
    materials = {"type": "string", "enum": sorted(SHELTER_BUILDING_BLOCKS)}
    orient = {"type": "string", "enum": ["north", "south", "east", "west"], "default": "north"}
    add("plan_shelter", "Read-only 5x5 ground site validation at feet pos.",
        {**pos, "material": materials, "orientation": orient}, ("pos", "material"))
    add("inspect_shelter", "Recheck 5x5 shelter floor, walls, roof, door at feet pos; geometry != light/safety.",
        {**pos, "orientation": orient}, ("pos",))
    add("build_shelter_step", "Place ONE missing shelter block from fresh layout; reports safe move need.",
        {**pos, "material": materials, "orientation": orient}, ("pos", "material"))
    add("build_shelter_batch", "Place up to 8 shelter blocks; stops on move, hazard, or unknown write.",
        {**pos, "material": materials, "orientation": orient,
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


def competition_tools():
    """Task lifecycle tools; executed locally by the competition runtime."""
    reason = {"type": "string", "minLength": 1, "maxLength": 300}
    return [
        _tool("select_task", "Select ONE scored goal ID from the competition board. This does not grant items or complete it. Pending/excluded scoring goals cannot be selected.",
              {"goal_id": {"type": "string", "minLength": 1, "maxLength": 160}}, ("goal_id",)),
        _tool("update_progress", "Save a short progress card: verified progress and next intent. Do not invent completion or paste logs.",
              {"summary": {"type": "string", "minLength": 1, "maxLength": 500}}, ("summary",)),
        _tool("finish_task", "Return to goal selection ONLY after current server advancement checks are available. Unfinished/unknown goals never earn points; only server-completed scored goals count.",
              {"reason": reason}, ("reason",)),
        _tool("force_exit_task", "Abandon the current goal and return to selection. The goal stays unfinished and enters cooldown. No effect on provider uncertainty or game-action pending state.",
              {"reason": reason}, ("reason",)),
        _tool("end_competition", "End the competition run ONLY after every preset advancement has a fresh server assessment. This does not claim every goal was completed.",
              {"reason": reason}, ("reason",)),
    ]
