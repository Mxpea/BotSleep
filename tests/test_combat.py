"""Bounded combat and real client protocol tests; no game or provider requests."""

import copy
import math
import tempfile
import time
import threading
import unittest
from unittest.mock import Mock, patch

from agent.llm_planner import LLMSurvivalPlanner
from core.client import APIError, MinecraftAgentClient
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from skills.combat import CombatSkills, aim_angles, clear_entity_ray, combat_options


class Clock:
    def __init__(self): self.now = 0.
    def monotonic(self): return self.now
    def sleep(self, duration): self.now += max(.00001, duration)


class CombatWorld:
    def __init__(self):
        self.status = {"ready": True, "alive": True, "busy": None, "health": 20, "food": 20,
                       "dimension": "overworld", "gamemode": "survival", "on_ground": True,
                       "pos": [.5, 64, .5], "blocking": False, "held": None, "offhand": None}
        self.entities = [{"id": 5, "name": "skeleton", "type": "hostile", "pos": [2.7, 64, .5], "health": 20}]
        self.counts = {"iron_sword": 1}
        self.blocks, self.calls, self.effects = {}, [], {}

    @staticmethod
    def ok(value): return {"ok": True, "result": copy.deepcopy(value)}

    def get_status(self, **kwargs):
        self.calls.append(("status",))
        if "status" in self.effects: self.effects["status"]()
        return self.ok(self.status)

    def get_entities(self, **kwargs):
        self.calls.append(("entities",))
        if "entities" in self.effects: self.effects["entities"]()
        return copy.deepcopy(self.entities)

    def inventory(self, **kwargs):
        self.calls.append(("inventory",))
        return self.ok({"items": [{"name": k, "count": n} for k, n in self.counts.items() if n > 0],
                        "offhand": self.status["offhand"]})

    def get_block(self, pos, **kwargs):
        self.calls.append(("block", list(pos)))
        return self.ok(self.blocks.get(tuple(pos), {"name": "stone" if pos[1] < 64 else "air", "properties": {}}))

    def equip(self, item, slot="hand", **kwargs):
        self.calls.append(("equip", item))
        self.status["held"] = {"name": item, "count": 1}
        return self.ok({"held": self.status["held"]})

    def look_at(self, *pos, **kwargs):
        self.calls.append(("look", pos))
        return self.ok({"yaw": 0})

    def posture(self, shield=False, **kwargs):
        self.calls.append(("posture", shield))
        self.status["blocking"] = shield
        if shield: self.status["offhand"] = {"name": "shield", "count": 1}
        if "posture" in self.effects: return self.effects["posture"](shield)
        return self.ok({"blocking": shield})

    def control(self, **kwargs):
        self.calls.append(("control", kwargs))
        if "control" in self.effects: return self.effects["control"](kwargs)
        if kwargs.get("back"):
            yaw = kwargs["yaw"]
            self.status["pos"][0] += math.sin(yaw)
            self.status["pos"][2] += math.cos(yaw)
            return self.ok({"moved": 1})
        target = self.entities[0]
        target["health"] = target.get("health", 20) - 6
        return self.ok({"hits": [{"t_ms": 0, "entity": {"id": target["id"], "name": target["name"]}}]})

    def shoot(self, entity_id, **kwargs):
        self.calls.append(("shoot", entity_id, kwargs))
        if "shoot" in self.effects: return self.effects["shoot"]()
        self.counts["arrow"] -= 1
        self.entities[0]["health"] -= 6
        return self.ok({"fired": True, "projectiles": [{"id": 40, "name": "arrow"}]})

    def observation(self):
        return {"status": copy.deepcopy(self.status), "entities": copy.deepcopy(self.entities),
                "inventory": self.inventory()["result"]["items"], "fresh": True, "inventory_fresh": True,
                "observation_scope": "safety"}


