"""Offline native model protocol, validation, feedback and transport regressions."""

import copy
import json
import ssl
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from agent.brain import LLMError, OpenAIBrain, _TLS12Adapter
from agent.llm_planner import LLMSurvivalPlanner
from agent.tools import TOOL_DEFINITIONS
from skills import SkillResult


def observation(counts=None, **changes):
    return {"fresh": True, "status": {"ready": True, "alive": True, "busy": None, "gamemode": "survival",
            "dimension": "overworld", "pos": [0.5, 64, 0.5], "health": 20, "food": 18,
            "is_day": True, "thundering": False, "on_ground": True, **changes},
            "counts": counts or {}, "inventory": [], "entities": [],
            "blocks": [{"name": "oak_log", "pos": [3, 64, 0]}, {"name": "stone", "pos": [5, 64, 0]}]}


def response(name="wait", args=None, call_id="call_1", **message_extra):
    return {"id": "completion_1", "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args or {})}}], **message_extra}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 12}}


class Brain:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def chat_completion(self, messages, tools=None):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value(messages) if callable(value) else copy.deepcopy(value)


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, *args):
        self.events.append(copy.deepcopy(args))


class LLMPlannerTests(unittest.TestCase):
    def planner(self, *responses, **settings):
        self.brain = Brain(*responses)
        self.recorder = Recorder()
        return LLMSurvivalPlanner({"enabled": True, "api_key": "test-secret-never-log",
                                  "model": "test-model", **settings}, {}, self.recorder, self.brain)

    def test_only_nine_implemented_tools_are_registered(self):
        self.assertEqual({tool["function"]["name"] for tool in TOOL_DEFINITIONS},
                         {"gather_block", "craft_item", "place_item", "eat_food", "move_to",
                          "retreat_from", "auto_sleep", "wait", "goal_complete"})
        self.assertTrue(all(tool["function"]["parameters"]["additionalProperties"] is False for tool in TOOL_DEFINITIONS))

    def test_reason_is_removed_from_skill_arguments_and_actual_result_is_paired(self):
        planner = self.planner(response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log", "reason": "Get wood"}),
                               response("craft_item", {"item": "oak_planks", "count": 4}, "call_2"))
        decision = planner.choose(observation(), {})
        self.assertEqual(decision.reason, "Get wood")
        self.assertNotIn("reason", decision.args)
        after = observation({"oak_log": 1})
        after["blocks"] = []
        planner.observe_result(decision, SkillResult(True, "Verified wood gain", {"gained": {"oak_log": 1}}), after)
        next_decision = planner.choose(after, {})
        self.assertEqual(next_decision.name, "craft_item")
        messages = self.brain.requests[-1]["messages"]
        tool_result = next(message for message in messages if message["role"] == "tool")
        self.assertEqual(tool_result["tool_call_id"], "call_1")
        self.assertEqual(json.loads(tool_result["content"])["result"]["data"]["gained"], {"oak_log": 1})
        self.assertEqual(json.loads(messages[-1]["content"])["observation"]["blocks"], [])

    def test_no_second_request_until_previous_action_feedback(self):
        planner = self.planner(response())
        planner.choose(observation(), {})
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {})
        self.assertEqual(caught.exception.code, "llm_missing_feedback")
        self.assertEqual(len(self.brain.requests), 1)

    def test_schema_rejects_invalid_values_without_sending_any_skill_action(self):
        cases = [("craft_item", {"item": "stick", "count": True}),
                 ("craft_item", {"item": "stick", "count": 65}),
                 ("craft_item", {"item": "stick", "extra": 1}),
                 ("move_to", {"pos": [1, 64, 0], "range": float("nan")}),
                 ("gather_block", {"pos": [3.5, 64, 0], "expected_name": "oak_log"}),
                 ("gather_block", {"pos": [3, 64, 0], "expected_name": "diamond_ore"}),
                 ("wait", {"reason": 12}), ("pillar_up", {"height": 3})]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                planner = self.planner(response(name, args), max_repair_attempts=0)
                with self.assertRaises(LLMError) as caught:
                    planner.choose(observation(), {})
                self.assertEqual(caught.exception.code, "llm_invalid_arguments")
                self.assertIsNone(planner._pending)
                self.assertEqual(len(self.brain.requests), 1)

    def test_duplicate_json_keys_are_rejected(self):
        reply = response("wait")
        reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"seconds":1,"seconds":2}'
        planner = self.planner(reply, max_repair_attempts=0)
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {})
        self.assertEqual(caught.exception.code, "llm_invalid_arguments")

    def test_malformed_function_name_can_be_repaired(self):
        reply = response()
        reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = {"unexpected": "object"}
        planner = self.planner(reply, response())
        self.assertEqual(planner.choose(observation(), {}).name, "wait")
        self.assertEqual(len(self.brain.requests), 2)

    def test_invalid_call_gets_one_bounded_repair_with_rejection_result(self):
        planner = self.planner(response("craft_item", {"item": "stick", "count": 0}), response("wait", {"seconds": 1}, "repair"))
        self.assertEqual(planner.choose(observation(), {}).name, "wait")
        repair_messages = self.brain.requests[1]["messages"]
        feedback = next(message for message in repair_messages if message["role"] == "tool")
        self.assertEqual(feedback["tool_call_id"], "call_1")
        self.assertFalse(json.loads(feedback["content"])["executed"])
        self.assertTrue(any(event[2] == "LLM_REPAIR" for event in self.recorder.events))

    def test_multiple_tools_are_all_rejected_and_paired_before_repair(self):
        reply = response("wait")
        reply["choices"][0]["message"]["tool_calls"] += response("wait", call_id="call_2")["choices"][0]["message"]["tool_calls"]
        planner = self.planner(reply, response())
        planner.choose(observation(), {})
        results = [m for m in self.brain.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["call_1", "call_2"])
        self.assertTrue(all(json.loads(m["content"])["error_code"] == "llm_multiple_tool_calls" for m in results))

    def test_text_only_response_never_becomes_an_action_and_repair_is_bounded(self):
        reply = response(tool_calls=[], content='{"name":"gather_block"}')
        planner = self.planner(reply, reply)
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {})
        self.assertEqual(caught.exception.code, "llm_no_tool_call")
        self.assertEqual(len(self.brain.requests), 2)
        self.assertIsNone(planner._pending)

    def test_latest_observation_limits_target_items_and_completion_claims(self):
        cases = [("gather_block", {"pos": [4, 64, 0], "expected_name": "oak_log"}),
                 ("eat_food", {"item": "bread"}), ("place_item", {"item": "crafting_table", "pos": [2, 64, 0]}),
                 ("move_to", {"pos": [40, 64, 0]}), ("retreat_from", {"pos": [2, 64, 0]}),
                 ("goal_complete", {}), ("auto_sleep", {})]
        for name, args in cases:
            with self.subTest(name=name):
                planner = self.planner(response(name, args), max_repair_attempts=0)
                with self.assertRaises(LLMError) as caught:
                    planner.choose(observation(), {})
                self.assertEqual(caught.exception.code, "llm_unsafe_action")

    def test_unobserved_workspace_can_be_proposed_only_with_real_inventory(self):
        planner = self.planner(response("place_item", {"item": "crafting_table", "pos": [2, 64, 0]}))
        self.assertEqual(planner.choose(observation({"crafting_table": 1}), {}).args["pos"], [2, 64, 0])

    def test_verified_completion_delegates_to_current_progress(self):
        planner = self.planner(response("goal_complete"))
        obs = observation({"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "furnace": 1})
        self.assertTrue(planner.progress(obs)["starter_goal_complete"])
        self.assertEqual(planner.choose(obs, {}).goal, "starter_goal")

    def test_moves_cannot_bypass_exploration_or_target_cooldown(self):
        for context in ({"explore_index": 8}, {"blocked_targets": ["10,64,0"]}):
            planner = self.planner(response("move_to", {"pos": [10, 64, 0]}), max_repair_attempts=0)
            with self.assertRaises(LLMError) as caught:
                planner.choose(observation(), context)
            self.assertEqual(caught.exception.code, "llm_unsafe_action")

    def test_approaching_observed_workstation_is_not_exploration(self):
        planner = self.planner(response("move_to", {"pos": [10, 64, 0]}))
        obs = observation()
        obs["blocks"].append({"name": "crafting_table", "pos": [10, 64, 0]})
        self.assertEqual(planner.choose(obs, {"explore_index": 8}).goal, "crafting_table")

    def test_repeated_failure_requires_a_new_plan_in_unchanged_state(self):
        args = {"pos": [3, 64, 0], "expected_name": "oak_log"}
        planner = self.planner(response("gather_block", args), response("gather_block", args), response("wait"))
        obs = observation()
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(False, "No inventory gain", error_code="gather_unverified"), obs)
        self.assertEqual(planner.choose(obs, {}).name, "wait")
        self.assertTrue(any(event[2] == "LLM_REPAIR" and event[3]["code"] == "llm_repeated_failure" for event in self.recorder.events))
        request = json.dumps(self.brain.requests[1])
        self.assertIn("gather_unverified", request)

    def test_failure_can_be_reconsidered_after_observed_world_change(self):
        args = {"pos": [3, 64, 0], "expected_name": "oak_log"}
        planner = self.planner(response("gather_block", args), response("gather_block", args))
        obs = observation()
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(False, "Approach failed", error_code="out_of_reach"), obs)
        changed = observation(pos=[1.5, 64, 0.5])
        self.assertEqual(planner.choose(changed, {}).name, "gather_block")

    def test_history_discards_whole_turns_never_unpaired_tools(self):
        planner = self.planner(*(response(call_id=f"call_{i}") for i in range(4)), max_history_turns=2)
        for _ in range(3):
            decision = planner.choose(observation(), {})
            planner.observe_result(decision, SkillResult(True, "Waited"), observation())
        self.assertEqual(len(planner.history), 6)
        planner.choose(observation(), {})
        messages = self.brain.requests[-1]["messages"]
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["call_1", "call_2"])
        self.assertEqual([call["id"] for m in messages if m["role"] == "assistant" for call in m["tool_calls"]], ["call_1", "call_2"])

    def test_cancel_before_request_and_after_late_response_never_dispatches(self):
        cancel = threading.Event()
        cancel.set()
        planner = self.planner(response())
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {}, cancel)
        self.assertEqual(caught.exception.code, "llm_cancelled")
        self.assertFalse(self.brain.requests)
        cancel.clear()

        def delayed(messages):
            cancel.set()
            return response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"})

        planner = self.planner(delayed)
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {}, cancel)
        self.assertEqual(caught.exception.code, "llm_cancelled")
        self.assertIsNone(planner._pending)

    def test_model_error_is_explicit_without_repair_or_rule_fallback(self):
        planner = self.planner(LLMError("llm_timeout", "Model request timed out"))
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {})
        self.assertEqual(caught.exception.code, "llm_timeout")
        self.assertEqual(len(self.brain.requests), 1)
        self.assertEqual(self.recorder.events[-1][2], "LLM_ERROR")

    def test_http_status_details_are_recorded_for_harness_retry_policy(self):
        planner = self.planner(LLMError("llm_http_error", "Model endpoint returned HTTP 502", {"http_status": 502}))
        with self.assertRaises(LLMError):
            planner.choose(observation(), {})
        self.assertEqual(self.recorder.events[-1][3]["details"], {"http_status": 502})
        self.assertEqual(len(self.brain.requests), 1)

    def test_public_response_fields_are_recorded_and_secrets_redacted(self):
        secret = "test-secret-never-log"
        planner = self.planner(response(content=f"Wait {secret}", reasoning_content=f"Public model field {secret}"))
        obs = observation()
        obs["status"]["token"] = "private-status-token"
        decision = planner.choose(obs, {})
        self.assertNotIn(secret, decision.reason)
        raw = json.dumps(self.recorder.events)
        self.assertNotIn(secret, raw)
        self.assertNotIn("private-status-token", raw)
        data = next(e[3]["response"] for e in self.recorder.events if e[2] == "LLM_RESPONSE")
        self.assertEqual(data["usage"]["completion_tokens"], 12)
        self.assertIn("reasoning_content", data["choices"][0]["message"])
        self.assertEqual(data["choices"][0]["finish_reason"], "tool_calls")
        self.assertNotIn("think step by step", self.brain.requests[0]["messages"][0]["content"].lower())

    def test_stale_observation_never_calls_model(self):
        planner = self.planner()
        obs = observation()
        obs["stale"] = True
        with self.assertRaises(LLMError) as caught:
            planner.choose(obs, {})
        self.assertEqual(caught.exception.code, "llm_unsafe_action")
        self.assertFalse(self.brain.requests)

    def test_terrain_summary_is_available_without_claiming_complete_placement_map(self):
        planner = self.planner(response())
        obs = observation()
        obs["terrain"] = [{"name": "grass_block", "pos": [1, 63, 0], "dist": 1.2, "count": 10}]
        planner.choose(obs, {})
        messages = self.brain.requests[0]["messages"]
        self.assertEqual(json.loads(messages[-1]["content"])["observation"]["terrain"], obs["terrain"])
        self.assertIn("nearest-per-type summary", messages[0]["content"])
        self.assertIn("Placement skills", messages[0]["content"])

    def test_invalid_feedback_cannot_orphan_or_close_the_pending_call(self):
        planner = self.planner(response())
        decision = planner.choose(observation(), {})
        with self.assertRaises(LLMError) as caught:
            planner.observe_result(decision, SkillResult(True, "Invalid", {"value": float("nan")}), observation())
        self.assertEqual(caught.exception.code, "llm_invalid_feedback")
        self.assertIsNotNone(planner._pending)
        self.assertFalse(planner.history)

    def test_large_observations_and_history_fit_wire_budget_with_complete_tool_pairs(self):
        planner = self.planner(*(response(call_id=f"bounded_{i}") for i in range(30)), max_history_turns=100)
        obs = observation()
        obs["blocks"] = [{"name": "oak_log" if i < 64 else "stone", "pos": [i % 32, 64, i // 32],
                          "displayName": "unused metadata " * 500, "diggable": True}
                         for i in range(128)]
        obs["blocks"].extend([{"name": "lava", "pos": [30, 64, 30]},
                              {"name": "crafting_table", "pos": [20, 64, 20]}])
        obs["entities"] = [{"name": "sheep", "type": "passive", "pos": [i, 64, 1],
                            "raw_metadata": "unused metadata " * 500} for i in range(60)]
        obs["entities"].append({"name": "zombie", "type": "hostile", "pos": [25, 64, 25]})
        for _ in range(30):
            decision = planner.choose(obs, {})
            planner.observe_result(decision, SkillResult(True, "Waited"), obs)
        self.assertTrue(any(event[2] == "LLM_HISTORY_COMPACTED" for event in self.recorder.events))
        for request in self.brain.requests:
            messages = request["messages"]
            self.assertLessEqual(planner._request_bytes(messages), planner.MAX_REQUEST_BYTES)
            self.assertNotIn("unused metadata", json.dumps(messages))
            pending = set()
            for message in messages:
                if message["role"] == "assistant":
                    self.assertFalse(pending)
                    pending.update(call["id"] for call in message.get("tool_calls", []))
                elif message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], pending)
                    pending.remove(message["tool_call_id"])
                    self.assertNotIn("blocks", json.loads(message["content"])["observation"])
            self.assertFalse(pending)
        latest = json.loads(self.brain.requests[-1]["messages"][-1]["content"])["observation"]
        self.assertLessEqual(len(latest["blocks"]), 32)
        self.assertLessEqual(len(latest["entities"]), 12)
        self.assertIn("lava", {row["name"] for row in latest["blocks"]})
        self.assertIn("crafting_table", {row["name"] for row in latest["blocks"]})
        self.assertEqual(latest["entities"][0]["type"], "hostile")
        self.assertFalse(latest["candidate_summary"]["complete_voxel_map"])

    def test_transport_failures_do_not_accumulate_unanswered_user_snapshots(self):
        planner = self.planner(*[LLMError("llm_timeout", "Timeout") for _ in range(7)], response())
        for _ in range(7):
            with self.assertRaises(LLMError):
                planner.choose(observation(), {})
            self.assertEqual(planner.history, [])
        self.assertEqual(planner.choose(observation(), {}).name, "wait")
        self.assertTrue(all([m["role"] for m in request["messages"]] == ["system", "user"]
                            for request in self.brain.requests))

    def test_oversized_current_context_is_rejected_before_network(self):
        planner = self.planner(response())
        planner.MAX_REQUEST_BYTES = 100
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {})
        self.assertEqual(caught.exception.code, "llm_context_limit")
        self.assertFalse(self.brain.requests)

    def test_successful_wait_and_hunger_change_do_not_erase_failed_target(self):
        args = {"pos": [3, 64, 0], "expected_name": "oak_log"}
        planner = self.planner(response("gather_block", args), response("wait", call_id="wait_2"),
                               response("gather_block", args, "retry_3"), response("wait", call_id="repair_4"))
        obs = observation()
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(False, "Stalled", error_code="stalled"), obs)
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(True, "Waited"), obs)
        self.assertEqual(planner.choose(observation(food=17), {}).name, "wait")
        self.assertTrue(any(e[2] == "LLM_REPAIR" and e[3]["code"] == "llm_repeated_failure" for e in self.recorder.events))
        prompt = json.loads(self.brain.requests[2]["messages"][-1]["content"])
        self.assertEqual(prompt["context"]["failed_actions"][0]["error_code"], "stalled")

    def test_failed_move_does_not_forbid_direct_gather_at_the_same_resource(self):
        planner = self.planner(response("move_to", {"pos": [5, 64, 0]}),
                               response("gather_block", {"pos": [5, 64, 0], "expected_name": "stone"}, "direct_2"))
        obs = observation({"wooden_pickaxe": 1})
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(False, "Cannot path into block", error_code="stalled"), obs)
        self.assertEqual(planner.choose(obs, {}).name, "gather_block")

    def test_move_already_inside_tolerance_repairs_to_direct_resource_action(self):
        planner = self.planner(response("move_to", {"pos": [1, 64, 0], "range": 2}),
                               response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}, "direct_2"))
        self.assertEqual(planner.choose(observation(), {}).name, "gather_block")
        self.assertTrue(any(e[2] == "LLM_REPAIR" and e[3]["code"] == "llm_no_progress" for e in self.recorder.events))

    def test_successful_move_without_displacement_is_not_recovery_evidence(self):
        args = {"pos": [10, 64, 0]}
        planner = self.planner(response("move_to", args), response("wait", call_id="wait_2"),
                               response("move_to", {**args, "range": 1}, "retry_3"), response("wait", call_id="repair_4"))
        obs = observation()
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(True, "Already in range", {"moved": False, "already_in_range": True}), obs)
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(True, "Waited"), obs)
        self.assertEqual(planner.choose(obs, {}).name, "wait")
        self.assertIn("no_movement", json.dumps(self.brain.requests[-1]))

    def test_checked_local_geometry_candidates_survive_model_summary(self):
        planner = self.planner(response())
        obs = observation()
        obs["safe_stand_candidates"] = [{"pos": [x + 0.5, 64, 2.5], "support": "grass_block"} for x in range(10)]
        obs["placement_candidates"] = [{"pos": [x, 64, 2], "support": "dirt"} for x in range(10)]
        obs["local_geometry_origin"] = obs["status"]["pos"]
        planner.choose(obs, {})
        snapshot = json.loads(self.brain.requests[0]["messages"][-1]["content"])["observation"]
        self.assertEqual(snapshot["safe_stand_candidates"], obs["safe_stand_candidates"][:8])
        self.assertEqual(snapshot["placement_candidates"], obs["placement_candidates"][:8])
        self.assertEqual(snapshot["local_geometry_origin"], obs["status"]["pos"])

    def test_resource_summary_prioritizes_visibility_and_reach_without_dropping_unknowns(self):
        planner = self.planner(response())
        obs = observation()
        obs["blocks"] = [
            {"name": "oak_log", "pos": [1, 64, 0], "can_dig": True, "can_see": False},
            {"name": "oak_log", "pos": [2, 64, 0]},
            {"name": "oak_log", "pos": [5, 64, 0], "can_dig": False, "can_see": True},
            {"name": "oak_log", "pos": [4, 64, 0], "can_dig": True, "can_see": True},
        ]
        planner.choose(obs, {})
        rows = json.loads(self.brain.requests[0]["messages"][-1]["content"])["observation"]["blocks"]
        self.assertEqual([row["pos"][0] for row in rows], [4, 5, 2, 1])
        self.assertTrue(rows[0]["can_dig"])
        self.assertTrue(rows[0]["can_see"])
        self.assertNotIn("can_see", rows[2])
        self.assertFalse(rows[-1]["can_see"])


