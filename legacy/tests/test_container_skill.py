"""Offline unit tests for ContainerSkill (skills/container.py)."""

import copy
import unittest

from skills.container import ContainerSkill


class SimContainerWorld:
    """In-memory chest/furnace simulator implementing window_view and window_act."""

    def __init__(self, *, bot_pos=(0.5, 64.0, 0.5)):
        self.blocks = {
            (2, 64, 0): {"name": "chest", "properties": {"type": "right"}},
            (2, 64, 1): {"name": "chest", "properties": {"type": "left"}},
            (2, 65, 0): {"name": "furnace", "properties": {"lit": "false"}},
            (2, 65, 1): {"name": "smoker", "properties": {}},
        }
        self.windows = {
            (2, 64, 0): {"container": {}},
            (2, 64, 1): None,  # Share chest contents with (2,64,0)
            (2, 65, 0): {"input": {}, "fuel": {}, "output": {}},
            (2, 65, 1): {"input": {}, "fuel": {}, "output": {}},
        }
        self.burning = {}
        self.smelt_ticks = 0
        self.status = {
            "ready": True,
            "alive": True,
            "busy": False,
            "health": 20,
            "food": 20,
            "in_lava": False,
            "on_fire": False,
            "pos": list(bot_pos),
        }
        self.items = {
            "cobblestone": 64,
            "dirt": 12,
            "raw_iron": 30,
            "coal": 6,
            "oak_planks": 8,
            "beef": 4,
        }
        self.full_chest_limit = 54 * 64

    def _win_for(self, key):
        if key == (2, 64, 1):
            key = (2, 64, 0)
        return self.windows[key]

    def get_status(self, **kwargs):
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def get_events(self, **kwargs):
        return []

    def inventory(self, **kwargs):
        return {
            "ok": True,
            "result": {
                "counts": dict(self.items),
                "items": [{"name": k, "count": v} for k, v in self.items.items() if v > 0],
            },
        }

    def get_block(self, x, y=None, z=None, **kwargs):
        pos = x if y is None else (x, y, z)
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        blk = copy.deepcopy(self.blocks.get(key, {"name": "air", "properties": {}}))
        blk["pos"] = list(key)
        return {"ok": True, "result": blk}

    def move_to(self, x, y, z, range=1.0, **kwargs):
        self.status["pos"] = [float(x), float(y), float(z)]
        return {"ok": True, "result": {"pos": list(self.status["pos"])}}

    def window_view(self, pos, **kwargs):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        win = self._win_for(key)
        # Simulate smelting progress when viewing an active furnace
        if key == (2, 65, 0) and win["input"] and (win["fuel"] or self.burning.get(key)):
            self.smelt_ticks += 1
            for in_name, in_cnt in list(win["input"].items()):
                if in_cnt > 0:
                    take_n = min(in_cnt, 15)
                    win["input"][in_name] -= take_n
                    if win["input"][in_name] <= 0:
                        win["input"].pop(in_name, None)
                    out_name = "iron_ingot" if "iron" in in_name else "cooked_beef"
                    win["output"][out_name] = win["output"].get(out_name, 0) + take_n
                    self.burning[key] = True
            if not win["input"]:
                self.burning[key] = False
        sections = {
            sec: [{"name": k, "count": v} for k, v in bag.items() if v > 0]
            for sec, bag in win.items()
        }
        return {
            "ok": True,
            "result": {
                "window": "furnace" if "input" in win else "chest",
                "sections": sections,
                "info": {"burning": bool(self.burning.get(key))},
            },
        }

    def window_act(self, pos, ops, **kwargs):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        win = self._win_for(key)
        done = []
        for op in ops:
            kind = op["op"]
            if kind == "put":
                to_sec = op["to"]
                item = op["item"]
                cnt = int(op.get("count", 1))
                have = self.items.get(item, 0)
                if have <= 0:
                    return {"ok": False, "error": {"code": "missing_item", "message": f"No {item}"}}
                put_n = min(cnt, have)
                if sum(win[to_sec].values()) + put_n > self.full_chest_limit:
                    return {"ok": False, "error": {"code": "section_full", "message": "Container full"}}
                self.items[item] -= put_n
                if self.items[item] <= 0:
                    self.items.pop(item, None)
                win[to_sec][item] = win[to_sec].get(item, 0) + put_n
                done.append({"op": "put", "item": item, "count": put_n, "to": to_sec})
            elif kind == "take":
                from_sec = op["from"]
                item = op.get("item")
                cnt = op.get("count")
                bag = win[from_sec]
                if not item:
                    item = next((k for k, v in bag.items() if v > 0), None)
                avail = bag.get(item, 0) if item else 0
                if avail <= 0:
                    return {"ok": False, "error": {"code": "missing_item", "message": f"Missing {item}"}}
                take_n = avail if (cnt is None or from_sec == "output") else min(int(cnt), avail)
                bag[item] -= take_n
                if bag[item] <= 0:
                    bag.pop(item, None)
                self.items[item] = self.items.get(item, 0) + take_n
                done.append({"op": "take", "item": item, "count": take_n, "from": from_sec})
        return {"ok": True, "result": {"done": done, "window": self.window_view(pos)["result"]}}

    def stop(self):
        return {"ok": True, "result": {}}


