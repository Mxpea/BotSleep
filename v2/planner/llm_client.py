"""L4 OpenAI-compatible LLM client with ModelRequestGate, TLS 1.2 option, primary/overflow dual routing."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import ssl
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

from v2.kernel.model_gate import ModelGateError, ModelRequestGate


class LLMError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


class _TLS12Adapter(HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    requested_model: str
    confirmed_model: str  # response.model or "未确认" per AGENTS.md
    content: str
    tool_calls: List[LLMToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    route: str = "primary"
    latency_ms: int = 0


class OpenAIBrain:
    """OpenAI chat/completions client gated by ModelRequestGate with optional overflow routing."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 60.0,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        tls_version: str = "auto",
        gate: Optional[ModelRequestGate] = None,
        overflow_brain: Optional["OpenAIBrain"] = None,
        primary_wait_seconds: float = 1.5,
        recorder: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.gate = gate or ModelRequestGate(self.base_url, self.api_key, wait_seconds=60.0)
        self.overflow_brain = overflow_brain
        self.primary_wait_seconds = float(primary_wait_seconds)
        self.recorder = recorder
        self._session = requests.Session()
        if tls_version == "1.2":
            self._session.mount("https://", _TLS12Adapter())
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.primary_calls = 0
        self.overflow_calls = 0

    def close(self) -> None:
        self._session.close()
        if self.overflow_brain is not None:
            self.overflow_brain.close()

    @property
    def prompt_completion_ratio(self) -> float:
        if self.total_completion_tokens <= 0:
            return 0.0
        return self.total_prompt_tokens / self.total_completion_tokens

    def _call_single(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        route: str = "primary",
        wait_seconds: Optional[float] = None,
    ) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        started = time.perf_counter()
        with self.gate.request(wait_seconds=wait_seconds):
            try:
                resp = self._session.post(url, headers=headers, json=body, timeout=self.timeout_seconds)
            except requests.Timeout:
                raise LLMError("llm_timeout", "Model HTTP request timed out") from None
            except requests.RequestException:
                raise LLMError("llm_network_error", "Model HTTP request failed") from None

            if not 200 <= resp.status_code < 300:
                raise LLMError("llm_http_error", f"Model HTTP {resp.status_code}", status_code=resp.status_code)
            try:
                data = resp.json()
            except ValueError:
                raise LLMError("llm_bad_response", "Model returned invalid JSON", status_code=resp.status_code) from None

        latency_ms = round((time.perf_counter() - started) * 1000)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("llm_bad_response", "Model returned empty choices")
        msg = choices[0].get("message") or {}
        content = str(msg.get("content") or "")
        confirmed_model = str(data.get("model") or "").strip() or "未确认"
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        pt = int(usage.get("prompt_tokens", 0) or 0)
        ct = int(usage.get("completion_tokens", 0) or 0)
        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct

        parsed_calls: List[LLMToolCall] = []
        for raw_tc in msg.get("tool_calls") or []:
            if not isinstance(raw_tc, dict):
                continue
            fn = raw_tc.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except ValueError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            if name:
                parsed_calls.append(LLMToolCall(id=str(raw_tc.get("id") or f"call_{len(parsed_calls)}"), name=name, arguments=args))

        if self.recorder is not None:
            self.recorder.record(
                "INFO",
                "v2.llm",
                "LLM_HTTP",
                {
                    "route": route,
                    "requested_model": self.model,
                    "confirmed_model": confirmed_model,
                    "latency_ms": latency_ms,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "tool_calls": [tc.name for tc in parsed_calls],
                },
            )
        return LLMResponse(
            requested_model=self.model,
            confirmed_model=confirmed_model,
            content=content,
            tool_calls=parsed_calls,
            prompt_tokens=pt,
            completion_tokens=ct,
            route=route,
            latency_ms=latency_ms,
        )

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> LLMResponse:
        if self.overflow_brain is None:
            resp = self._call_single(messages, tools, route="primary")
            self.primary_calls += 1
            return resp
        try:
            resp = self._call_single(messages, tools, route="primary", wait_seconds=self.primary_wait_seconds)
            self.primary_calls += 1
            return resp
        except ModelGateError as err:
            if err.code != "llm_busy":
                raise
            resp = self.overflow_brain._call_single(messages, tools, route="overflow")
            self.overflow_calls += 1
            self.total_prompt_tokens += resp.prompt_tokens
            self.total_completion_tokens += resp.completion_tokens
            return resp
