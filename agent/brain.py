"""OpenAI-Compatible LLM Brain Interface

Connects to any OpenAI-compatible API endpoint (OpenAI, DeepSeek, Qwen, Ollama, etc.).
"""

from typing import List, Dict, Any, Optional, Tuple
import json
import re
import ssl
import threading
import time
import requests
from requests.adapters import HTTPAdapter

from core.model_gate import ModelGateError, ModelRequestGate, TRANSPORT_WINDOW_SECONDS

_AUTO_GATE = object()
_ORIG_REQUESTS_POST = requests.post
_ORIG_REQUESTS_SESSION = requests.Session


class LLMError(Exception):
    """A bounded, credential-free model request or planning failure."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class _TLS12Adapter(HTTPAdapter):
    """Opt-in endpoint compatibility, retaining CA and hostname verification."""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        self._tls_context = context
        super().init_poolmanager(connections, maxsize, block=block, ssl_context=context, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._tls_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


class OpenAIBrain:
    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        timeout_seconds: float = 30,
        tls_version: str = "auto",
        gate_wait_seconds: float = 600.0,
        recover_uncertain_transport: bool = True,
        global_concurrency: bool = True,
        scope_id: Optional[str] = None,
        overflow_brain: Optional["OpenAIBrain"] = None,
        primary_wait_seconds: float = 1.5,
        primary_cooldown_seconds: float = 45.0,
        request_gate=_AUTO_GATE,
        recorder=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = min(timeout_seconds, TRANSPORT_WINDOW_SECONDS)
        if tls_version not in ("auto", "1.2"):
            raise LLMError("llm_invalid_config", "Unsupported model TLS version setting")
        self.tls_version = tls_version
        self.recover_uncertain_transport = bool(recover_uncertain_transport)
        self.global_concurrency = bool(global_concurrency)
        self.scope_id = scope_id
        self.overflow_brain = overflow_brain
        self.primary_wait_seconds = max(0.1, float(primary_wait_seconds))
        self.primary_cooldown_seconds = max(1.0, float(primary_cooldown_seconds))
        self._primary_cooldown_until: float = 0.0
        self.recorder = recorder
        self.request_gate = (
            ModelRequestGate(
                self.base_url,
                self.api_key,
                wait_seconds=max(5.0, float(gate_wait_seconds)),
                recorder=recorder,
                global_concurrency=self.global_concurrency,
                auto_recover_uncertain=self.recover_uncertain_transport,
                scope_id=self.scope_id,
            )
            if request_gate is _AUTO_GATE else request_gate
        )
        self._session_lock = threading.Lock()
        self._session: Optional[requests.Session] = None
        self._session_last_used: float = 0.0
        self.last_request_meta: Dict[str, Any] = {}

    def _get_session_with_state(self) -> Tuple[requests.Session, bool]:
        with self._session_lock:
            now = time.monotonic()
            # Recycle keep-alive sockets idle >20s so upstream proxies do not drop them mid-request.
            if self._session is not None and (now - self._session_last_used) > 20.0:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
            reused = self._session is not None
            if self._session is None:
                session = requests.Session()
                if self.tls_version == "1.2":
                    session.mount("https://", _TLS12Adapter())
                self._session = session
            self._session_last_used = now
            return self._session, reused

    def _get_session(self) -> requests.Session:
        return self._get_session_with_state()[0]

    def _send_post(self, url: str, options: Dict[str, Any]) -> Tuple[Any, bool]:
        post_patched = requests.post is not _ORIG_REQUESTS_POST
        session_patched = requests.Session is not _ORIG_REQUESTS_SESSION
        if self.tls_version == "1.2" and (session_patched or not post_patched):
            if session_patched:
                with requests.Session() as session:
                    session.mount("https://", _TLS12Adapter())
                    return session.post(url, **options), False
            session, reused = self._get_session_with_state()
            return session.post(url, **options), reused
        if post_patched:
            return requests.post(url, **options), False
        session, reused = self._get_session_with_state()
        return session.post(url, **options), reused

    def close(self) -> None:
        with self._session_lock:
            if self._session is not None:
                try:
                    self._session.close()
                finally:
                    self._session = None
        if self.overflow_brain is not None:
            self.overflow_brain.close()

    @property
    def headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _call_overflow(
        self,
        reason: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        cancel_event=None,
    ) -> Dict[str, Any]:
        if self.recorder is not None:
            try:
                self.recorder.record("INFO", "llm", "MODEL_OVERFLOW_ROUTED", {
                    "reason": reason,
                    "overflow_model": self.overflow_brain.model,
                })
            except Exception:
                pass
        data = self.overflow_brain.chat_completion(messages, tools, cancel_event=cancel_event)
        meta = dict(getattr(self.overflow_brain, "last_request_meta", {}) or {})
        meta.update({
            "route": "overflow",
            "overflow_reason": reason,
            "request_model": self.overflow_brain.model,
            "actual_model": data.get("model") if isinstance(data, dict) else None,
        })
        self.last_request_meta = meta
        return data

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        cancel_event=None,
    ) -> Dict[str, Any]:
        """Serialize every production model request; overflow only on busy/explicit error."""
        if self.request_gate is None:  # Explicit injection for isolated transport tests.
            data = self._chat_completion(messages, tools, cancel_event=cancel_event)
            self.last_request_meta.setdefault("route", "primary")
            if isinstance(data, dict) and data.get("model"):
                self.last_request_meta.setdefault("actual_model", data.get("model"))
            return data
        if self.overflow_brain is not None and time.monotonic() < self._primary_cooldown_until:
            return self._call_overflow("primary_cooldown", messages, tools, cancel_event=cancel_event)
        wait_limit = self.primary_wait_seconds if self.overflow_brain is not None else None
        try:
            with self.request_gate.request(cancel_event, wait_seconds=wait_limit):
                data = self._chat_completion(messages, tools, cancel_event=cancel_event)
                self.last_request_meta.update({
                    "route": "primary",
                    "request_model": self.model,
                    "actual_model": data.get("model") if isinstance(data, dict) else None,
                })
                return data
        except ModelGateError as error:
            if self.overflow_brain is not None and error.code == "llm_busy":
                return self._call_overflow("primary_busy", messages, tools, cancel_event=cancel_event)
            raise LLMError(error.code, str(error), error.details) from None
        except LLMError as error:
            if self.overflow_brain is not None and error.code in {"llm_http_error", "llm_invalid_response"}:
                self._primary_cooldown_until = time.monotonic() + self.primary_cooldown_seconds
                return self._call_overflow(f"primary_{error.code}", messages, tools, cancel_event=cancel_event)
            if error.code in {"llm_timeout", "llm_network_error"}:
                if self.overflow_brain is not None:
                    self._primary_cooldown_until = max(
                        self._primary_cooldown_until,
                        time.monotonic() + self.primary_cooldown_seconds,
                    )
                if self.recover_uncertain_transport:
                    raise
                details = {"transport_error": error.code, "executed": False}
                if isinstance(error.details, dict) and error.details.get("exception_type"):
                    details["exception_type"] = error.details["exception_type"]
                raise LLMError("llm_request_uncertain",
                               "Model transport ended without a complete response; replan after the retry window",
                               details) from None
            raise

    def _chat_completion(self, messages, tools=None, *, cancel_event=None):
        """Perform exactly one HTTP request while the caller owns its model slot."""
        if cancel_event is not None and cancel_event.is_set():
            raise LLMError("llm_cancelled", "Model request was cancelled")
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "required"
            payload["parallel_tool_calls"] = False

        url = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        payload_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        started = time.monotonic()
        session_reused = False
        try:
            # Do not follow redirects or retry: each response is one billed request.
            options = {"headers": self.headers, "json": payload, "timeout": self.timeout_seconds,
                       "allow_redirects": False}
            resp, session_reused = self._send_post(url, options)
        except requests.Timeout as exc:
            self.last_request_meta = {
                "session_reused": session_reused, "payload_bytes": payload_bytes,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "exception_type": type(exc).__name__,
            }
            self.close()
            raise LLMError("llm_timeout", "Model request timed out", {"exception_type": type(exc).__name__}) from None
        except requests.RequestException as exc:
            self.last_request_meta = {
                "session_reused": session_reused, "payload_bytes": payload_bytes,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "exception_type": type(exc).__name__,
            }
            self.close()
            raise LLMError("llm_network_error", "Model endpoint could not be reached", {"exception_type": type(exc).__name__}) from None
        self.last_request_meta = {
            "session_reused": session_reused, "payload_bytes": payload_bytes,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "http_status": int(resp.status_code),
        }
        if cancel_event is not None and cancel_event.is_set():
            raise LLMError("llm_cancelled", "Model response arrived after cancellation")
        if not 200 <= resp.status_code < 300:
            details = {"http_status": resp.status_code}
            retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") and resp.headers else None
            if isinstance(retry_after, str):
                try:
                    parsed_retry = float(retry_after.strip())
                except ValueError:
                    parsed_retry = None
                if parsed_retry is not None and 0 < parsed_retry <= 120:
                    details["retry_after"] = parsed_retry
            # Recognize context failures without retaining arbitrary provider
            # bodies, headers, URLs or any text that might echo credentials.
            if resp.status_code == 400:
                try:
                    body = resp.json()
                except (ValueError, requests.RequestException):
                    body = None
                error = body.get("error") if isinstance(body, dict) else None
                if isinstance(error, dict):
                    kind = error.get("type") or error.get("code")
                    if kind in ("exceed_context_size_error", "context_length_exceeded"):
                        details["context_limit"] = True
                        message = error.get("message")
                        if isinstance(message, str):
                            match = re.search(r"request \((\d+) tokens\).*context size \((\d+) tokens\)", message)
                            if match:
                                details.update(request_tokens=int(match[1]), context_tokens=int(match[2]))
            raise LLMError("llm_http_error", f"Model endpoint returned HTTP {resp.status_code}",
                           details)
        try:
            data = resp.json()
        except (ValueError, requests.RequestException):
            raise LLMError("llm_invalid_response", "Model endpoint returned invalid JSON") from None
        if not isinstance(data, dict) or data.get("error"):
            raise LLMError("llm_invalid_response", "Model endpoint returned an invalid completion object")
        return data
