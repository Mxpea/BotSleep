"""Harness integration for goal-level tasks and one deferred native tool result.

The controller chooses from fresh observations; this layer never stores a queue
of game writes. Each child uses the ordinary harness journal and skill checks.
"""

import copy
import math
import time
from dataclasses import asdict

from agent.brain import LLMError
from agent.survival_planner import Decision
from core.build_projection import wood_material
from core.client import APIError
from core.domain import LOG_NAMES, PLANK_NAMES, extract_blocks, finite_number, inventory_counts, parse_pos
from core.model_gate import ModelGateError
from core.starter_mining_task import recover_interrupted_mine
from core.survival_mainline import choose_chapter, mainline_enabled, same_goal
from skills import SkillResult
from skills.action_workflow import WorkflowGuard, WorkflowStepError, WorkflowUncertainError


class _TaskWindow:
    """Let each nested workflow observe the same cancellation and time limit."""

    def __init__(self, cancel, seconds):
        self.cancel = cancel
        self.deadline = time.monotonic() + seconds

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def is_set(self):
        return self.cancel.is_set() or self.remaining() == 0

    def wait(self, seconds=None):
        remaining = self.remaining()
        self.cancel.wait(remaining if seconds is None else min(remaining, seconds))
        return self.is_set()


class AutonomyRuntime:
    def _init_autonomy(self):
        self.task_controller = None
        self._task_model_decision = None
        self._task_child = False
        self._task_safety = False
        self._task_resume_requested = False
        self._task_event_signature = None
        self._mainline_decision = None
        self._mainline_repair_id = None
        self._mainline_repair_after = 0
        self.mainline_enabled = mainline_enabled(self.settings)
        if not self.mainline_enabled:
            self.context.pop("mainline", None)
        if self.daily and self.settings.get("autonomy", {}).get("enabled", True):
            from core.autonomous_tasks import AutonomousTaskController
            saved = self.context.get("autonomous_task")
            objective = self.settings["objective"]
            owner = self.context.get("autonomous_task_objective")
            if owner is None and saved and saved.get("task_id") and saved["task_id"] == self.context.get("mainline_task_id"):
                owner = "daily"
            if (saved and owner and owner != objective and not self.journal.state.get("pending")
                    and saved.get("status") != "unknown" and not saved.get("pending_digest")):
                self._record("INFO", "TASK_SCOPE_CHANGED", {"from": owner, "to": objective, "task_id": saved.get("task_id")})
                saved = None
                for key in ("autonomous_task", "task_execution", "autonomous_task_objective"):
                    self.context.pop(key, None)
                self._checkpoint(context=self.context)
            self.task_controller = AutonomousTaskController(self.settings, saved=saved, scope=self.journal.scope)
            if self.task_controller.snapshot().get("status") == "unknown":
                self._paused = True
                self.pause_reason = "task_outcome_requires_resume"
        self.context.setdefault("autonomy_stats", {"model_planning_rounds": 0, "model_calls_avoided": 0})

    def _task_snapshot(self):
        if self.task_controller is None:
            return None
        return {**self.task_controller.snapshot(), **self.context.get("autonomy_stats", {})}

    def _task_active(self):
        return (self.task_controller is not None
                and self.task_controller.snapshot().get("status") in {"running", "suspended"})

    def _update_mainline(self, observation):
        self._mainline_decision = None
        if not self.mainline_enabled or not self.daily_board:
            return
        status = observation.get("status") or {}
        if (not self.task_controller._fresh(observation) or status.get("gamemode") != "survival"
                or status.get("dimension") not in ("overworld", "minecraft:overworld")):
            if self.context.get("mainline"):
                self.context["mainline"]["fresh"] = False
            return
        old = self.context.get("mainline") or {}
        view, self._mainline_decision = choose_chapter(observation, self.daily_board, self.settings,
                                                       self.context.setdefault("mainline_sites", {}))
        if old.get("stage") != view["stage"]:
            self._mainline_repair_id = None
            self._mainline_repair_after = 0
            self._record("INFO", "MAINLINE_STAGE", {"mainline": view})
        self.context["mainline"] = view

    def _mainline_action(self, observation):
        if not self.mainline_enabled:
            return None
        goal = self._mainline_decision
        if goal is None or self.journal.state.get("pending"):
            return Decision("wait", {"seconds": 2}, "Reobserve before choosing a mainline goal", "observe")
        if self.daily_policy.validate(goal, observation, context=self.context):
            return Decision("wait", {"seconds": 2}, "Mainline production requires a fresh safe opening", "observe")
        task = self._task_snapshot() or {}
        if (goal.name == "run_survival_task" and task.get("status") == "needs_ai"
                and same_goal(task.get("args"), goal.args)):
            now = time.monotonic()
            if self._mainline_repair_id != task["task_id"]:
                if now < self._mainline_repair_after:
                    return Decision("wait", {"seconds": min(5, self._mainline_repair_after - now)},
                                    "Local recovery exhausted; recheck before another repair request", "observe")
                self._mainline_repair_id = task["task_id"]
                self._mainline_repair_after = now + 30
                if self.uses_llm and not self._model_blocked:
                    return None  # One serial, stage-scoped repair turn.
        return goal

    def _local_opening(self, observation):
        """Start one observed tool goal through the same controller used by native calls."""
        if (self.task_controller is None or self.settings.get("objective") != "daily"
                or self.settings.get("autonomy", {}).get("local_opening") is not True
                or self.settings.get("operator_goal") or self.settings.get("intent", "survival") != "survival"
                or self._task_snapshot().get("status") != "idle"
                or self.journal.state.get("pending") or self._model_blocked
                or self._model_worker is not None and self._model_worker.pending):
            return None
        gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
        if gate is not None:
            try:
                if gate.snapshot()["state"] != "idle":
                    return None
            except ModelGateError:
                return None
        if (observation.get("fresh") is not True or observation.get("stale") is True
                or observation.get("observation_scope") == "safety" or observation.get("inventory_fresh") is False):
            return None
        row = next((row for row in (self.daily_board or {}).get("tasks", []) if row["id"] == "starter_tools"), None)
        action = (row or {}).get("next_action")
        if not action or action["name"] not in {"craft_item", "place_item", "gather_block", "gather_materials",
                                                 "clear_foliage", "take_items"}:
            return None
        decision = Decision("run_survival_task", {"task": "starter_tools"},
                            "Current observations support starting the basic tool goal", "autonomous_task")
        return decision if self.daily_policy.validate(decision, observation, context=self.context) is None else None

    def _save_task(self, event=None):
        if self.task_controller is None:
            return
        self.context["autonomous_task_objective"] = self.settings["objective"]
        self.context["autonomous_task"] = self.task_controller.state()
        self.context["task_execution"] = self._task_snapshot()
        self._checkpoint(context=self.context)
        if event:
            self._record("WARN" if event in {"TASK_NEEDS_AI", "TASK_UNKNOWN"} else "INFO",
                         event, {"task_execution": self._task_snapshot()})

    def _task_start(self, decision, observation):
        if self.task_controller is None:
            return SkillResult(False, "Goal execution is disabled", {"executed": False}, "unsupported_skill")
        if self._task_active() or self.journal.state.get("pending"):
            return SkillResult(False, "Resolve the existing task or action before accepting another goal",
                               {"executed": False}, "task_conflict")
        try:
            cautious = (self.context.get("survival_constraints") or {}).get("scenario") == "cautious_bootstrap"
            self.context["cautious_bootstrap"] = cautious
            if cautious:
                self.context["cautious_bootstrap_basis"] = copy.deepcopy(observation["status"])
            self.task_controller.start(decision.args, observation, self.context)
        except ValueError as error:
            return SkillResult(False, str(error), {"executed": False}, "bad_args")
        if self.mainline_enabled:
            self.context["mainline_task_id"] = self._task_snapshot()["task_id"]
            view = self.context.get("mainline") or {}
            self.context["mainline_task_stage"] = (view.get("stage")
                if same_goal(decision.args, view.get("goal")) else None)
        if self._model_feedback:
            # Keep the original tool_call_id open until the goal ends. Children
            # are not fabricated native tool calls and never trigger the model.
            self._task_model_decision = copy.deepcopy(decision)
            self._model_feedback = False
        self._save_task("TASK_STARTED")
        return SkillResult(True, "Goal accepted; local substeps will run without model calls", {
            "accepted": True, "completed": False, "executed": False,
            "task_id": self._task_snapshot().get("task_id")})

    def _remote_log_waypoint(self, observation, log_names=LOG_NAMES, *, blocked=None):
        origin = parse_pos((observation.get("status") or {}).get("pos"))
        if origin is None:
            return None
        now = time.time()
        blocked_keys = [key for key, until in (blocked or {}).items() if finite_number(until) and until > now]
        blocked_xz = set()
        for bkey in blocked_keys:
            parts = str(bkey).split(",")
            if len(parts) == 3:
                try:
                    blocked_xz.add((int(parts[0]), int(parts[2])))
                except ValueError:
                    pass
        for row in extract_blocks(observation):
            if row["name"] not in log_names:
                continue
            rpos = row["pos"]
            if abs(float(rpos[1]) - float(origin[1])) > 3.1:
                continue
            if (math.floor(rpos[0]), math.floor(rpos[2])) in blocked_xz:
                continue
            return None
        def candidates(rows):
            return [row["pos"] for row in extract_blocks({"blocks": rows}) if row["name"] in log_names
                    and (math.floor(row["pos"][0]), math.floor(row["pos"][2])) not in blocked_xz]

        try:
            rows = self.client.get_blocks(radius=64, types=sorted(log_names), limit=12)
        except APIError:
            return None
        waypoints = candidates(rows)
        if not waypoints:
            return None
        best = min(
            waypoints,
            key=lambda p: (0 if abs(float(p[1]) - float(origin[1])) <= 4.0 else 1, math.dist(origin, p)),
        )
        dist = math.dist(origin, best)
        if dist <= 24.0:
            if abs(float(best[0]) - float(origin[0])) + abs(float(best[2]) - float(origin[2])) <= 4.0:
                return None
            return [float(best[0]), float(best[1]), float(best[2])]
        scale = 24.0 / dist
        return [
            round(float(origin[0]) + (float(best[0]) - float(origin[0])) * scale, 1),
            round(float(origin[1]) + (float(best[1]) - float(origin[1])) * scale, 1),
            round(float(origin[2]) + (float(best[2]) - float(origin[2])) * scale, 1),
        ]

    _MINING_CLUTTER_KEEP = (
        ("granite", 0), ("diorite", 0), ("andesite", 0), ("cobbled_deepslate", 0),
        ("tuff", 0), ("gravel", 8), ("flint", 4), ("leaf_litter", 0),
        ("red_mushroom", 0), ("brown_mushroom", 0), ("rotten_flesh", 0),
        ("raw_copper", 0), ("sand", 0), ("dirt", 0), ("cobblestone", 64),
    )

    def _discard_mining_clutter(self, guard):
        from core.mining_targets import estimate_empty_main_slots

        if not callable(getattr(self.client, "drop", None)):
            return
        raw = guard.read(self.client.inventory, timeout_seconds=guard.remaining_timeout(5))
        items = (raw.get("result") or {}).get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list) or (len(items) < 22 and estimate_empty_main_slots(items) > 12):
            return
        counts = inventory_counts({"inventory": items})
        for name, keep in self._MINING_CLUTTER_KEEP:
            excess = counts.get(name, 0) - keep
            if excess <= 0 or (name == "dirt" and counts.get("cobblestone", 0) < 32):
                continue
            guard.check_status()
            receipt = guard.write(self.client.drop, name, excess)
            after = (receipt.get("result") or {}).get("inventory") if isinstance(receipt, dict) else None
            if (not isinstance(after, dict) or after.get(name, 0) != keep
                    or any(type(amount) is not int or amount < 0 for amount in after.values())
                    or any(after.get(item, 0) < amount for item, amount in counts.items() if item != name)):
                guard.fail_uncertain("action_unconfirmed", "Discarded inventory amount could not be confirmed")
            counts = after

    def _run_task_workflow(self, decision, observation):
        if (decision.goal in {"deep_mine", "scout_surface", "nether_portal"}
                and observation["status"].get("dimension") not in {"overworld", "minecraft:overworld"}):
            return SkillResult(False, "Surface expeditions require the overworld", {"executed": False}, "unsafe_dimension")
        runners = {"deep_mine": (self._run_task_deep_mine, 1140),
                   "scout_surface": (self._run_task_scout, 55),
                   "equip_loadout": (self._run_task_equip_loadout, 30),
                   "nether_portal": (self._run_task_nether_portal, 390)}
        run, cap = runners[decision.goal]
        available = cap
        if not self.daily_policy.advisory:
            available = min(available, self.task_controller.remaining_seconds())
        global_seconds = self._remaining_budget()["remaining_seconds"]
        if global_seconds is not None:
            available = min(available, global_seconds)
        if available < 1:
            return SkillResult(False, "No task time left for a workflow", {"executed": False}, "task_budget_exhausted")
        window = _TaskWindow(self._cancel, available)
        try:
            return run(decision, observation, window)
        except (WorkflowStepError, WorkflowUncertainError) as error:
            return SkillResult(False, str(error), dict(error.data), error.code)

    def _run_task_deep_mine(self, decision, observation, window):
        from core.memory import MemoryEngine
        from skills.mining import MineShaftSkill

        spec = self.task_controller.state().get("pending_deep_mine") or {}
        resource = spec.get("resource")
        if resource not in {"coal", "raw_iron", "diamond", "cobblestone"}:
            return SkillResult(False, "Missing mining target", {"executed": False}, "bad_args")
        count = max(1, min(32, int(spec["count"])))
        guard = WorkflowGuard(self.client, cancel_event=window, timeout_seconds=window.remaining())
        self._discard_mining_clutter(guard)
        guard.check_budget()
        memory = MemoryEngine(str(self.journal.directory / "deep_mine_memory"))
        miner = MineShaftSkill(self.client, memory=memory)
        deep_home_thresholds = {"min_torches": 0, "min_blocks": 0, "min_empty_slots": 0}
        if resource == "cobblestone":
            start_surface = [math.floor(coord) for coord in observation["status"]["pos"]]
            target_y = max(36, start_surface[1] - 11)
            before_c = observation["counts"].get("cobblestone", 0)
            res = miner.build_mine_shaft(
                origin=None,
                radius=24,
                branch_length=4,
                place_workstation=False,
                return_to_surface=True,
                target_y=target_y,
                mining_mode="single",
                rib_length=3,
                max_trunk_steps=6,
                home_thresholds=deep_home_thresholds,
                cancel_event=window,
                timeout_seconds=min(210.0, window.remaining()),
                allow_damage=True,
            )
            data = dict(res.data or {}) if isinstance(res.data, dict) else {}
            data.setdefault("executed", True)
            if not res.success:
                return res
            # build_mine_shaft already returns to the requested surface anchor.
            # Confirm its position without starting another movement workflow.
            status = guard.check_status()
            pos = parse_pos(status.get("pos"))
            anchor = parse_pos(data.get("surface_entry")) or start_surface
            on_surface = pos is not None and math.dist(pos, anchor) <= 1.6
            surface_chk = {"on_surface": on_surface, "pos": pos}
            data["surface_return"] = surface_chk
            raw = guard.read(self.client.inventory, timeout_seconds=guard.remaining_timeout(5))
            payload = raw.get("result") if isinstance(raw, dict) else None
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                guard.fail_uncertain("action_unconfirmed", "Mined cobblestone inventory could not be verified")
            after_c = inventory_counts({"inventory": items})
            gained_cobble = max(0, int(after_c.get("cobblestone", 0)) - before_c)
            if gained_cobble > 0:
                data.setdefault("gained", {})["cobblestone"] = gained_cobble
            ok = gained_cobble > 0 and on_surface
            err = res.error_code if not ok else None
            if not ok and err is None:
                err = "no_cobblestone_found" if gained_cobble <= 0 else "surface_return_failed"
            return SkillResult(ok, str(res.message), data, err)

        if resource == "diamond":
            targets = {"diamond": {"count": count, "min_tool": "iron_pickaxe"}}
        elif resource == "raw_iron":
            targets = {"iron": {"count": count, "min_tool": "stone_pickaxe"}}
        else:
            targets = {"coal": {"count": count, "min_tool": "wooden_pickaxe"}}
        if self.campaign is not None:
            deep_home_thresholds["min_pickaxe_tool"] = next(iter(targets.values()))["min_tool"]
        total_t = min({"diamond": 1140, "raw_iron": 420, "coal": 540}[resource], window.remaining())
        band_t = min({"diamond": 960, "raw_iron": 360, "coal": 460}[resource], total_t * .85)
        res = miner.mine_targets_from_surface(
            targets=targets,
            cave_policy={"explore_timeout_seconds": min(220.0, total_t), "allow_tall_cave_subgraph": True},
            home_thresholds=deep_home_thresholds,
            prefer_caves_first=(resource in {"raw_iron", "coal"}),
            cave_search_radius=40,
            max_caves=3,
            place_workstation=False,
            rib_length=12 if resource in {"diamond", "raw_iron"} else 6,
            rib_spacing=3 if resource in {"diamond", "raw_iron"} else 4,
            max_trunk_steps=36 if resource == "diamond" else 28 if resource == "raw_iron" else 10,
            band_timeout_seconds=band_t,
            total_timeout_seconds=total_t,
            cancel_event=window,
            allow_damage=True,
        )
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        data.setdefault("executed", True)
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _run_task_scout(self, decision, observation, window):
        from core.memory import MemoryEngine
        from skills.scouting import SurfaceScoutSkill

        memory = MemoryEngine(str(self.journal.directory / "scout_memory"))
        counts = (observation.get("counts") or {}) if isinstance(observation, dict) else {}
        prefer_wood = sum(counts.get(n, 0) for n in (*LOG_NAMES, *PLANK_NAMES)) == 0
        distance = int(decision.args.get("distance", 256)) if isinstance(decision.args, dict) else 256
        max_hops = max(1, min(4, math.ceil(distance / 64)))
        if self.continuous:
            # Allow about 18 seconds per hop, including movement and nearby scans.
            # An expired planning estimate still permits a short attempt.
            estimated_hops = max(1, self._budget_advice()["remaining_seconds"] // 18)
            max_hops = min(max_hops, estimated_hops)
        raw_r = decision.args.get("scan_radius", 48) if isinstance(decision.args, dict) else 48
        scan_radius = max(16, min(64, int(raw_r)))
        goal_focus = (
            "lava"
            if any(counts.get(k, 0) > 0 for k in ("bucket", "water_bucket", "iron_pickaxe", "flint_and_steel"))
            else "auto"
        )
        attempt = int(self.context.get("scout_attempts", 0))
        self.context["scout_attempts"] = attempt + 1
        res = SurfaceScoutSkill(self.client, memory=memory).execute(
            scan_radius=scan_radius,
            hop_distance=64.0,
            max_hops=max_hops,
            distance=distance,
            prefer_wood=prefer_wood,
            goal_focus=goal_focus,
            attempt=attempt,
            timeout_seconds=window.remaining(),
            cancel_event=window,
        )
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        if res.success:
            self.context["last_scout_report"] = copy.deepcopy(data)
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _run_task_equip_loadout(self, decision, observation, window):
        from skills.equipment import EquipmentSkill

        prefer_offhand = (decision.args.get("prefer_offhand", "shield") if isinstance(decision.args, dict) else "shield")
        res = EquipmentSkill(self.client).execute(
            "auto_equip_best",
            prefer_offhand=prefer_offhand,
            timeout_seconds=window.remaining(),
            cancel_event=window,
        )
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _run_task_wide_find(self, decision, observation, resource_type: str):
        find_fn = getattr(self.client, "find_blocks", None) or getattr(self.client, "get_blocks", None)
        origin = ((observation.get("status") or {}).get("pos") if isinstance(observation, dict) else None)
        if not callable(find_fn) or not isinstance(origin, (list, tuple)) or len(origin) != 3:
            return SkillResult(False, f"Cannot search wide {resource_type}", {"executed": False}, "search_unavailable")
        try:
            rows = find_fn(types=[resource_type], radius=64, limit=32) or []
        except Exception:
            rows = []
        pts = [
            row.get("pos") for row in rows
            if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple)) and len(row["pos"]) == 3
        ]
        if not pts:
            return SkillResult(False, f"No {resource_type} found within 64 blocks", {"executed": False}, f"no_{resource_type}_found")
        pts.sort(key=lambda p: (0 if abs(float(p[1]) - float(origin[1])) <= 6 else 1, math.dist(origin, p)))
        target = pts[0]
        from skills.survival import SurvivalSkills
        return SurvivalSkills(self.client).execute(
            "move_to",
            {"pos": [int(target[0]), int(target[1]) + 1, int(target[2])], "range": 3},
            cancel_event=getattr(self, "_cancel", None),
        )

    def _enter_lit_portal(self):
        find_fn = getattr(self.client, "find_blocks", None) or getattr(self.client, "get_blocks", None)
        portal_pts = []
        if callable(find_fn):
            try:
                rows = find_fn(types=["nether_portal"], radius=24, limit=16) or []
                portal_pts = [
                    row["pos"] for row in rows
                    if isinstance(row, dict) and row.get("name") == "nether_portal"
                    and isinstance(row.get("pos"), (list, tuple)) and len(row["pos"]) == 3
                ]
            except Exception:
                portal_pts = []
        if not portal_pts:
            return None
        portal_pts.sort(key=lambda p: p[1])
        target = portal_pts[0]
        from skills.survival import SurvivalSkills
        mv = SurvivalSkills(self.client).execute(
            "move_to",
            {"pos": [float(target[0]) + 0.5, float(target[1]), float(target[2]) + 0.5], "range": 0.6},
            cancel_event=getattr(self, "_cancel", None),
        )
        if (isinstance(mv.data, dict) and mv.data.get("action_state") == "unknown") or mv.error_code == "action_unconfirmed":
            return mv
        deadline = time.monotonic() + 12.0
        cancel = getattr(self, "_cancel", None)
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                break
            try:
                st_raw = self.client.get_status()
                st = st_raw.get("result") if isinstance(st_raw, dict) and "result" in st_raw else st_raw
                if isinstance(st, dict):
                    dim = st.get("dimension")
                    if dim in {"the_nether", "minecraft:the_nether"}:
                        return SkillResult(
                            True,
                            "Entered the_nether through verified Nether Portal!",
                            {"portal_active": True, "nether_entered": True, "dimension": dim, "executed": True},
                        )
                    if st.get("alive") is False:
                        break
            except Exception:
                pass
            time.sleep(0.4)
        return SkillResult(
            True,
            "Standing inside active Nether Portal; awaiting dimension transition",
            {"portal_active": True, "nether_entered": False, "portal_skill": True, "executed": True},
        )

    def _build_obsidian_portal_on_land(self, observation):
        counts = (observation.get("counts") or {}) if isinstance(observation, dict) else {}
        if counts.get("obsidian", 0) < 10 or counts.get("flint_and_steel", 0) < 1:
            return None
        from core.structure_spec import NETHER_PORTAL_SPEC, find_structure_sites, transform_rel_pos
        from skills.portal_speedrun import NetherPortalSpeedrunSkill
        status = (observation.get("status") or {}) if isinstance(observation, dict) else {}
        bot_pos = status.get("pos")
        if not isinstance(bot_pos, (list, tuple)) or len(bot_pos) != 3:
            return None
        sites = find_structure_sites(
            NETHER_PORTAL_SPEC,
            [float(v) for v in bot_pos],
            lambda pos: self.client.get_block(pos[0], pos[1], pos[2]),
            radius=12,
            max_results=4,
        )
        bx, by, bz = int(math.floor(bot_pos[0])), int(math.floor(bot_pos[1])), int(math.floor(bot_pos[2]))
        candidates = sites or [{"origin": [bx + 2, by, bz], "orientation": "north"}]
        skill = NetherPortalSpeedrunSkill(self.client)
        for site in candidates:
            origin = site["origin"]
            orient = site.get("orientation", "north")
            frame_rels = sorted(
                {c.rel_pos for c in NETHER_PORTAL_SPEC.cells if c.stage in {"base", "frame"} and "obsidian" in c.accepted},
                key=lambda r: (r[1], r[0], r[2]),
            )
            ok_all = True
            for rel in frame_rels:
                wp = transform_rel_pos(rel, origin, orient, integer=True)
                try:
                    cur = (self.client.get_block(wp[0], wp[1], wp[2]).get("result") or {}).get("name")
                    if cur == "obsidian":
                        continue
                    res = self.client.place_block("obsidian", wp[0], wp[1], wp[2])
                    if not isinstance(res, dict) or not res.get("ok"):
                        ok_all = False
                        break
                except Exception:
                    ok_all = False
                    break
            if ok_all:
                base_wp = transform_rel_pos((1, -1, 0), origin, orient, integer=True)
                try:
                    self.client.equip("flint_and_steel", slot="hand")
                    self.client.interact(pos=base_wp, item="flint_and_steel", face=[0, 1, 0])
                    time.sleep(0.3)
                    rep = skill.inspect_portal(origin, orientation=orient, include_scaffold=False)
                    if rep.get("portal_active"):
                        return SkillResult(True, "Built and ignited Nether Portal using held obsidian!", {"portal_active": True, "structure_verified": True, "executed": True})
                except Exception:
                    pass
        return None

    def _run_task_nether_portal(self, decision, observation, window):
        from core.memory import MemoryEngine
        from skills.portal_speedrun import NetherPortalSpeedrunSkill
        from skills.scouting import SurfaceScoutSkill
        from skills.survival import SurvivalSkills

        entered_existing = self._enter_lit_portal()
        if entered_existing is not None:
            entered_existing.data["portal_skill"] = True
            return entered_existing

        skill = NetherPortalSpeedrunSkill(self.client)
        res = skill.build_portal(timeout_seconds=min(210, window.remaining()), allow_damage=True, cancel_event=window)
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        if (not res.success and res.error_code == "no_portal_site_found"
                and data.get("action_state") != "unknown" and window.remaining() > 1 and not window.is_set()):
            obsidian_try = self._build_obsidian_portal_on_land(observation)
            if obsidian_try is not None and obsidian_try.success:
                res = obsidian_try
                data = dict(res.data or {})
            else:
                origin = observation["status"]["pos"]
                guard = WorkflowGuard(self.client, cancel_event=window, timeout_seconds=window.remaining())
                rows = guard.read(self.client.find_blocks, types=["lava"], radius=64, limit=64)
                pts = [row["pos"] for row in extract_blocks({"blocks": rows}) if row["name"] == "lava"]
                approached = False
                if pts:
                    nearest = min(pts, key=lambda pos: math.dist(origin, pos))
                    if math.dist(origin, nearest) > 12:
                        x, y, z = map(math.floor, nearest)
                        moved = SurvivalSkills(self.client).execute(
                            "move_to", {"pos": [x, y + 1, z], "range": 5}, cancel_event=window)
                        if not moved.success:
                            return moved
                        approached = True
                        guard.check_budget()
                        res = skill.build_portal(timeout_seconds=min(180, window.remaining()), allow_damage=True,
                                                 cancel_event=window)
                        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
                        data["executed"] = True
                if not res.success and not approached and window.remaining() > 25:
                    memory = MemoryEngine(str(self.journal.directory / "scout_memory"))
                    attempt = int(self.context.get("lava_scout_attempts", 0))
                    self.context["lava_scout_attempts"] = attempt + 1
                    scout_res = SurfaceScoutSkill(self.client, memory=memory).execute(
                        scan_radius=64,
                        hop_distance=56.0,
                        max_hops=3,
                        prefer_wood=False,
                        goal_focus="lava",
                        attempt=attempt,
                        timeout_seconds=min(70.0, window.remaining() - 5.0),
                        cancel_event=window,
                    )
                    sdata = dict(scout_res.data or {}) if isinstance(scout_res.data, dict) else {}
                    if scout_res.success:
                        self.context["last_scout_report"] = copy.deepcopy(sdata)
                    lava_pts = [r["pos"] for r in (sdata.get("found_lava") or []) if isinstance(r, dict) and isinstance(r.get("pos"), list)]
                    if lava_pts and window.remaining() > 15:
                        best = lava_pts[0]
                        mv = SurvivalSkills(self.client).execute(
                            "move_to", {"pos": [int(best[0]), int(best[1]) + 1, int(best[2])], "range": 5}, cancel_event=window)
                        if not mv.success:
                            return mv
                        res = skill.build_portal(timeout_seconds=min(180, window.remaining()), allow_damage=True,
                                                 cancel_event=window)
                        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        data["portal_skill"] = True
        data.setdefault("executed", bool(res.success or data.get("writes", 0) > 0))
        if res.success:
            data.setdefault("portal_active", True)
            entered = self._enter_lit_portal()
            if entered is not None:
                data.update({k: v for k, v in (entered.data or {}).items() if k in ("nether_entered", "dimension", "portal_active")})
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _task_next(self, observation, *, relaxed_policy=None):
        if not self._task_active():
            return None
        if self.mainline_enabled and not self.journal.state.get("pending"):
            task = self._task_snapshot()
            view = self.context.get("mainline") or {}
            previous_stage = self.context.get("mainline_task_stage")
            chapter_changed = previous_stage is not None and previous_stage != view.get("stage")
            if (view.get("fresh") and (self.context.get("mainline_task_id") != task["task_id"] or chapter_changed)
                    and not (task.get("starter_mine") or {}).get("site")):
                if not same_goal(task.get("args"), view.get("goal")):
                    self.task_controller.stop("Fresh evidence selects another chapter; replan at this confirmed boundary",
                                              cancelled=True)
                    self._save_task("TASK_CANCELLED")
                    self._finish_task_feedback(observation)
                    return Decision("wait", {"seconds": .25}, "Continue from the current mainline chapter", "mainline")
                self.context["mainline_task_id"] = task["task_id"]
                self.context["mainline_task_stage"] = view["stage"]
        recovered = None
        if self.continuous and not self.journal.state.get("pending"):
            old = self.context.get("last_abandoned_action") or {}
            if old.get("name") == "step_starter_staircase" and old.get("generation") == self.context["life_generation"]:
                if "mine_landmark" not in old:
                    old["mine_landmark"] = recover_interrupted_mine(self.journal, old)
                if old["mine_landmark"] and self.task_controller.restore_interrupted_mine(observation, self.context, old):
                    recovered = old
        task_ctx = {**self.context, "deep_mining_enabled": self.settings.get("disable_deep_mining") is not True}
        args = self._task_snapshot()["args"]
        counts = observation.get("counts") or {}
        material = wood_material(args.get("resource") or args.get("item"))
        log_names = (material[0] + "_log",) if material is not None else LOG_NAMES
        plank_names = (material[0] + "_planks",) if material is not None else PLANK_NAMES
        gathering_wood = args.get("task") == "gather" and args.get("resource") in {"logs", *LOG_NAMES}
        needs_wood = args.get("task") in {"craft", "starter_tools", "shelter", "storage", "bed"} and not any(
            counts.get(name) for name in (*log_names, *plank_names))
        if (relaxed_policy is None and (gathering_wood or needs_wood)
                and self.task_controller._fresh(observation)
                and not task_ctx.get("cautious_bootstrap")):
            task_ctx["remote_log_target"] = self._remote_log_waypoint(
                observation, log_names, blocked=self.task_controller.state().get("blocked"))
        decision = self.task_controller.next_action(observation, task_ctx, relaxed_policy=relaxed_policy)
        if recovered and self._task_snapshot()["status"] in {"running", "suspended", "completed"}:
            self._record("INFO", "STARTER_MINE_LANDMARK_RECOVERED", {"pos": observation["status"]["pos"],
                         "site": recovered["mine_landmark"]["site"], "original_action_confirmed": False})
            recovered["mine_landmark"] = None
        if decision is None:
            status = self._task_snapshot().get("status")
            event = {"completed": "TASK_COMPLETED", "needs_ai": "TASK_NEEDS_AI",
                     "unknown": "TASK_UNKNOWN"}.get(status, "TASK_PROGRESS")
            self._save_task(event)
            self._finish_task_feedback(observation)
            if relaxed_policy is not None and status == "suspended":
                return None  # Fall back to the original safety hold.
            return Decision("wait", {"seconds": .25},
                            "Goal boundary reached; next planning uses the recorded task report", "autonomous_task")
        self._task_child = True
        stats = self.context["autonomy_stats"]
        stats["model_calls_avoided"] = stats.get("model_calls_avoided", 0) + 1
        self._save_task("TASK_PROGRESS")
        return decision

    def _task_interrupt(self, reason):
        if not self._task_active():
            return
        self._task_safety = True
        before = self.task_controller.snapshot()
        self.task_controller.interrupt(reason)
        # Repeated night waits need not publish an identical interruption.
        after = self.task_controller.snapshot()
        self._save_task("TASK_SUSPENDED" if before.get("status") != after.get("status")
                        or before.get("reason") != after.get("reason") else None)

    def _task_result(self, decision, result, observation, *, uncertain=False, fatal=False):
        """Return whether a known task failure uses bounded task recovery."""
        child, self._task_child = self._task_child, False
        safety, self._task_safety = self._task_safety, False
        if self.task_controller is None:
            return False
        if child:
            self.task_controller.record_result(decision, result, observation, context=self.context)
            if not uncertain and not fatal:
                self.task_controller.confirm_completion(observation, self.context)
                if (result.data.get("relaxed_policy") and not result.success and result.error_code != "cancelled"
                        and self._task_active()):
                    self.task_controller.stop("relaxed_work_failed_replan")
        if uncertain and (child or safety or self._task_active()):
            self.task_controller.stop(result.error_code or "unknown_action", unknown=True)
            result.data.setdefault("cleanup_skipped", "task_unknown")
        elif fatal and (child or self._task_active()):
            self.task_controller.stop(result.error_code or "fatal_action")
        if child or safety or uncertain or fatal:
            status = self.task_controller.snapshot().get("status")
            event = {"completed": "TASK_COMPLETED", "needs_ai": "TASK_NEEDS_AI",
                     "unknown": "TASK_UNKNOWN", "suspended": "TASK_SUSPENDED"}.get(status, "TASK_PROGRESS")
            self._save_task(event)
        self._finish_task_feedback(observation)
        return bool(child and not uncertain and not fatal and self.task_controller.snapshot().get("status") in {
            "running", "suspended", "completed", "needs_ai"})

    def _finish_task_feedback(self, observation=None, *, paused=False):
        if self._task_model_decision is None or self.task_controller is None:
            return
        snapshot = self._task_snapshot()
        status = snapshot.get("status")
        if status not in {"completed", "needs_ai", "unknown", "cancelled"} and not paused:
            return
        data = {"task_execution": snapshot, "completed": status == "completed",
                "executed": bool(snapshot.get("steps", 0)), "partial": status != "completed"}
        if snapshot.get("task") == "scout" and isinstance(self.context.get("last_scout_report"), dict):
            data["scout_report"] = copy.deepcopy(self.context["last_scout_report"])
        code = None if status == "completed" else "action_unconfirmed" if status == "unknown" else "task_paused" if paused else "task_needs_ai"
        if status == "unknown":
            data.update(action_state="unknown", cleanup_skipped="task_unknown")
        result = SkillResult(status == "completed", snapshot.get("reason") or
                             ("Goal verified from current observations" if status == "completed"
                              else "Local goal execution needs review"), data, code)
        decision, self._task_model_decision = self._task_model_decision, None
        try:
            self.planner.observe_result(decision, result, observation or {})
        except LLMError as error:
            self._paused = True
            self.pause_reason = error.code
            self._record("ERROR", "LLM_FEEDBACK_FAILED", {"code": error.code, "message": str(error)})
        self._record("INFO", "TASK_FEEDBACK", {"decision": asdict(decision), "result": asdict(result)})

    def _task_reconcile(self, observation):
        """An explicit resume abandons an uncertain child, never repeats it."""
        if not self._task_resume_requested or self.task_controller is None:
            return None
        if observation.get("fresh") is not True or observation.get("observation_scope") == "safety":
            return None
        status = observation.get("status", {})
        if status.get("ready") is not True or status.get("alive") is not True or status.get("busy"):
            return None
        state = self._task_snapshot().get("status")
        if state == "unknown" or self.journal.state.get("pending") and state in {"running", "suspended"}:
            if not self.task_controller.abandon_after_reobserve(observation):
                return None
            self._task_resume_requested = False
            self._save_task("TASK_NEEDS_AI")
            self._finish_task_feedback(observation)
            return SkillResult(True, "Fresh state observed; interrupted goal requires a new plan", {
                "executed": False, "reobserved": True, "task_abandoned": True})
        self._task_resume_requested = False
        return None

    def _task_end_at_boundary(self, reason):
        """Called only with no in-flight child; unknown evidence stays unknown."""
        if self._task_active():
            complete = (reason == "global_budget_exhausted"
                        and self.task_controller.confirm_completion(self.last_observation, self.context))
            if not complete:
                self.task_controller.stop(reason)
            self._save_task("TASK_COMPLETED" if complete else "TASK_NEEDS_AI")
        self._finish_task_feedback(self.last_observation)

    def _local_task_food(self, observation):
        """Choose a currently proven emergency food action without a model."""
        if not self._task_active() or not self.daily_policy:
            return None
        constraints = self.daily_policy.constraints(observation, context=self.context)
        if constraints.get("scenario") not in {"low_health", "critical_hunger"}:
            return None
        options = self.daily_policy._food_options(observation,
            low_health=constraints["scenario"] == "low_health", task_board=self.daily_board)
        for decision in options:
            if self.daily_policy.validate(decision, observation, context=self.context) is None:
                return decision
        return None
