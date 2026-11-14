"""OpenAI-Compatible LLM Brain Interface

Connects to any OpenAI-compatible API endpoint (OpenAI, DeepSeek, Qwen, Ollama, etc.).
"""

from typing import List, Dict, Any, Optional
import re
import ssl
import requests
from requests.adapters import HTTPAdapter


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
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        if tls_version not in ("auto", "1.2"):
            raise LLMError("llm_invalid_config", "Unsupported model TLS version setting")
        self.tls_version = tls_version

    @property
    def headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        cancel_event=None,
    ) -> Dict[str, Any]:
        """Sends chat completion request with optional tool schemas."""
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
            payload["tool_choice"] = "auto"

        url = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        try:
            # Do not follow redirects or retry: each response is one billed request.
            options = {"headers": self.headers, "json": payload, "timeout": self.timeout_seconds,
                       "allow_redirects": False}
            if self.tls_version == "1.2":
                with requests.Session() as session:
                    session.mount("https://", _TLS12Adapter())
                    resp = session.post(url, **options)
            else:
                resp = requests.post(url, **options)
        except requests.Timeout:
            raise LLMError("llm_timeout", "Model request timed out") from None
        except requests.RequestException:
            raise LLMError("llm_network_error", "Model endpoint could not be reached") from None
        if cancel_event is not None and cancel_event.is_set():
            raise LLMError("llm_cancelled", "Model response arrived after cancellation")
        if not 200 <= resp.status_code < 300:
            details = {"http_status": resp.status_code}
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
