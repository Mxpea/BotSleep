"""Offline HTTP contract tests for required native tool calls."""

import copy
import unittest
from unittest.mock import Mock, patch

import requests

from agent.brain import LLMError, OpenAIBrain
from agent.tools import TOOL_DEFINITIONS


class RequiredToolChoiceTests(unittest.TestCase):
    def setUp(self):
        self.post = self.enterContext(patch("agent.brain.requests.post"))
        self.session_factory = self.enterContext(patch("agent.brain.requests.Session"))
        self.completion = {"model": "returned-model", "choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "call_next", "type": "function", "function": {
                    "name": "wait", "arguments": '{"seconds": 1}'}}]}}]}
        self.post.return_value = Mock(status_code=200, json=Mock(return_value=self.completion))
        self.brain = OpenAIBrain(
            "https://offline.invalid/v1", "offline-key", "requested-model",
            0.2, 128, timeout_seconds=7, request_gate=None,
        )
        self.messages = [
            {"role": "system", "content": "Choose a registered tool."},
            {"role": "user", "content": "Observe"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_previous", "type": "function", "function": {
                    "name": "wait", "arguments": '{"seconds": 1}'}}]},
            {"role": "tool", "tool_call_id": "call_previous", "content": '{"ok": true}'},
        ]

    def test_nonempty_tools_require_native_calls_and_preserve_http_json(self):
        original_messages = copy.deepcopy(self.messages)
        original_tools = copy.deepcopy(TOOL_DEFINITIONS)

        result = self.brain.chat_completion(self.messages, TOOL_DEFINITIONS)

        self.assertEqual(result, self.completion)
        self.post.assert_called_once_with(
            "https://offline.invalid/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": "Bearer offline-key"},
            json={"model": "requested-model", "messages": original_messages,
                  "temperature": 0.2, "max_tokens": 128, "stream": False,
                  "tools": original_tools, "tool_choice": "required", "parallel_tool_calls": False},
            timeout=7, allow_redirects=False,
        )
        self.assertEqual(self.messages, original_messages)
        self.assertEqual(TOOL_DEFINITIONS, original_tools)
        self.session_factory.assert_not_called()

    def test_omitted_tools_allow_plain_text_without_tool_choice(self):
        completion = {"choices": [{"message": {"role": "assistant", "content": "Ready"}}]}
        self.post.return_value.json.return_value = completion

        self.assertEqual(self.brain.chat_completion(self.messages), completion)

        self.post.assert_called_once()
        payload = self.post.call_args.kwargs["json"]
        self.assertNotIn("tools", payload)
        self.assertNotIn("parallel_tool_calls", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(payload["messages"], self.messages)

    def test_none_and_empty_tools_omit_tool_fields(self):
        for tools in (None, []):
            with self.subTest(tools=tools):
                self.post.reset_mock()
                self.brain.chat_completion(self.messages, tools)
                self.post.assert_called_once()
                payload = self.post.call_args.kwargs["json"]
                self.assertNotIn("tools", payload)
                self.assertNotIn("tool_choice", payload)
                self.assertEqual(payload["messages"], self.messages)

    def test_failures_do_not_retry_or_fall_back_to_auto(self):
        cases = [
            (requests.Timeout("offline timeout"), "llm_timeout", None),
            (requests.ConnectionError("offline connection error"), "llm_network_error", None),
            (Mock(status_code=400, json=Mock(return_value={"error": {
                "message": "tool_choice required is unsupported"}})), "llm_http_error", 400),
            (Mock(status_code=429), "llm_http_error", 429),
            (Mock(status_code=500), "llm_http_error", 500),
            (Mock(status_code=200, json=Mock(side_effect=ValueError("invalid JSON"))),
             "llm_invalid_response", None),
        ]
        for outcome, code, status in cases:
            with self.subTest(code=code, status=status):
                self.post.reset_mock()
                self.post.side_effect = outcome if isinstance(outcome, Exception) else None
                self.post.return_value = outcome
                with self.assertRaises(LLMError) as caught:
                    self.brain.chat_completion(self.messages, TOOL_DEFINITIONS)
                self.assertEqual(caught.exception.code, code)
                if status is not None:
                    self.assertEqual(caught.exception.details["http_status"], status)
                self.post.assert_called_once()
                payload = self.post.call_args.kwargs["json"]
                self.assertEqual(payload["tool_choice"], "required")
                self.assertEqual(payload["tools"], TOOL_DEFINITIONS)
                self.session_factory.assert_not_called()

    def test_tls12_session_also_requires_tools(self):
        session = self.session_factory.return_value.__enter__.return_value
        session.post.return_value = Mock(status_code=200, json=Mock(return_value=self.completion))
        self.brain.tls_version = "1.2"

        self.assertEqual(self.brain.chat_completion(self.messages, TOOL_DEFINITIONS), self.completion)

        session.post.assert_called_once()
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["tool_choice"], "required")
        self.assertEqual(payload["tools"], TOOL_DEFINITIONS)
        self.assertEqual(payload["messages"], self.messages)
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
