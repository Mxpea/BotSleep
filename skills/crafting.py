"""Crafting Skill Module for BotSleep

Implements automated 2x2 and 3x3 item crafting, prerequisite synthesis,
automatic crafting table placement, ground stabilization, and retrieval.
"""

import logging
import time
from typing import Optional, Dict, Any, List

from . import BaseSkill, SkillResult
from core.recipes import RecipeBook

logger = logging.getLogger("BotSleep.Skills.Crafting")

SOLID_GROUND_BLOCKS = [
    "grass_block", "dirt", "coarse_dirt", "stone", "cobblestone",
    "deepslate", "sand", "gravel", "clay", "sandstone", "podzol"
]


class CraftItemSkill(BaseSkill):
    """Executes single or hierarchical crafting recipes.
    
    Behavior:
      - 2x2 recipes: crafted directly within player inventory.
      - 3x3 recipes:
          1. Searches for an existing placed crafting table within reach.
          2. If none found, crafts a crafting table in inventory (2x2).
          3. Ensures bot is standing on solid ground (moves away from leaves/water if necessary).
          4. Places the crafting table nearby on solid ground.
          5. Automatically crafts prerequisite ingredients (e.g. planks, sticks).
          6. Crafts the target item using the 3x3 crafting table.
          7. Optionally retrieves (digs) the crafting table to keep it in inventory.
    """

    def __init__(self, client, memory=None, recipe_book: Optional[RecipeBook] = None):
        super().__init__(client)
        self.memory = memory
        self.recipe_book = recipe_book or RecipeBook()

    def validate_preconditions(
        self,
        item: str,
        count: int = 1,
        auto_prerequisites: bool = True,
        **kwargs,
    ) -> bool:
        """Validates that the bot is ready and target item has valid recipes."""
        st = self.client.get_status()
        if not st.get("ok") or not st.get("result", {}).get("ready"):
            return False

        if not self.recipe_book.has_recipe(item):
            logger.warning(f"No known recipe for '{item}'")
            return False

        return True

    def _ensure_solid_ground(self) -> bool:
        """Ensures the bot is standing on solid ground rather than leaves, vines, or water."""
        st = self.client.get_status().get("result", {})
        in_water = st.get("in_water", False)
        pos = st.get("pos", [0, 64, 0])

        bx, by, bz = int(pos[0]), int(pos[1] - 0.5), int(pos[2])
        below_block = self.client.get_block(bx, by, bz).get("result", {})
        below_name = below_block.get("name", "")

        is_problematic = (
            in_water
            or below_name.endswith("_leaves")
            or "vine" in below_name
            or below_name in ["air", "water", "lava"]
        )

        if not is_problematic:
            return True

        logger.info(f"Bot on unstable footing ({below_name}, in_water={in_water}), repositioning to solid ground...")
        found = self.client.find_blocks(types=SOLID_GROUND_BLOCKS, radius=16, limit=1)
        blocks = found.get("result", {}).get("blocks", [])
        if blocks:
            tgt = blocks[0]["pos"]
            logger.info(f"Moving to solid ground at [{tgt[0]}, {tgt[1] + 1}, {tgt[2]}]...")
            mv_res = self.client.move_to(tgt[0], tgt[1] + 1, tgt[2], timeout_ms=10000)
            return mv_res.get("ok", False)

        return False

    def execute(
        self,
        item: str,
        count: int = 1,
        auto_prerequisites: bool = True,
        retrieve_table: bool = True,
        max_duration_seconds: float = 60.0,
        **kwargs,
    ) -> SkillResult:
        """Executes the automated crafting pipeline.

        Args:
            item: Item identifier (e.g. 'wooden_pickaxe', 'oak_planks', 'stick').
            count: Desired number of items to produce.
            auto_prerequisites: Whether to synthesize sub-components automatically.
            retrieve_table: Whether to dig and collect the crafting table after 3x3 craft.
            max_duration_seconds: Execution timeout limit.
        """
        start_time = time.time()
        logger.info(f"Initiating CraftItemSkill for {count}x {item}")

        # 1. Check basic preconditions
        if not self.recipe_book.has_recipe(item):
            return SkillResult(
                success=False,
                error_code="no_recipe",
                message=f"No crafting recipe known for '{item}'."
            )

        # 2. Check if crafting table is required and if one is already placed nearby
        is_3x3 = self.recipe_book.is_3x3(item)
        has_nearby_table = False
        nearby_table_pos = None

        if is_3x3:
            # Check for existing table in block vicinity
            found = self.client.find_blocks(types=["crafting_table"], radius=12, limit=1)
            blocks = found.get("result", {}).get("blocks", [])
            if blocks:
                has_nearby_table = True
                nearby_table_pos = blocks[0].get("pos")
                logger.info(f"Found existing crafting table at {nearby_table_pos}")

        # 3. Retrieve current inventory counts
        inv = self.client.get_inventory_counts()
        logger.info(f"Current inventory counts: {inv}")

        # 4. Generate recipe execution chain
        try:
            plan = self.recipe_book.plan_recipe_chain(
                target_item=item,
                target_count=count,
                inventory=inv,
                has_nearby_table=has_nearby_table,
            )
        except ValueError as ve:
            return SkillResult(
                success=False,
                error_code="insufficient_materials",
                message=str(ve),
                data={"inventory": inv}
            )

        logger.info(f"Crafting plan generated ({len(plan)} steps): {plan}")

        # 5. Execute steps sequentially
        placed_table_pos: Optional[List[float]] = None
        actions_completed: List[Dict[str, Any]] = []

        for step in plan:
            if time.time() - start_time > max_duration_seconds:
                return SkillResult(
                    success=False,
                    error_code="timeout",
                    message="Crafting sequence exceeded time limit.",
                    data={"completed_steps": actions_completed}
                )

            stype = step["type"]

            if stype == "craft":
                c_item = step["item"]
                c_count = step.get("count", 1)
                grid_mode = step.get("grid", "2x2")
                logger.info(f"Executing craft: {c_count}x {c_item} ({grid_mode})")

                res = self.client.craft(item=c_item, count=c_count)
                if not res.get("ok"):
                    err = res.get("error", {})
                    return SkillResult(
                        success=False,
                        error_code=err.get("code", "craft_failed"),
                        message=f"Failed crafting {c_item}: {err.get('message')}",
                        data={"step": step, "error": err}
                    )
                actions_completed.append(step)

            elif stype == "place_table":
                # Ensure solid ground first
                self._ensure_solid_ground()

                logger.info("Placing crafting table on nearby ground...")
                res = self.client.place_block(item="crafting_table")
                if not res.get("ok"):
                    # Retry with aggressive ground reposition
                    err = res.get("error", {})
                    if err.get("code") == "no_support":
                        logger.warning("place_block returned no_support, relocating to ground...")
                        self._ensure_solid_ground()
                        res = self.client.place_block(item="crafting_table")

                if not res.get("ok"):
                    err = res.get("error", {})
                    return SkillResult(
                        success=False,
                        error_code=err.get("code", "place_failed"),
                        message=f"Failed placing crafting table: {err.get('message')}",
                        data={"error": err}
                    )

                placed_pos = res.get("result", {}).get("pos")
                placed_table_pos = placed_pos
                logger.info(f"Crafting table placed successfully at {placed_table_pos}")

                # Save landmark to memory if memory engine is available
                if self.memory and placed_table_pos:
                    self.memory.record_landmark(
                        name="crafting_table",
                        category="crafting",
                        pos=placed_table_pos,
                        metadata={"temporary": retrieve_table}
                    )
                actions_completed.append({"type": "place_table", "pos": placed_table_pos})

            elif stype == "retrieve_table":
                if retrieve_table and placed_table_pos:
                    logger.info(f"Retrieving crafting table at {placed_table_pos}...")
                    dig_res = self.client.dig(
                        x=placed_table_pos[0],
                        y=placed_table_pos[1],
                        z=placed_table_pos[2],
                        collect=True
                    )
                    if dig_res.get("ok"):
                        logger.info("Crafting table successfully retrieved into inventory.")
                        actions_completed.append({"type": "retrieve_table", "pos": placed_table_pos})
                    else:
                        logger.warning(f"Could not retrieve crafting table: {dig_res.get('error')}")

        # 6. Verify final inventory
        final_inv = self.client.get_inventory_counts()
        gained = final_inv.get(item, 0) - inv.get(item, 0)
        total = final_inv.get(item, 0)

        elapsed = round(time.time() - start_time, 2)
        logger.info(f"Crafting complete in {elapsed}s. Final {item} in inventory: {total} (gained {gained})")

        return SkillResult(
            success=True,
            message=f"Successfully crafted {item} (total in inventory: {total}, elapsed: {elapsed}s).",
            data={
                "item": item,
                "requested_count": count,
                "total_in_inventory": total,
                "gained": gained,
                "duration_seconds": elapsed,
                "steps": actions_completed,
                "final_inventory": final_inv
            }
        )
