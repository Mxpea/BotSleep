"""L3b Competition Advancement Dependency Graph (declarative DAG for non-serial PPM planning)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Optional, Tuple


@dataclass(frozen=True)
class AdvancementNodeSpec:
    """Declarative advancement execution & scheduling metadata."""

    adv_id: str
    handler: str  # acquire | enter_nether | avoid_vibration | interact_block | interact_entity | use_item | combat_ranged | window_action | structure_build
    target: str
    required_items: Tuple[Tuple[str, int], ...] = ()
    capabilities_req: FrozenSet[str] = frozenset()
    capabilities_grant: FrozenSet[str] = frozenset()
    world_triggers: FrozenSet[str] = frozenset()
    base_minutes: float = 2.0
    timeout_seconds: float = 210.0
    tier: int = 1


_F = frozenset


def _spec(
    adv_id: str,
    handler: str,
    target: str,
    *,
    req_items: Tuple[Tuple[str, int], ...] = (),
    req_caps: FrozenSet[str] = frozenset(),
    grant_caps: FrozenSet[str] = frozenset(),
    triggers: FrozenSet[str] = frozenset(),
    base_min: float = 2.0,
    timeout_s: float = 210.0,
    tier: int = 1,
) -> Tuple[str, AdvancementNodeSpec]:
    return adv_id, AdvancementNodeSpec(
        adv_id=adv_id,
        handler=handler,
        target=target,
        required_items=req_items,
        capabilities_req=req_caps,
        capabilities_grant=grant_caps,
        world_triggers=triggers,
        base_minutes=float(base_min),
        timeout_seconds=float(timeout_s),
        tier=int(tier),
    )


ADVANCEMENT_GRAPH: Dict[str, AdvancementNodeSpec] = dict([
    # ---- T0 / T1 Core Acquire & Dimension Milestones (low gear, high fan-out!) ----
    _spec("minecraft:story/mine_stone", "acquire", "cobblestone", req_items=(("cobblestone", 1),), grant_caps=_F({"stone_avail"}), base_min=0.7, timeout_s=120.0, tier=0),
    _spec("minecraft:story/upgrade_tools", "acquire", "stone_pickaxe", req_items=(("stone_pickaxe", 1),), grant_caps=_F({"stone_pick"}), base_min=1.0, timeout_s=150.0, tier=0),
    _spec("minecraft:story/smelt_iron", "acquire", "iron_ingot", req_items=(("iron_ingot", 1),), req_caps=_F({"stone_pick"}), grant_caps=_F({"iron_ingot"}), base_min=2.4, timeout_s=320.0, tier=1),
    _spec("minecraft:story/iron_tools", "acquire", "iron_pickaxe", req_items=(("iron_pickaxe", 1),), req_caps=_F({"stone_pick"}), grant_caps=_F({"iron_pick"}), base_min=2.6, timeout_s=380.0, tier=1),
    _spec("minecraft:story/obtain_armor", "acquire", "iron_boots", req_items=(("iron_boots", 1),), req_caps=_F({"stone_pick"}), base_min=2.4, timeout_s=340.0, tier=1),
    _spec("minecraft:story/lava_bucket", "acquire", "lava_bucket", req_items=(("lava_bucket", 1),), req_caps=_F({"stone_pick"}), grant_caps=_F({"lava_bucket"}), triggers=_F({"lava"}), base_min=2.8, timeout_s=300.0, tier=1),
    _spec("minecraft:story/form_obsidian", "acquire", "obsidian", req_items=(("obsidian", 1),), req_caps=_F({"iron_pick"}), grant_caps=_F({"obsidian"}), triggers=_F({"obsidian", "lava"}), base_min=4.5, timeout_s=360.0, tier=2),
    _spec("minecraft:story/enter_the_nether", "enter_nether", "the_nether", req_caps=_F({"iron_pick"}), grant_caps=_F({"nether"}), base_min=5.5, timeout_s=420.0, tier=2),
    _spec("minecraft:story/mine_diamond", "acquire", "diamond", req_items=(("diamond", 1),), req_caps=_F({"iron_pick"}), grant_caps=_F({"diamond"}), base_min=6.5, timeout_s=420.0, tier=2),
    _spec("minecraft:story/shiny_gear", "acquire", "diamond_boots", req_items=(("diamond_boots", 1),), req_caps=_F({"iron_pick"}), base_min=8.0, timeout_s=480.0, tier=2),
    _spec("minecraft:story/follow_ender_eye", "acquire", "ender_eye", req_items=(("ender_eye", 1),), req_caps=_F({"nether"}), base_min=10.0, timeout_s=300.0, tier=2),
    _spec("minecraft:story/enter_the_end", "enter_nether", "the_nether", req_caps=_F({"iron_pick"}), base_min=15.0, timeout_s=360.0, tier=2),
    _spec("minecraft:adventure/avoid_vibration", "avoid_vibration", "sculk_sensor", triggers=_F({"sculk_sensor", "calibrated_sculk_sensor"}), base_min=1.0, timeout_s=90.0, tier=1),
    _spec("minecraft:adventure/lightning_rod_with_villager_no_fire", "acquire", "lightning_rod", req_items=(("lightning_rod", 1),), req_caps=_F({"stone_pick"}), base_min=4.0, timeout_s=180.0, tier=1),
    _spec("minecraft:husbandry/balanced_diet", "acquire", "bread", req_items=(("bread", 1),), triggers=_F({"wheat", "hay_block"}), base_min=4.5, timeout_s=180.0, tier=1),
    _spec("minecraft:husbandry/obtain_netherite_hoe", "acquire", "diamond_hoe", req_items=(("diamond_hoe", 1),), req_caps=_F({"iron_pick"}), base_min=12.0, timeout_s=300.0, tier=2),
    _spec("minecraft:nether/obtain_crying_obsidian", "acquire", "obsidian", req_items=(("obsidian", 1),), req_caps=_F({"nether"}), base_min=9.0, timeout_s=240.0, tier=2),
    _spec("minecraft:nether/brew_potion", "acquire", "glass_bottle", req_items=(("glass_bottle", 1),), req_caps=_F({"nether"}), base_min=8.0, timeout_s=240.0, tier=2),
    _spec("minecraft:nether/ride_strider", "acquire", "fishing_rod", req_items=(("fishing_rod", 1),), req_caps=_F({"nether"}), base_min=10.0, timeout_s=240.0, tier=2),
    _spec("minecraft:nether/get_wither_skull", "acquire", "iron_sword", req_items=(("iron_sword", 1),), req_caps=_F({"nether"}), base_min=12.0, timeout_s=240.0, tier=2),
    _spec("minecraft:nether/obtain_blaze_rod", "acquire", "iron_sword", req_items=(("iron_sword", 1),), req_caps=_F({"nether"}), base_min=10.0, timeout_s=240.0, tier=2),
    _spec("minecraft:adventure/honey_block_slide", "acquire", "glass_bottle", req_items=(("glass_bottle", 1),), triggers=_F({"bee_nest", "beehive"}), base_min=6.0, timeout_s=210.0, tier=2),

    # ---- Plugin 1: InteractBlockAdvTask ----
    _spec("minecraft:adventure/sleep_in_bed", "interact_block", "sleep_in_bed", req_items=(("white_bed", 1),), grant_caps=_F({"bed"}), triggers=_F({"sheep", "white_bed"}), base_min=2.2, timeout_s=210.0, tier=0),
    _spec("minecraft:husbandry/plant_seed", "interact_block", "plant_seed", req_items=(("wooden_hoe", 1), ("wheat_seeds", 1)), triggers=_F({"short_grass", "tall_grass", "wheat", "farmland"}), base_min=1.2, timeout_s=130.0, tier=0),
    _spec("minecraft:husbandry/make_a_sign_glow", "interact_block", "make_a_sign_glow", req_items=(("oak_sign", 1), ("glow_ink_sac", 1)), triggers=_F({"glow_squid"}), base_min=3.0, timeout_s=210.0, tier=1),
    _spec("minecraft:nether/use_lodestone", "interact_block", "use_lodestone", req_items=(("lodestone", 1), ("compass", 1)), req_caps=_F({"iron_pick"}), base_min=4.2, timeout_s=240.0, tier=1),
    _spec("minecraft:husbandry/wax_on", "interact_block", "wax_on", req_items=(("copper_block", 1), ("honeycomb", 1)), req_caps=_F({"stone_pick"}), triggers=_F({"bee_nest", "beehive"}), base_min=4.0, timeout_s=210.0, tier=1),
    _spec("minecraft:husbandry/wax_off", "interact_block", "wax_off", req_items=(("stone_axe", 1),), triggers=_F({"waxed_copper_block"}), base_min=2.0, timeout_s=150.0, tier=1),
    _spec("minecraft:husbandry/safely_harvest_honey", "interact_block", "safely_harvest_honey", req_items=(("campfire", 1), ("glass_bottle", 1)), triggers=_F({"bee_nest", "beehive"}), base_min=2.8, timeout_s=180.0, tier=1),
    _spec("minecraft:adventure/read_power_of_chiseled_bookshelf", "interact_block", "read_power_of_chiseled_bookshelf", req_items=(("chiseled_bookshelf", 1), ("comparator", 1)), req_caps=_F({"nether"}), base_min=4.5, timeout_s=240.0, tier=2),
    _spec("minecraft:nether/charge_respawn_anchor", "interact_block", "charge_respawn_anchor", req_items=(("respawn_anchor", 1), ("glowstone", 1)), req_caps=_F({"nether"}), base_min=6.5, timeout_s=240.0, tier=2),

    # ---- Plugin 2: InteractEntityAdvTask ----
    _spec("minecraft:husbandry/breed_an_animal", "interact_entity", "breed_an_animal", req_items=(("wheat_seeds", 2),), triggers=_F({"chicken", "cow", "sheep", "pig"}), base_min=1.6, timeout_s=150.0, tier=0),
    _spec("minecraft:husbandry/tame_an_animal", "interact_entity", "tame_an_animal", req_items=(("bone", 3),), triggers=_F({"wolf", "cat", "horse", "donkey"}), base_min=2.4, timeout_s=180.0, tier=1),
    _spec("minecraft:husbandry/tactical_fishing", "interact_entity", "tactical_fishing", req_items=(("water_bucket", 1),), req_caps=_F({"stone_pick"}), triggers=_F({"cod", "salmon", "tropical_fish", "pufferfish"}), base_min=2.0, timeout_s=180.0, tier=1),
    _spec("minecraft:husbandry/axolotl_in_a_bucket", "interact_entity", "axolotl_in_a_bucket", req_items=(("water_bucket", 1),), req_caps=_F({"stone_pick"}), triggers=_F({"axolotl"}), base_min=2.5, timeout_s=180.0, tier=1),
    _spec("minecraft:nether/distract_piglin", "interact_entity", "distract_piglin", req_items=(("gold_ingot", 1),), req_caps=_F({"nether"}), triggers=_F({"piglin"}), base_min=1.8, timeout_s=150.0, tier=2),
    _spec("minecraft:adventure/brush_armadillo", "interact_entity", "brush_armadillo", req_items=(("brush", 1),), triggers=_F({"armadillo"}), base_min=2.5, timeout_s=150.0, tier=1),
    _spec("minecraft:husbandry/ride_a_boat_with_a_goat", "interact_entity", "ride_a_boat_with_a_goat", req_items=(("oak_boat", 1),), triggers=_F({"goat"}), base_min=2.5, timeout_s=150.0, tier=1),

    # ---- Plugin 3: UseItemAndFishAdvTask ----
    _spec("minecraft:husbandry/fishy_business", "use_item", "fishy_business", req_items=(("fishing_rod", 1),), triggers=_F({"water"}), base_min=2.2, timeout_s=180.0, tier=1),
    _spec("minecraft:adventure/spyglass_at_parrot", "use_item", "spyglass_at_parrot", req_items=(("spyglass", 1),), triggers=_F({"parrot"}), base_min=2.5, timeout_s=150.0, tier=1),
    _spec("minecraft:adventure/spyglass_at_ghast", "use_item", "spyglass_at_ghast", req_items=(("spyglass", 1),), req_caps=_F({"nether"}), triggers=_F({"ghast"}), base_min=2.5, timeout_s=150.0, tier=2),

    # ---- Plugin 4: CombatAndRangedAdvTask ----
    _spec("minecraft:adventure/kill_a_mob", "combat_ranged", "kill_a_mob", req_items=(("stone_sword", 1),), triggers=_F({"zombie", "skeleton", "spider", "creeper", "drowned", "husk", "stray"}), base_min=1.5, timeout_s=150.0, tier=0),
    _spec("minecraft:story/deflect_arrow", "combat_ranged", "deflect_arrow", req_items=(("shield", 1),), req_caps=_F({"stone_pick"}), grant_caps=_F({"shield"}), triggers=_F({"skeleton", "stray", "pillager"}), base_min=2.2, timeout_s=180.0, tier=1),
    _spec("minecraft:adventure/shoot_arrow", "combat_ranged", "shoot_arrow", req_items=(("bow", 1), ("arrow", 2)), triggers=_F({"zombie", "skeleton", "spider", "cow", "sheep", "pig", "chicken"}), base_min=2.5, timeout_s=180.0, tier=1),
    _spec("minecraft:adventure/ol_betsy", "combat_ranged", "ol_betsy", req_items=(("crossbow", 1), ("arrow", 1)), req_caps=_F({"stone_pick"}), triggers=_F({"zombie", "skeleton", "spider", "cow", "sheep", "pig"}), base_min=3.2, timeout_s=210.0, tier=1),
    _spec("minecraft:adventure/bullseye", "combat_ranged", "bullseye", req_items=(("target", 1), ("bow", 1), ("arrow", 4)), req_caps=_F({"iron_pick"}), base_min=4.0, timeout_s=240.0, tier=2),
    _spec("minecraft:adventure/sniper_duel", "combat_ranged", "sniper_duel", req_items=(("bow", 1), ("arrow", 6)), triggers=_F({"skeleton"}), base_min=4.5, timeout_s=240.0, tier=2),

    # ---- Plugin 5: WindowActionAdvTask ----
    _spec("minecraft:adventure/trade", "window_action", "trade", req_items=(), triggers=_F({"villager", "wandering_trader"}), base_min=1.6, timeout_s=150.0, tier=0),
    _spec("minecraft:story/enchant_item", "window_action", "enchant_item", req_items=(("enchanting_table", 1), ("lapis_lazuli", 2)), req_caps=_F({"iron_pick"}), base_min=5.5, timeout_s=270.0, tier=2),

    # ---- Plugin 6: StructureBuildAdvTask ----
    _spec("minecraft:adventure/summon_iron_golem", "structure_build", "summon_iron_golem", req_items=(("iron_block", 4), ("carved_pumpkin", 1)), req_caps=_F({"stone_pick"}), triggers=_F({"pumpkin"}), base_min=6.5, timeout_s=300.0, tier=2),
    _spec("minecraft:adventure/crafters_crafting_crafters", "structure_build", "crafters_crafting_crafters", req_items=(("crafter", 1), ("lever", 1), ("iron_ingot", 5), ("crafting_table", 1), ("redstone", 2), ("dropper", 1)), req_caps=_F({"iron_pick"}), base_min=5.5, timeout_s=300.0, tier=2),
])

VALID_HANDLERS: FrozenSet[str] = frozenset({
    "acquire",
    "enter_nether",
    "avoid_vibration",
    "interact_block",
    "interact_entity",
    "use_item",
    "combat_ranged",
    "window_action",
    "structure_build",
})


def get_advancement_spec(adv_id: str) -> Optional[AdvancementNodeSpec]:
    return ADVANCEMENT_GRAPH.get(adv_id)


def unlocked_capability_fanout(
    adv_id: str,
    completed_ids: FrozenSet[str] | set[str],
    catalog: Mapping[str, object],
) -> float:
    """Return total points of still-uncompleted eligible nodes that require a capability granted by adv_id."""
    spec = ADVANCEMENT_GRAPH.get(adv_id)
    if spec is None or not spec.capabilities_grant:
        return 0.0
    granted = spec.capabilities_grant
    bonus_pts = 0.0
    for dep_id, dep_spec in ADVANCEMENT_GRAPH.items():
        if dep_id == adv_id or dep_id in completed_ids or dep_id not in catalog:
            continue
        if dep_spec.capabilities_req & granted:
            entry = catalog[dep_id]
            bonus_pts += float(getattr(entry, "points", 0) or 0)
    return bonus_pts


def validate_graph() -> None:
    """Validate structural invariants of ADVANCEMENT_GRAPH."""
    for aid, spec in ADVANCEMENT_GRAPH.items():
        assert spec.adv_id == aid, f"mismatched adv_id {aid}"
        assert spec.handler in VALID_HANDLERS, f"invalid handler {spec.handler} on {aid}"
        assert spec.base_minutes > 0.0 and spec.timeout_seconds >= 30.0, f"bad timing on {aid}"