class CombatTests(unittest.TestCase):
    def setUp(self):
        self.world, self.clock = CombatWorld(), Clock()
        self.skills = CombatSkills(self.world)
        self.patches = [patch("skills.combat.time.monotonic", self.clock.monotonic), patch("skills.combat.time.sleep", self.clock.sleep)]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def run_step(self, name=None, cancel=None):
        return self.skills.execute("fight_hostile", {"entity_id": 5, "expected_name": name or self.world.entities[0]["name"]}, cancel)

    def writes(self): return [c for c in self.world.calls if c[0] in {"equip", "look", "posture", "control", "shoot"}]

    def test_unshielded_melee_is_one_targeted_hit_with_damage_evidence(self):
        result = self.run_step()
        self.assertTrue(result.success, result)
        hits = [c for c in self.writes() if c[0] == "control"]
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0][1]["hit"], hits[0][1]["ms"], hits[0][1]["wait"]), (1, 50, True))
        self.assertTrue(result.data["damage_verified"])
        self.assertFalse(result.data["killed"])
        self.assertGreaterEqual(self.clock.now, .65)

    def test_shield_is_faced_warmed_lowered_for_hit_and_raised_again(self):
        self.world.counts["shield"] = 1
        result = self.run_step()
        self.assertTrue(result.success, result)
        writes = self.writes()
        self.assertLess(next(i for i, c in enumerate(writes) if c[0] == "look"), next(i for i, c in enumerate(writes) if c == ("posture", True)))
        self.assertEqual([c[1] for c in writes if c[0] == "posture"], [True, False, True])
        self.assertTrue(self.world.status["blocking"])
        self.assertGreaterEqual(self.clock.now, 1.25)
        self.assertFalse(result.data["shield_protection_verified"])

    def test_bow_and_crossbow_have_one_shot_with_ammo_verification(self):
        for weapon in ("bow", "crossbow"):
            with self.subTest(weapon=weapon):
                self.world = CombatWorld(); self.skills = CombatSkills(self.world)
                self.world.entities[0]["pos"] = [8.5, 64, .5]
                self.world.counts = {weapon: 1, "arrow": 3}
                result = self.run_step()
                self.assertTrue(result.success, result)
                self.assertEqual(len([c for c in self.writes() if c[0] == "shoot"]), 1)
                self.assertFalse(any(c[0] == "control" for c in self.writes()))
                self.assertEqual(result.data["ammo_used"], 1)
                shot = next(c for c in self.writes() if c[0] == "shoot")
                self.assertEqual(shot[2]["charge_ms"], 1400 if weapon == "crossbow" else 1100)

    def test_creeper_is_ranged_at_distance_and_withdraws_close_without_hit(self):
        self.world.entities[0].update(name="creeper", pos=[8.5, 64, .5])
        self.world.counts.update(bow=1, arrow=2)
        self.assertTrue(self.run_step().success)
        self.world.calls.clear(); self.world.entities[0]["pos"] = [2.7, 64, .5]
        result = self.run_step()
        self.assertTrue(result.success, result)
        controls = [c[1] for c in self.writes() if c[0] == "control"]
        self.assertEqual(len(controls), 1)
        self.assertTrue(controls[0]["back"])
        self.assertNotIn("hit", controls[0])
        self.assertEqual(result.data["phase"], "withdraw")
        self.assertFalse(result.data["killed"])

    def test_creeper_unsafe_escape_floor_never_presses_movement(self):
        self.world.entities[0]["name"] = "creeper"
        self.world.blocks[(-1, 63, 0)] = {"name": "lava"}
        result = self.run_step()
        self.assertFalse(result.success)
        self.assertTrue(result.data["retreat_required"])
        self.assertFalse(self.writes())

    def test_target_disappearing_is_not_kill(self):
        def hit(kwargs):
            self.world.entities.clear()
            return self.world.ok({"hits": [{"entity": {"id": 5, "name": "skeleton"}}]})
        self.world.effects["control"] = hit
        result = self.run_step("skeleton")
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["target_missing"])
        self.assertFalse(result.data["completed"])
        self.assertFalse(result.data["killed"])

    def test_fresh_zero_health_is_the_only_kill_confirmation(self):
        self.world.entities[0]["health"] = 6
        result = self.run_step()
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["completed"])
        self.assertTrue(result.data["killed"])

    def test_miss_does_not_repeat_and_wrong_entity_is_unknown(self):
        for selected, code in ((None, "attack_unverified"), ({"id": 99, "name": "Player"}, "action_unconfirmed")):
            with self.subTest(selected=selected):
                self.world.calls.clear()
                self.world.effects["control"] = lambda kwargs: self.world.ok({"hits": [{"entity": selected}]})
                result = self.run_step()
                self.assertEqual(result.error_code, code)
                self.assertEqual(len([c for c in self.writes() if c[0] == "control"]), 1)

    def test_transport_unknown_has_no_cleanup_or_further_attack(self):
        for code in ("not_connected", "request_timeout", "invalid_json"):
            with self.subTest(code=code):
                self.world.calls.clear()
                def fail(kwargs): raise APIError(code, "unknown")
                self.world.effects["control"] = fail
                result = self.run_step()
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(self.writes()[-1][0], "control")
                self.assertEqual(len([c for c in self.writes() if c[0] == "control"]), 1)

    def test_posthit_read_disconnect_keeps_unknown(self):
        original = self.world.control
        def hit(**kwargs):
            reply = original(**kwargs)
            self.world.effects["status"] = lambda: (_ for _ in ()).throw(APIError("not_connected", "lost"))
            return reply
        self.world.control = hit
        result = self.run_step()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["cleanup_skipped"], "disconnected_unknown")

    def test_precancelled_and_invalid_target_do_not_write(self):
        event = threading.Event(); event.set()
        self.assertEqual(self.run_step(cancel=event).error_code, "cancelled")
        self.assertEqual(self.run_step("cow").error_code, "bad_args")
        self.assertFalse(self.writes())

    def test_occlusion_bystander_and_multiple_enemies_stop_before_attack(self):
        for case in ("wall", "player", "animal", "enemy"):
            with self.subTest(case=case):
                self.world = CombatWorld(); self.skills = CombatSkills(self.world)
                if case == "wall": self.world.blocks[(1, 65, 0)] = {"name": "stone"}
                else: self.world.entities.append({"id": 9, "name": {"player": "Steve", "animal": "cow", "enemy": "zombie"}[case],
                                                  "type": "hostile" if case == "enemy" else case, "pos": [1.5, 64, .5]})
                self.assertFalse(self.run_step("skeleton").success)
                self.assertFalse(self.writes())

    def test_health_loss_during_preparation_causes_only_withdrawal(self):
        def equip(item, slot="hand", **kwargs):
            self.world.status["held"] = {"name": item}
            self.world.status["health"] = 17
            return self.world.ok({"held": {"name": item}})
        self.world.equip = equip
        result = self.run_step()
        self.assertFalse(any(c[0] == "control" and c[1].get("hit") for c in self.writes()))
        self.assertTrue(result.data["retreat_required"])

    def test_new_hostile_in_final_entity_read_prevents_the_hit(self):
        seen = 0
        def update():
            nonlocal seen
            seen += 1
            if seen == 3:
                self.world.entities.append({"id": 9, "name": "zombie", "type": "hostile", "pos": [.5, 64, 2.5]})
        self.world.effects["entities"] = update
        result = self.run_step("skeleton")
        self.assertEqual(result.error_code, "safety_changed")
        self.assertFalse(any(c[0] == "control" for c in self.writes()))

    def test_last_stance_change_after_entity_read_never_sends_old_aim(self):
        seen = 0
        def update():
            nonlocal seen
            seen += 1
            if seen == 3: self.world.status["pos"] = [.5, 64, 1.5]
        self.world.effects["entities"] = update
        result = self.run_step()
        self.assertEqual(result.error_code, "safety_changed")
        self.assertFalse(any(c[0] == "control" for c in self.writes()))

    def test_small_target_change_recomputes_the_verified_melee_angles(self):
        seen = 0
        def update():
            nonlocal seen
            seen += 1
            if seen == 3:
                self.world.entities[0]["pos"] = [2.7, 64, .8]
        self.world.effects["entities"] = update
        result = self.run_step()
        self.assertTrue(result.success, result)
        hits = [c[1] for c in self.writes() if c[0] == "control"]
        self.assertEqual(len(hits), 1)
        expected = aim_angles([.5, 64, .5], [2.7, 64, .8])
        self.assertAlmostEqual(hits[0]["yaw"], expected[0])
        self.assertAlmostEqual(hits[0]["pitch"], expected[1])
        self.assertEqual(result.data["target"]["pos"], [2.7, 64, .8])

    def test_small_same_cell_stance_change_after_ray_rejects_old_geometry(self):
        seen = 0
        def update():
            nonlocal seen
            seen += 1
            if seen == 3:
                self.world.status["pos"] = [.5, 64, .6]
        self.world.effects["entities"] = update
        result = self.run_step()
        self.assertEqual(result.error_code, "safety_changed", result)
        self.assertFalse(any(c[0] == "control" for c in self.writes()))

    def test_deadline_during_final_status_never_marks_unsent_hit_pending(self):
        final_read = False
        seen = 0
        def entities():
            nonlocal seen, final_read
            seen += 1
            if seen == 3:
                final_read = True
        def status():
            if final_read:
                self.clock.now = self.skills.MAX_SECONDS + 1
        self.world.effects.update(entities=entities, status=status)
        result = self.run_step()
        self.assertEqual(result.error_code, "combat_budget", result)
        self.assertNotEqual(result.data.get("action_state"), "unknown")
        self.assertFalse(any(c[0] == "control" for c in self.writes()))

    def test_cancel_received_with_hit_ack_keeps_unknown_and_no_new_write(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.side_effect = self.clock.sleep
        def hit(kwargs):
            cancel.is_set.return_value = True
            return self.world.ok({"hits": [{"entity": {"id": 5}}]})
        self.world.effects["control"] = hit
        result = self.run_step(cancel=cancel)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(self.writes()[-1][0], "control")

    def test_cancel_during_known_shield_warmup_sends_no_attack(self):
        self.world.counts["shield"] = 1
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.side_effect = self.clock.sleep
        def posture(shield):
            if shield: cancel.is_set.return_value = True
            return self.world.ok({"blocking": shield})
        self.world.effects["posture"] = posture
        result = self.run_step(cancel=cancel)
        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual(self.writes()[-1], ("posture", True))
        self.assertFalse(any(c[0] == "control" for c in self.writes()))

    def test_missing_projectile_and_ammo_change_is_unknown_not_shot_success(self):
        self.world.entities[0]["pos"] = [8.5, 64, .5]
        self.world.counts.update(bow=1, arrow=3)
        self.world.effects["shoot"] = lambda: self.world.ok({"fired": True, "projectiles": []})
        result = self.run_step()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertFalse(result.data["completed"])
        self.assertEqual(len([c for c in self.writes() if c[0] == "shoot"]), 1)

    def test_block_budget_and_unknown_support_never_authorize_a_hit(self):
        self.skills.MAX_BLOCK_READS = 1
        result = self.run_step()
        self.assertEqual(result.error_code, "combat_budget")
        self.assertEqual(result.data["block_reads"], 1)
        self.assertFalse(self.writes())

    def test_unobserved_target_full_entity_scan_and_invalid_inventory_are_rejected(self):
        observation = self.world.observation()
        self.assertFalse(combat_options(observation, 99, "skeleton")["allowed"])
        observation["entities"] *= 40
        self.assertFalse(combat_options(observation)["allowed"])
        observation = self.world.observation(); observation["inventory"] = [{"name": "iron_sword", "count": "1"}]
        self.assertFalse(combat_options(observation)["allowed"])

    def test_repeated_unconfirmed_damage_forces_retreat_and_cooldown(self):
        self.world.entities[0].pop("health")
        self.world.effects["control"] = lambda kwargs: self.world.ok({"hits": [{"entity": {"id": 5}}]})
        self.assertTrue(self.run_step().success)
        self.assertTrue(self.run_step().success)
        result = self.run_step()
        self.assertEqual(result.error_code, "combat_no_progress")
        self.assertTrue(result.data["retreat_required"])
        self.assertEqual(result.data["cooldown_seconds"], 30)
        controls = len([c for c in self.writes() if c[0] == "control"])
        self.assertEqual(self.run_step().error_code, "combat_cooldown")
        self.assertEqual(len([c for c in self.writes() if c[0] == "control"]), controls)

    def test_encounter_total_cap_applies_even_with_small_verified_progress(self):
        self.world.entities[0]["health"] = 100
        for _ in range(6): self.assertTrue(self.run_step().success)
        result = self.run_step()
        self.assertEqual(result.error_code, "combat_budget")
        self.assertEqual(len([c for c in self.writes() if c[0] == "control"]), 6)

    def test_encounter_life_change_does_not_inherit_old_cooldown(self):
        self.world.effects["control"] = lambda kwargs: self.world.ok({"hits": [{"entity": {"id": 5}}]})
        for _ in range(3): self.run_step()
        self.assertEqual(self.run_step().error_code, "combat_cooldown")
        self.world.status["last_death"] = {"t": 1000}
        self.assertTrue(self.run_step().success)

    def test_defense_release_is_explicit_read_checked_and_never_used_near_threat(self):
        self.world.counts["shield"] = 1
        self.assertTrue(self.run_step().success)
        self.assertTrue(self.skills.defense_active)
        count = len(self.writes())
        self.assertEqual(self.skills.release_defense().error_code, "combat_threat_present")
        self.assertEqual(len(self.writes()), count)
        self.world.entities.clear()
        result = self.skills.release_defense()
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["released"])
        self.assertFalse(self.skills.defense_active)
        self.assertEqual(self.writes()[-1], ("posture", False))

    def test_unknown_defense_release_never_retries(self):
        self.world.counts["shield"] = 1
        self.assertTrue(self.run_step().success)
        self.world.entities.clear()
        def posture(shield): raise APIError("request_timeout", "unknown release")
        self.world.effects["posture"] = posture
        count = len(self.writes())
        result = self.skills.release_defense()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["cleanup_skipped"], "combat_unknown")
        self.assertEqual(len(self.writes()), count + 1)

    def test_gate_requires_fresh_safe_state_and_has_real_unshielded_path(self):
        self.assertTrue(combat_options(self.world.observation())["allowed"])
        for field, value in (("health", 17), ("food", 7), ("on_ground", False), ("on_fire", True), ("gamemode", "creative")):
            observation = self.world.observation(); observation["status"][field] = value
            self.assertFalse(combat_options(observation)["allowed"])
        observation = self.world.observation(); observation["inventory_fresh"] = False
        self.assertFalse(combat_options(observation)["allowed"])


