"""Mining and Harvesting Skills: Tree Chopping, Mineral Mining, Excavation."""

import time
import math
import requests
from typing import Optional, List, Dict, Any, Set, Tuple
from . import BaseSkill, SkillResult
from core.memory import MemoryEngine


class ChopTreeSkill(BaseSkill):
    """Locates the nearest accessible tree, cleanly harvests all contiguous logs,

    and respects configurable target count and working time limits.
    """

    KNOWN_LOGS = [
        "oak_log",
        "birch_log",
        "spruce_log",
        "jungle_log",
        "acacia_log",
        "dark_oak_log",
        "mangrove_log",
        "cherry_log",
        "pale_oak_log",
    ]

    def __init__(self, client, memory: Optional[MemoryEngine] = None):
        super().__init__(client)
        self.memory = memory or MemoryEngine()

    def validate_preconditions(self, **kwargs) -> bool:
        """Verifies bot is connected and ready."""
        st = self.client.get_status().get("result", {})
        return bool(st and st.get("ready"))

    def execute(
        self,
        target_count: int = 5,
        max_duration_seconds: float = 60.0,
        search_radius: int = 32,
        clean_entire_tree: bool = True,
        log_types: Optional[List[str]] = None,
    ) -> SkillResult:
        """Finds the nearest tree and cleanly chops logs until target count or time limit is reached.

        Args:
            target_count: Target number of logs to harvest (default 5).
            max_duration_seconds: Time limit in seconds for the operation (default 60s).
            search_radius: Max distance in blocks to search for trees (default 32).
            clean_entire_tree: If True, fully harvests the current tree before moving to the next.
            log_types: Specific log types to chop (defaults to all known wood logs).

        Returns:
            SkillResult containing detailed completion status, logs collected, and timing.
        """
        if not self.validate_preconditions():
            return SkillResult(
                success=False,
                error_code="not_ready",
                message="Bot is not connected or not ready.",
            )

        start_time = time.time()
        harvested_total = 0
        trees_cleared = 0
        harvested_breakdown: Dict[str, int] = {}
        target_logs = log_types or self.KNOWN_LOGS
        unreachable_trees: Set[Tuple[int, int]] = set()

        while harvested_total < target_count:
            # Check elapsed time limit
            elapsed = time.time() - start_time
            if elapsed >= max_duration_seconds:
                break

            # 1. Search for nearest candidate logs
            url = f"{self.client.base_url}/find_blocks"
            try:
                resp = requests.post(
                    url,
                    headers=self.client.headers,
                    json={"types": target_logs, "radius": search_radius, "limit": 40},
                    timeout=10,
                ).json()
            except Exception as e:
                return SkillResult(
                    success=False,
                    error_code="api_error",
                    message=f"Failed to scan for trees: {e}",
                    data={"harvested_count": harvested_total},
                )

            found_logs = resp.get("result", {}).get("blocks", [])
            if not found_logs:
                break

            # 2. Group logs by tree trunks and filter out unreachable ones
            tree_groups = self._group_logs_into_trees(found_logs)
            candidate_trees = [
                tg for tg in tree_groups
                if (tg["trunk_x"], tg["trunk_z"]) not in unreachable_trees
            ]

            if not candidate_trees:
                # All detected trees are either unreachable or no trees left
                break

            # Pick the closest candidate tree
            active_tree = candidate_trees[0]
            tree_key = (active_tree["trunk_x"], active_tree["trunk_z"])
            tree_logs = active_tree["logs"]

            if not clean_entire_tree:
                remaining_needed = target_count - harvested_total
                tree_logs = tree_logs[:remaining_needed]

            # 3. Chop all logs of this tree (accessible order: bottom-to-top)
            chopped_in_this_tree = 0
            tree_failed = False

            for log_entry in tree_logs:
                # Check time limit
                if time.time() - start_time >= max_duration_seconds:
                    break
                if harvested_total >= target_count and not clean_entire_tree:
                    break

                lx, ly, lz = log_entry["pos"]
                block_name = log_entry["name"]

                dig_res = self.client.dig(lx, ly, lz, collect=True)
                if dig_res.get("ok"):
                    harvested_total += 1
                    chopped_in_this_tree += 1
                    harvested_breakdown[block_name] = harvested_breakdown.get(block_name, 0) + 1
                else:
                    err = dig_res.get("error", {}).get("code", "unknown")
                    # If stalled or unreachable, mark tree as unreachable and switch to next tree
                    if err in ["stalled", "path_not_found", "out_of_reach"]:
                        unreachable_trees.add(tree_key)
                        tree_failed = True
                        break

            if not tree_failed and chopped_in_this_tree > 0:
                trees_cleared += 1

        total_elapsed = round(time.time() - start_time, 1)
        target_reached = harvested_total >= target_count
        time_limit_reached = total_elapsed >= max_duration_seconds

        success = target_reached or (harvested_total > 0 and not time_limit_reached)

        summary_msg = (
            f"Chopped {harvested_total}/{target_count} logs from {trees_cleared} tree(s) "
            f"in {total_elapsed}s. Target reached: {target_reached}, Time limit reached: {time_limit_reached}."
        )

        # Update memory engine episode
        self.memory.append_episode(
            event_type="chop_tree",
            summary=summary_msg,
            details={
                "harvested_count": harvested_total,
                "target_count": target_count,
                "elapsed_seconds": total_elapsed,
                "trees_cleared": trees_cleared,
                "breakdown": harvested_breakdown,
            },
        )

        return SkillResult(
            success=success,
            message=summary_msg,
            data={
                "harvested_count": harvested_total,
                "target_count": target_count,
                "elapsed_seconds": total_elapsed,
                "target_reached": target_reached,
                "time_limit_reached": time_limit_reached,
                "trees_cleared": trees_cleared,
                "breakdown": harvested_breakdown,
            },
            error_code=None if target_reached else ("time_limit_exceeded" if time_limit_reached else "no_more_trees"),
        )

    def _group_logs_into_trees(self, found_logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Clusters log blocks into distinct trees based on horizontal column proximity."""
        trees: List[Dict[str, Any]] = []

        for log in found_logs:
            lx, ly, lz = log["pos"]
            assigned = False
            for tree in trees:
                tx, tz = tree["trunk_x"], tree["trunk_z"]
                if abs(lx - tx) <= 1 and abs(lz - tz) <= 1:
                    tree["logs"].append(log)
                    tree["min_dist"] = min(tree["min_dist"], log.get("dist", 999))
                    assigned = True
                    break

            if not assigned:
                trees.append({
                    "trunk_x": lx,
                    "trunk_z": lz,
                    "min_dist": log.get("dist", 999),
                    "logs": [log],
                })

        # Sort logs within each tree from bottom (lowest Y) to top
        for tree in trees:
            tree["logs"].sort(key=lambda b: (b["pos"][1], b["dist"]))

        # Sort trees by closest distance to the bot
        trees.sort(key=lambda t: t["min_dist"])
        return trees