class BrainTransportTests(unittest.TestCase):
    @patch("agent.brain.requests.post")
    def test_context_limit_error_exposes_only_status_and_numeric_counts(self, post):
        post.return_value = Mock(status_code=400, json=Mock(return_value={"error": {
            "type": "exceed_context_size_error",
            "message": "request (51524 tokens) exceeds the available context size (43776 tokens), SECRET"}}))
        with self.assertRaises(LLMError) as caught:
            OpenAIBrain(api_key="SECRET").chat_completion([])
        self.assertEqual(caught.exception.details, {"http_status": 400, "context_limit": True,
                                                   "request_tokens": 51524, "context_tokens": 43776})
        self.assertNotIn("SECRET", str(caught.exception) + json.dumps(caught.exception.details))
        self.assertEqual(post.call_count, 1)

    def test_llm_error_remains_compatible_with_two_arguments(self):
        error = LLMError("llm_timeout", "Model request timed out")
        self.assertEqual(error.details, {})

    @patch("agent.brain.requests.post")
    def test_http_status_metadata_is_preserved_without_sensitive_headers(self, post):
        for status in (401, 403, 429, 502, 503):
            with self.subTest(status=status):
                post.reset_mock()
                post.return_value = Mock(status_code=status, headers={"Authorization": "SECRET", "x-oneapi-request-id": "SECRET"}, text="SECRET")
                with self.assertRaises(LLMError) as caught:
                    OpenAIBrain(api_key="SECRET").chat_completion([])
                self.assertEqual(caught.exception.code, "llm_http_error")
                self.assertEqual(caught.exception.details, {"http_status": status})
                self.assertNotIn("SECRET", str(caught.exception) + json.dumps(caught.exception.details))
                self.assertEqual(post.call_count, 1)

    @patch("agent.brain.requests.post")
    def test_native_payload_preserves_legacy_constructor_and_disables_stream(self, post):
        post.return_value = Mock(status_code=200, json=Mock(return_value=response()))
        brain = OpenAIBrain("https://example.invalid/v1", "test-key", "model", 0.2, 128, timeout_seconds=7)
        self.assertIn("choices", brain.chat_completion([{"role": "user", "content": "Observe"}], TOOL_DEFINITIONS))
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://example.invalid/v1/chat/completions")
        self.assertFalse(kwargs["json"]["stream"])
        self.assertEqual(kwargs["json"]["tool_choice"], "auto")
        self.assertEqual(kwargs["timeout"], 7)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("verify", kwargs)

    @patch("agent.brain.requests.post")
    def test_timeout_network_http_and_json_errors_do_not_expose_body_or_headers(self, post):
        cases = [(requests.Timeout("SECRET"), "llm_timeout"),
                 (requests.ConnectionError("SECRET"), "llm_network_error"),
                 (Mock(status_code=401, text="SECRET"), "llm_http_error"),
                 (Mock(status_code=200, json=Mock(side_effect=ValueError("SECRET"))), "llm_invalid_response")]
        for value, code in cases:
            with self.subTest(code=code):
                post.reset_mock()
                post.side_effect = value if isinstance(value, Exception) else None
                post.return_value = value
                with self.assertRaises(LLMError) as caught:
                    OpenAIBrain(api_key="SECRET").chat_completion([])
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(post.call_count, 1)

    @patch("agent.brain.requests.Session")
    def test_tls12_is_opt_in_and_retains_certificate_checks(self, session_factory):
        session = session_factory.return_value.__enter__.return_value
        session.post.return_value = Mock(status_code=200, json=Mock(return_value=response()))
        OpenAIBrain(tls_version="1.2").chat_completion([])
        adapter = session.mount.call_args.args[1]
        self.assertIsInstance(adapter, _TLS12Adapter)
        self.assertEqual(adapter._tls_context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(adapter._tls_context.maximum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(adapter._tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(adapter._tls_context.check_hostname)
        self.assertNotIn("verify", session.post.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
