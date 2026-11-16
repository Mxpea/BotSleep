"""Offline unit tests for core/mining_targets.py."""

import unittest

from core.mining_targets import (
    best_usable_pickaxe,
    evaluate_home_trigger,
    evaluate_ore_block,
    parse_mining_targets,
    pickaxe_durability_ratio,
    pickaxe_tier,
)


class MiningTargetsTests(unittest.TestCase):
    def test_parse_targets_and_evaluate_ore_statuses(self):
        targets = parse_mining_targets({
            "iron": {"count": 2, "min_tool": "stone_pickaxe"},
            "diamond": {"count": 3, "min_tool": "iron_pickaxe"},
        })
        self.assertEqual(targets["iron"]["drop_item"], "raw_iron")
        self.assertIn("deepslate_iron_ore", targets["iron"]["ore_blocks"])

        # Mine iron with stone_pickaxe
        self.assertEqual(
            evaluate_ore_block("deepslate_iron_ore", targets, harvested_counts={"raw_iron": 1}, best_tool_tier=pickaxe_tier("stone_pickaxe")),
            ("mine", "iron", "raw_iron"),
        )
        # Iron quota reached -> skipped_quota
        self.assertEqual(
            evaluate_ore_block("iron_ore", targets, harvested_counts={"raw_iron": 2}, best_tool_tier=3),
            ("skipped_quota", "iron", "raw_iron"),
        )
        # Stone pickaxe vs diamond -> skipped_tool
        self.assertEqual(
            evaluate_ore_block("diamond_ore", targets, harvested_counts={}, best_tool_tier=pickaxe_tier("stone_pickaxe")),
            ("skipped_tool", "diamond", "diamond"),
        )
        # Unlisted copper -> seen
        self.assertEqual(
            evaluate_ore_block("copper_ore", targets, harvested_counts={}, best_tool_tier=3),
            ("seen", "copper", "raw_copper"),
        )

    def test_durability_ratio_and_best_usable_pickaxe(self):
        damaged_stone = {"name": "stone_pickaxe", "count": 1, "durability_used": 120}  # 11 / 131 = 8.4%
        fresh_wood = {"name": "wooden_pickaxe", "count": 1, "durability_used": 10}   # 49 / 59 = 83%
        self.assertLess(pickaxe_durability_ratio(damaged_stone), 0.10)
        self.assertEqual(best_usable_pickaxe([damaged_stone, fresh_wood], min_durability_ratio=0.10), "wooden_pickaxe")
        self.assertIsNone(best_usable_pickaxe([damaged_stone], min_durability_ratio=0.10))

    def test_home_trigger_thresholds_and_time_wrap_up(self):
        base_status = {"health": 20, "food": 20}
        healthy_inv = [
            {"name": "stone_pickaxe", "count": 1, "slot": 0},
            {"name": "torch", "count": 8, "slot": 1},
            {"name": "cobblestone", "count": 32, "slot": 2},
        ]
        self.assertFalse(evaluate_home_trigger(base_status, healthy_inv, remaining_seconds=120.0)["trigger"])

        # Low time (<30s) requests wrap-up WITHOUT triggering home trip
        wrap = evaluate_home_trigger(base_status, healthy_inv, remaining_seconds=20.0)
        self.assertFalse(wrap["trigger"])
        self.assertTrue(wrap["wrap_up"])

        # Low health (<12) -> low_health
        self.assertEqual(
            evaluate_home_trigger({"health": 10, "food": 20}, healthy_inv)["home_reason"],
            "low_health",
        )
        # Low food (<10) without edible items -> low_food; with bread -> ok
        self.assertEqual(
            evaluate_home_trigger({"health": 20, "food": 8}, healthy_inv)["home_reason"],
            "low_food",
        )
        self.assertIsNone(
            evaluate_home_trigger({"health": 20, "food": 8}, healthy_inv + [{"name": "bread", "count": 2, "slot": 3}])["home_reason"],
        )
        # Empty slots <= 3 -> inventory_full
        full_inv = list(healthy_inv) + [{"name": f"dirt_{i}", "count": 1, "slot": 3 + i} for i in range(31)]
        self.assertEqual(evaluate_home_trigger(base_status, full_inv)["home_reason"], "inventory_full")
        # Low torch / low blocks / worn-out pickaxe
        self.assertEqual(
            evaluate_home_trigger(base_status, [{"name": "stone_pickaxe", "count": 1, "slot": 0}, {"name": "cobblestone", "count": 32, "slot": 1}, {"name": "torch", "count": 2, "slot": 2}])["home_reason"],
            "low_torch",
        )
        self.assertEqual(
            evaluate_home_trigger(base_status, [{"name": "stone_pickaxe", "count": 1, "slot": 0}, {"name": "cobblestone", "count": 5, "slot": 1}, {"name": "torch", "count": 8, "slot": 2}])["home_reason"],
            "low_blocks",
        )
        self.assertEqual(
            evaluate_home_trigger(base_status, [{"name": "stone_pickaxe", "count": 1, "durability_used": 125, "slot": 0}, {"name": "cobblestone", "count": 32, "slot": 1}, {"name": "torch", "count": 8, "slot": 2}])["home_reason"],
            "no_pickaxe",
        )


if __name__ == "__main__":
    unittest.main()