class StarterDefenseTests(unittest.TestCase):
    def setUp(self):
        self.world = CombatWorld()
        self.world.username = "StarterDefenseTest"
        self.world.base_url = "http://local/api/v1"
        self.world.entities[0]["name"] = "zombie"
        self.world.status.update(is_day=False, thundering=False,
                                 nearest_hostile={"id": 5, "name": "zombie", "dist": 2.2})
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = {**copy.deepcopy(DEFAULTS["survival"]), "objective": "starter", "state_dir": temp.name}
        self.brain = Mock()
        planner = LLMSurvivalPlanner({"enabled": True, "model": "fake"}, settings, brain=self.brain)
        self.harness = SurvivalHarness(self.world, settings, DEFAULTS["sleep"], planner=planner)
        self.addCleanup(self.harness.close)
        patcher = patch.object(self.harness.combat_skills, "_wait")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cycle(self):
        self.harness.tick()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.harness.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.001)
        self.fail("Starter defense did not finish")

    def test_isolated_zombie_gets_one_local_counterattack(self):
        self.assertNotIn("fight_hostile", self.harness.planner.tool_schemas)
        self.world.status["health"] = 19
        self.world.counts = {"wooden_sword": 1, "iron_axe": 1}
        result = self.run_cycle()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["weapon"], "iron_axe")
        self.assertTrue(result.data["damage_verified"])
        controls = [row[1] for row in self.world.calls if row[0] == "control"]
        self.assertEqual([row["hit"] for row in controls], [1])
        self.assertIsNone(self.harness.journal.state["pending"])
        self.brain.chat_completion.assert_not_called()

    def test_multiple_threats_and_weak_weapon_with_injury_keep_retreat(self):
        for extra, weapon, health in (
                (None, "iron_sword", 19), (None, "wooden_sword", 19),
                ("zombie", "iron_sword", 20), ("skeleton", "iron_sword", 20)):
            with self.subTest(extra=extra, weapon=weapon, health=health):
                current = self.world.observation()
                current["inventory"] = [{"name": weapon, "count": 1}]
                current["status"]["health"] = health
                if extra:
                    current["entities"].append({"id": 6, "type": "hostile", "name": extra,
                                                "pos": [14 if extra == "skeleton" else 5, 64, .5]})
                decision = self.harness._mandatory_safety(current)
                expected = "fight_hostile" if extra is None and weapon == "iron_sword" else "retreat_from"
                self.assertEqual(decision.name, expected)

    def test_extra_hostile_at_final_check_blocks_hit(self):
        scans = 0

        def spawn_extra():
            nonlocal scans
            scans += 1
            if scans == 4:
                self.world.entities.append({"id": 6, "type": "hostile", "name": "skeleton", "pos": [14, 64, .5]})

        self.world.effects["entities"] = spawn_extra
        result = self.run_cycle()
        self.assertEqual(result.error_code, "safety_changed", result)
        self.assertTrue(result.data["retreat_required"])
        self.assertFalse(any(row[0] == "control" for row in self.world.calls))
        self.brain.chat_completion.assert_not_called()

    def test_hostile_reported_after_ray_check_blocks_swing(self):
        geometry = self.harness.combat_skills._geometry
        calls = 0

        def report_creeper_after_ray(context, observation, target):
            nonlocal calls
            geometry(context, observation, target)
            calls += 1
            if calls == 3:
                self.world.status["nearest_hostile"] = {"id": 6, "name": "creeper", "dist": 1.0}

        with patch.object(self.harness.combat_skills, "_geometry", side_effect=report_creeper_after_ray):
            result = self.run_cycle()
        self.assertEqual(result.error_code, "safety_changed", result)
        self.assertEqual(result.data["reason"], "other_hostile_reported")
        self.assertFalse(any(row[0] == "control" for row in self.world.calls))
        self.assertIsNone(self.harness.journal.state["pending"])

    def test_new_enemy_during_preparation_is_handed_back_to_retreat(self):
        def add_enemy(*args, **kwargs):
            self.world.entities.append({"id": 6, "type": "hostile", "name": "zombie", "pos": [-2, 64, .5]})

        with patch.object(self.harness.combat_skills, "_wait", side_effect=add_enemy):
            result = self.run_cycle()
        self.assertEqual(result.error_code, "retreat_required", result)
        self.assertIsNone(self.harness.journal.state["pending"])
        self.assertFalse(any(row[0] == "control" for row in self.world.calls))
        self.assertEqual(self.harness._mandatory_safety(self.world.observation()).name, "retreat_from")

    def test_read_timeout_before_any_combat_write_keeps_retreat_available(self):
        def fail_combat_scan():
            if sum(row[0] == "entities" for row in self.world.calls) == 2:
                raise APIError("request_timeout", "Combat entity scan timed out")

        self.world.effects["entities"] = fail_combat_scan
        with patch.object(self.world, "stop", create=True) as stop:
            result = self.run_cycle()
            stop.assert_not_called()
        self.assertEqual(result.error_code, "request_timeout", result)
        self.assertEqual(result.data["action_state"], "not_executed")
        self.assertFalse(self.harness.paused)
        self.assertIsNone(self.harness.journal.state["pending"])
        self.assertEqual(self.harness._mandatory_safety(self.world.observation()).name, "retreat_from")

    def test_explicit_equip_rejection_keeps_retreat_available(self):
        with patch.object(self.world, "equip", side_effect=APIError(
                "missing_item", "The sword is no longer in inventory", 200)):
            result = self.run_cycle()
        self.assertEqual(result.error_code, "missing_item", result)
        self.assertEqual(result.data["action_state"], "not_executed")
        self.assertFalse(self.harness.paused)
        self.assertIsNone(self.harness.journal.state["pending"])
        self.assertEqual(self.harness._mandatory_safety(self.world.observation()).name, "retreat_from")

    def test_retreat_search_cooldown_precedes_counterattack(self):
        current = self.world.observation()
        threat = current["entities"][0]["pos"]
        recovery = self.harness._retreat_state(current, threat)
        recovery.update(retry_after=time.time() + 20, search_basis={"pos": current["status"]["pos"], "threat_pos": threat})
        self.assertEqual(self.harness._mandatory_safety(current).name, "wait")
        self.assertFalse(recovery["evacuating"])

    def test_optional_combat_reads_fail_without_losing_the_retreat_snapshot(self):
        get_status = self.world.get_status
        for failed_read in ("inventory", "status"):
            with self.subTest(failed_read=failed_read):
                self.world.calls.clear()

                def status(**kwargs):
                    if failed_read == "status" and ("inventory",) in self.world.calls:
                        raise APIError("request_timeout", "Status check timed out")
                    return get_status(**kwargs)

                with patch.object(self.world, "get_status", side_effect=status):
                    if failed_read == "inventory":
                        with patch.object(self.world, "inventory", return_value=self.world.ok({"items": None})):
                            current = self.harness.collector.collect_safety()
                    else:
                        current = self.harness.collector.collect_safety()
                self.assertFalse(current["inventory_fresh"])
                self.assertEqual(current["combat"]["reason"], "combat_observation_unavailable")
                self.assertEqual(self.harness._mandatory_safety(current).name, "retreat_from")

    def test_unconfirmed_hit_keeps_pending_and_does_not_replay(self):
        def lose_reply(kwargs):
            self.assertEqual(self.harness.journal.state["pending"]["decision"]["name"], "fight_hostile")
            raise APIError("request_timeout", "Lost attack response")

        self.world.effects["control"] = lose_reply
        result = self.run_cycle()
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(self.harness.paused)
        self.assertIsNotNone(self.harness.journal.state["pending"])
        self.harness._next_attempt = 0
        self.harness.tick()
        self.assertEqual(sum(row[0] == "control" for row in self.world.calls), 1)
        self.brain.chat_completion.assert_not_called()