class ContainerSkillTests(unittest.TestCase):
    def test_chest_batch_deposit_and_withdraw(self):
        w = SimContainerWorld()
        skill = ContainerSkill(w)

        dep = skill.deposit([2, 64, 0], {"cobblestone": 64, "dirt": 12})
        self.assertTrue(dep.success, dep)
        self.assertEqual(dep.data["deposited"], {"cobblestone": 64, "dirt": 12})
        self.assertNotIn("cobblestone", w.items)
        self.assertNotIn("dirt", w.items)

        insp = skill.inspect([2, 64, 1])  # Double chest second half sees same contents
        self.assertTrue(insp.success, insp)
        self.assertEqual(insp.data["counts"]["container"], {"cobblestone": 64, "dirt": 12})

        wd = skill.withdraw([2, 64, 0], {"cobblestone": 32, "dirt": 5})
        self.assertTrue(wd.success, wd)
        self.assertEqual(wd.data["withdrawn"], {"cobblestone": 32, "dirt": 5})
        self.assertEqual(w.items["cobblestone"], 32)
        self.assertEqual(w.items["dirt"], 5)
        self.assertEqual(w.windows[(2, 64, 0)]["container"]["dirt"], 7)

    def test_deposit_handles_partial_when_chest_full_and_missing_item(self):
        w = SimContainerWorld()
        w.full_chest_limit = 20
        skill = ContainerSkill(w)

        strict = skill.deposit([2, 64, 0], {"cobblestone": 64}, ensure_all=True)
        self.assertFalse(strict.success)
        self.assertEqual(strict.error_code, "section_full")

        w.full_chest_limit = 54 * 64
        miss = skill.deposit([2, 64, 0], {"diamond": 3}, ensure_all=True)
        self.assertFalse(miss.success)
        self.assertEqual(miss.error_code, "missing_item")

    def test_furnace_smelts_30_raw_iron_computes_coal_and_collects_output(self):
        w = SimContainerWorld()
        # Seed stale output in furnace to verify auto_collect_existing clears it
        w.windows[(2, 65, 0)]["output"]["charcoal"] = 2
        skill = ContainerSkill(w)

        # Smoker rejects iron
        bad_recipe = skill.smelt([2, 65, 1], "raw_iron", 4)
        self.assertFalse(bad_recipe.success)
        self.assertEqual(bad_recipe.error_code, "unsupported_recipe")

        # Smelt all 30 raw_iron -> 30 iron_ingot using optimal coal (ceil(30/8)=4 coal)
        res = skill.smelt([2, 65, 0], "raw_iron", 30, wait_complete=True, poll_interval_seconds=0.01)
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["gained"], 30)
        self.assertEqual(res.data["pre_collected"], {"charcoal": 2})
        self.assertEqual(w.items.get("iron_ingot"), 30)
        self.assertEqual(w.items.get("charcoal"), 2)
        # Optimal coal consumed: ceil(30 / 8) = 4, leaving 2 out of 6
        self.assertEqual(w.items.get("coal"), 2)


if __name__ == "__main__":
    unittest.main()
