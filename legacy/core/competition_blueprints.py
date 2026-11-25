"""Planning coverage for scored advancements without an executable route.

These are maintenance notes, not model tools or action templates. The first
column is a capability gap; the next two describe prerequisites and the trigger.
Eligibility, points and completion still come from the rules and server.
"""

_ROWS = """
story/cure_zombie_villager | zombie_villager_cure | Secure an isolated zombie villager, Weakness and a golden apple | Apply Weakness, use the apple and protect the villager until cured
story/enter_the_end | end_portal_entry | Find the stronghold portal room and prepare enough eyes for empty frames | Fill only empty frames, enter and confirm the dimension transition
story/follow_ender_eye | stronghold_route | Obtain eyes and verify a reachable route on the assigned island | Sample eye bearings with the existing calculator and reach the stronghold structure
story/enchant_item | enchanting_window | Prepare an enchanting table, lapis, XP and an enchantable item | Apply an available enchantment in the table and verify the result
story/enter_the_nether | portal_transit | Build or verify a stable lit portal and prepare a return route | Enter and verify arrival in the Nether; lighting alone is insufficient
story/deflect_arrow | projectile_block_evidence | Equip a shield and isolate one observable arrow source with cover and a retreat | Block an incoming arrow and check the server result
story/form_obsidian | contained_obsidian_formation | Find a contained lava source and prepare a water bucket and suitable pickaxe | Form or locate obsidian and obtain a block in inventory; verify the server criterion
nether/all_effects | simultaneous_effects | Verify the version-specific effect set, sources and timers | Apply all required effects concurrently within the shortest duration
nether/all_potions | simultaneous_effects | Brew the version-specific potion set and arrange doses by expiry | Maintain all required potion effects at the same time
nether/create_full_beacon | beacon_pyramid | Obtain a beacon and enough valid blocks for a four-layer base | Build the complete base and verify the powered beacon
nether/netherite_armor | smithing_upgrade | Find an upgrade template, obtain netherite and make diamond armor | Upgrade and wear the complete netherite set
nether/summon_wither | isolated_wither_encounter | Collect three wither skeleton skulls and four soul sand or soul soil | Assemble and summon at a site that does not endanger others
nether/uneasy_alliance | ghast_transport | Verify a ghast, a compatible portal route and a safe isolated destination | Bring a Nether ghast into the Overworld and defeat it there
nether/create_beacon | beacon_activation | Obtain a Nether star, craft a beacon and prepare a valid base | Activate the beacon and verify its level and server evidence
nether/get_wither_skull | fortress_mob_loot | Locate a fortress and a safe wither skeleton encounter | Obtain the skull into inventory rather than counting kills
nether/obtain_ancient_debris | nether_mining | Verify a Nether route and diamond-tier or better mining equipment | Mine or obtain ancient debris with inventory evidence and a return route
nether/ride_strider_in_overworld_lava | strider_transport | Prepare a saddled strider, steering item and the rule-qualified Overworld lava route | Ride the strider across the required lava distance in the Overworld
nether/charge_respawn_anchor | anchor_charge | Prepare a respawn anchor and four glowstone in the Nether | Charge to the maximum level; do not activate it outside the Nether
nether/explore_nether | biome_tracking | Observe own-island paths to all five Nether biomes | Visit each and assess criterion progress from the server
nether/find_bastion | nether_structure_navigation | Verify a reachable own-island bastion route | Enter the structure boundary and check the server advancement
nether/find_fortress | nether_structure_navigation | Verify a reachable own-island fortress route | Enter the structure boundary and check the server advancement
nether/loot_bastion | nether_structure_navigation | Locate a bastion chest accessible without interference or unacceptable piglin risk | Open the verified bastion chest and check the server result
nether/obtain_blaze_rod | fortress_mob_loot | Locate a fortress blaze and prepare resistance or cover and a retreat | Acquire a blaze rod and verify inventory gain
nether/ride_strider | strider_control | Verify a reachable strider, saddle and warped fungus on a stick | Mount and control the strider without uncontrolled lava exposure
nether/brew_potion | brewing_window | Obtain a brewing stand, blaze powder, bottles and ingredients | Brew and collect a valid potion from the stand
nether/obtain_crying_obsidian | crying_obsidian_source | Locate a loot source, barter route or mineable crying obsidian | Obtain the item and verify inventory gain
nether/distract_piglin | piglin_barter | Verify an adult piglin and a safe handoff location; prepare a gold ingot | Offer gold and verify the piglin picked it up while distracted
end/elytra | end_ship_navigation | Reach an End ship with a safe return route and an available elytra | Obtain the elytra without interfering with other entrants
end/find_end_city | end_navigation | Reach the outer End and identify a reachable city | Enter the city structure boundary and verify the server result
end/levitate | levitation_recovery | Verify a shulker, a vertical route and a safe landing or recovery plan | Travel the required vertical distance while levitating
end/enter_end_gateway | gateway_transit | Locate an open End gateway and a safe way to activate traversal | Traverse it and confirm arrival; no dragon respawn
adventure/arbalistic | piercing_shot_geometry | Obtain a Piercing crossbow and five distinct eligible mobs with safe alignment | Verify five distinct mobs are killed by one shot
adventure/hero_of_the_village | raid_lifecycle | Verify a qualified village and plan defenses, supplies and retreat | Win the raid and confirm the server effect and advancement
adventure/revaulting | vault_interaction | Obtain an ominous trial key and locate an unopened ominous vault | Use the matching key on the vault and verify the server result
adventure/throw_trident | trident_attack | Obtain a trident and identify an eligible mob and safe line of sight | Throw the trident and verify a hit, not merely a release
adventure/totem_of_undying | controlled_totem_trigger | Obtain and equip a totem and design a controlled survivable recovery site | Verify totem activation and actual survival; do not treat a death as success
adventure/trade_at_world_height | high_altitude_villager_trade | Verify a trader and legal access at the world height limit | Complete an eligible trade in the required height band
adventure/two_birds_one_arrow | piercing_shot_geometry | Obtain a Piercing crossbow and align two eligible phantoms | Confirm both were killed by the same arrow
adventure/craft_decorated_pot_using_only_sherds | exact_recipe_inputs | Obtain four pottery sherds and a crafting table | Craft with four sherds, not bricks, and verify the resulting pot
adventure/heart_transplanter | special_block_placement | Obtain a creaking heart and pale oak logs; verify the placement geometry | Place the heart between matching aligned logs and verify activation
adventure/kill_mob_near_sculk_catalyst | catalyst_kill_geometry | Find a catalyst and one eligible target at a safe verified distance | Kill the mob within catalyst range and verify the server criterion
adventure/play_jukebox_in_meadows | biome_interaction | Obtain a record and jukebox and identify the Meadow biome | Use the record on the jukebox while in the biome
adventure/sniper_duel | long_range_bow | Prepare a bow and verify a skeleton in the required distance band | Confirm the arrow kills the skeleton from at least 50 horizontal blocks
adventure/summon_iron_golem | iron_golem_assembly | Prepare four iron blocks, a carved pumpkin and verified body clearance | Build the golem with the pumpkin last and confirm spawning
adventure/trim_with_any_armor_pattern | smithing_trim | Prepare a trim template, material, armor and smithing table | Apply the trim and verify the returned item
adventure/under_lock_and_key | vault_interaction | Obtain a trial key and locate an unopened standard vault | Unlock the vault with the matching key
adventure/voluntary_exile | raid_captain_encounter | Locate an eligible raid captain and a safe isolated combat site | Defeat the captain and verify the criterion without starting an unintended raid
adventure/who_needs_rockets | wind_charge_vertical_motion | Obtain wind charges and verify a safe vertical launch and landing | Bounce the required height on a wind charge and verify the server result
adventure/avoid_vibration | sculk_stealth | Locate a sculk sensor or Warden detection area with verified retreat | Sneak near the relevant detection source without triggering an unsafe encounter
adventure/brush_armadillo | brush_entity | Obtain a brush and observe a reachable armadillo | Brush the armadillo and verify the interaction
adventure/bullseye | precision_target_shot | Place a target block and prepare a ranged shot from the required distance | Hit the center from at least 30 blocks
adventure/crafters_crafting_crafters | crafter_configuration | Prepare a crafter, recipe ingredients and a verified redstone trigger | Make a crafter craft another crafter
adventure/lighten_up | copper_bulb_scrape | Prepare an axe and an oxidized copper bulb | Scrape the bulb and verify its changed state
adventure/minecraft_trials_edition | trial_structure_navigation | Identify a reachable trial chamber within the assigned island | Enter the trial chamber and check the server criterion
adventure/read_power_of_chiseled_bookshelf | bookshelf_redstone | Prepare a chiseled bookshelf, book and comparator | Insert a book and read the bookshelf with the comparator
adventure/salvage_sherd | archaeology_brush | Locate suspicious sand or gravel and prepare a brush and safe stance | Brush without breaking the block and verify a pottery sherd was obtained
adventure/spyglass_at_ghast | spyglass_acquisition | Obtain a spyglass and verify a ghast in line of sight from cover | Look at the ghast through the spyglass within the safe window
adventure/spyglass_at_parrot | spyglass_acquisition | Obtain a spyglass and identify an observed parrot | Look at the parrot through the spyglass and confirm the criterion
adventure/trade | villager_trade | Verify an available villager offer and its actual inputs | Execute one trade and verify the resulting inventory change
adventure/use_lodestone | lodestone_interaction | Obtain a compass and find or craft a lodestone | Bind the compass to the lodestone and verify the result
adventure/walk_on_powder_snow_with_leather_boots | powder_snow_traversal | Equip leather boots and verify accessible powder snow with a safe exit | Step on the powder snow and check the server criterion
adventure/whos_the_pillager_now | crossbow_kill | Obtain a crossbow and isolate a verified pillager | Kill the pillager with the crossbow and check the server criterion
husbandry/feed_snifflet | sniffer_lifecycle | Hatch a sniffer and obtain torchflower seeds before the baby matures | Feed the snifflet and verify the interaction
husbandry/froglights | frog_and_magma_cube | Prepare all frog variants and controlled access to small magma cubes | Obtain all three froglight variants and verify inventory criteria
husbandry/leash_all_frog_variants | multi_leash | Obtain and identify all three frog variants and enough leads | Keep all three variants leashed at the required time
husbandry/obtain_netherite_hoe | smithing_upgrade | Obtain an upgrade template, a netherite ingot and a diamond hoe | Smith the netherite hoe and confirm inventory gain
husbandry/allay_deliver_cake_to_note_block | allay_delivery | Locate an allay and prepare cake, a note block and a safe delivery location | Use the note block and verify the allay drops cake at it
husbandry/allay_deliver_item_to_player | allay_delivery | Locate an allay and prepare one matching item for collection | Verify it delivers the item to the agent
husbandry/axolotl_in_a_bucket | entity_bucketing | Verify an axolotl, water bucket and stable reach | Capture the axolotl and verify the filled bucket
husbandry/kill_axolotl_target | axolotl_assisted_combat | Verify an axolotl and a valid shared underwater target with safe oxygen budget | Defeat its target and verify the granted effect or advancement
husbandry/make_a_sign_glow | sign_ink_interaction | Obtain a glow ink sac and place or find a sign with text | Apply the glow ink sac and verify the text glows
husbandry/obtain_sniffer_egg | archaeology_brush | Identify suspicious sand in a warm ocean ruin and prepare a brush | Brush the eligible block and confirm the sniffer egg entered inventory
husbandry/plant_any_sniffer_seed | sniffer_lifecycle | Hatch a sniffer, obtain a torchflower seed or pitcher pod and prepare soil | Plant the qualifying seed and verify the resulting crop
husbandry/remove_wolf_armor | wolf_armor_interaction | Tame a wolf and prepare wolf armor and shears | Remove the armor from the owned wolf with shears
husbandry/repair_wolf_armor | wolf_armor_interaction | Verify an owned wolf with damaged armor and obtain armadillo scutes | Repair the equipped armor and recheck its state
husbandry/ride_a_boat_with_a_goat | boat_passenger_control | Verify a goat, boat and a safe route to seat both occupants | Board the same boat and verify the criterion
husbandry/silk_touch_nest | enchanted_hive_harvest | Prepare Silk Touch equipment and observe a nest with three bees inside | Harvest it intact and verify the server criterion
husbandry/tadpole_in_a_bucket | entity_bucketing | Find a tadpole and prepare a water bucket and stable reach | Capture it and verify the filled bucket
husbandry/fishy_business | fishing_goal | Prepare a rod and a verified water casting spot | Catch a qualifying fish and verify the catch
husbandry/place_dried_ghast_in_water | waterlogged_placement | Obtain a dried ghast and verify a valid water placement site | Place it in water and verify the hydrated block state or criterion
husbandry/safely_harvest_honey | hive_honey_interaction | Locate a full hive or nest and prepare a bottle plus safe smoke coverage | Bottle honey under campfire smoke and check the result
husbandry/tactical_fishing | entity_bucketing | Verify a fish, a water bucket and a safe water-edge stance | Capture the fish into the bucket and verify inventory
husbandry/tame_an_animal | tame_and_ownership | Verify a tameable animal and the correct taming resource | Tame it and confirm the server criterion rather than a single interaction
husbandry/wax_on | copper_wax_interaction | Obtain honeycomb and a supported unwaxed copper block | Apply honeycomb and verify the waxed block
husbandry/breed_an_animal | animal_breeding | Observe two eligible adults and prepare their breeding food | Feed both and verify offspring or the advancement
husbandry/wax_off | copper_wax_interaction | Verify a waxed copper block and prepare an axe | Scrape wax off and verify the unwaxed block
"""


def _load_blueprints():
    plans = {}
    for line in _ROWS.strip().splitlines():
        path, gap, prerequisite, trigger = (part.strip() for part in line.split("|"))
        ident = "minecraft:" + path
        if ident in plans or not all((path, gap, prerequisite, trigger)):
            raise ValueError(f"Invalid competition blueprint: {ident}")
        plans[ident] = (gap, prerequisite, trigger)
    return plans


BLUEPRINTS = _load_blueprints()