class CombatClientTests(unittest.TestCase):
    def test_native_control_and_shoot_are_one_bounded_request(self):
        client = MinecraftAgentClient()
        with patch("core.client.requests.request", return_value=Mock(status_code=200, json=Mock(return_value={"ok": True, "result": {}}))) as request:
            client.control(hit=1, ms=50, yaw=0, pitch=0, wait=True, timeout_seconds=2)
            self.assertTrue(request.call_args.args[1].endswith("/control"))
            self.assertEqual(request.call_args.kwargs["json"]["stop_on"], ["hurt", "death"])
            self.assertEqual(request.call_args.kwargs["timeout"], 2)
            client.shoot(5, weapon="bow", charge_ms=1100, timeout_seconds=4)
            self.assertTrue(request.call_args.args[1].endswith("/shoot"))
            self.assertEqual(request.call_count, 2)
            client.shoot(5, weapon="crossbow")
            self.assertEqual(request.call_args.kwargs["json"]["charge_ms"], 1400)
            self.assertGreaterEqual(request.call_args.kwargs["timeout"], 7.4)

    def test_wrapper_rejects_unbounded_or_invalid_control(self):
        client = MinecraftAgentClient()
        with patch("core.client.requests.request") as request:
            for kwargs in ({"ms": 3001}, {"hit": 0}, {"yaw": float("nan")}, {"back": 1}):
                with self.assertRaises(APIError): client.control(**kwargs)
            request.assert_not_called()

    def test_combat_options_supports_expanded_easy_melee_mobs_at_12_hp(self):
        obs = {
            "fresh": True, "inventory_fresh": True,
            "status": {"ready": True, "alive": True, "busy": None, "health": 13, "food": 16,
                       "dimension": "overworld", "gamemode": "survival", "on_ground": True,
                       "pos": [0.5, 64, 0.5], "offhand": None},
            "inventory": [{"name": "iron_sword", "count": 1}],
            "entities": [{"id": 9, "name": "husk", "type": "hostile", "pos": [2.4, 64, 0.5]}],
        }
        res = combat_options(obs)
        self.assertTrue(res["allowed"], res)
        self.assertEqual(res["mode"], "melee")
