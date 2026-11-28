"""L0 Dual-channel mc-agent-api client: single-threaded action session + threading.local() AsyncReadPool.

Strict isolation guarantees:
- Action writes (`WRITE_ENDPOINTS`) use `_action_session` exclusively on the main loop thread.
- Read-only parallel calls (`observe_tick_bundle`, `batch_get_blocks`) run in `AsyncReadPool`
  using per-thread `requests.Session` instances; read workers NEVER mutate or close `_action_session`.
- Transport timeouts or `not_connected` on `WRITE_ENDPOINTS` always emit `action_state="unknown"`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import requests

from v2.domain import Coord3


class MinecraftAPIError(RuntimeError):
    """Compatibility base error class matching legacy APIError catches."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            error = {"code": "invalid_response", "message": "invalid API error envelope"}
        self.code = str(error.get("code", "invalid_response"))
        super().__init__(f"{self.code}: {error.get('message', 'API call failed')}")


class APIError(MinecraftAPIError):
    """HTTP, transport, or mc-agent-api business failure."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.payload = {"ok": False, "error": {**self.details, "code": code, "message": message}}
        RuntimeError.__init__(self, f"{code}: {message}")


def connection_retry_delay(error: APIError, base_seconds: float) -> float:
    seconds = error.details.get("retry_after_s")
    if error.code == "death_loop_cooldown":
        match = re.search(r"\b(\d+)s remaining\b", error.message)
        if match:
            seconds = int(match.group(1))
    if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0:
        return max(base_seconds, float(seconds))
    return base_seconds


class AsyncReadPool:
    """Thread-local keep-alive HTTP reader pool isolated from the action write session."""

    def __init__(
        self,
        base_url: str,
        token_provider: Any,
        *,
        timeout_seconds: float = 15.0,
        max_workers: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self.timeout_seconds = float(timeout_seconds)
        self.max_workers = max(1, int(max_workers))
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: List[requests.Session] = []
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="v2-read")

    def _thread_session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._local.session = sess
            with self._sessions_lock:
                self._sessions.append(sess)
        return sess

    def _reset_thread_session(self) -> requests.Session:
        old = getattr(self._local, "session", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        sess = requests.Session()
        self._local.session = sess
        with self._sessions_lock:
            self._sessions.append(sess)
        return sess

    def read_json(
        self,
        method: str,
        endpoint: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        token = self._token_provider()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": timeout}
        if payload is not None:
            kwargs["json"] = payload
        if params is not None:
            kwargs["params"] = params

        sess = self._thread_session()
        deadline = time.monotonic() + timeout
        try:
            resp = sess.request(method, url, **kwargs)
        except requests.Timeout:
            raise APIError("request_timeout", f"{endpoint} read timed out") from None
        except requests.ConnectionError:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise APIError("request_timeout", f"{endpoint} read retry exhausted") from None
            try:
                sess = self._reset_thread_session()
                resp = sess.request(method, url, **{**kwargs, "timeout": rem})
            except requests.Timeout:
                raise APIError("request_timeout", f"{endpoint} read timed out") from None
            except requests.RequestException:
                raise APIError("network_error", f"{endpoint} read failed") from None
        except requests.RequestException:
            raise APIError("network_error", f"{endpoint} read failed") from None

        try:
            data = resp.json()
        except ValueError:
            raise APIError("invalid_json", f"{endpoint} returned invalid JSON", resp.status_code) from None
        if not isinstance(data, dict):
            raise APIError("invalid_response", f"{endpoint} returned non-object", resp.status_code)
        if not 200 <= resp.status_code < 300 or data.get("ok") is False:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(err.get("code") or "api_error")
            msg = str(err.get("message") or f"{endpoint} failed")
            details = {k: v for k, v in err.items() if k not in {"code", "message"}}
            raise APIError(code, msg, resp.status_code, details)
        if data.get("ok") is not True:
            raise APIError("invalid_response", f"{endpoint} missing ok=true", resp.status_code)
        return data

    def observe_tick_bundle(
        self,
        *,
        last_event_seq: int = 0,
        entity_radius: int = 32,
        event_limit: int = 50,
    ) -> Dict[str, Any]:
        """Fetch status, inventory, entities, and incremental events concurrently in 1 RTT."""
        f_status = self._executor.submit(self.read_json, "GET", "status")
        f_inv = self._executor.submit(self.read_json, "GET", "inventory")
        f_ent = self._executor.submit(
            self.read_json, "POST", "get_entities", payload={"radius": int(entity_radius)}
        )
        f_evt = self._executor.submit(
            self.read_json, "GET", "events", params={"since": int(last_event_seq), "limit": int(event_limit)}
        )
        status = f_status.result()["result"]
        inventory = f_inv.result()["result"]
        entities = f_ent.result()["result"].get("entities", [])
        events_res = f_evt.result()["result"]
        return {
            "status": status,
            "inventory": inventory,
            "entities": entities,
            "events": events_res.get("events", []),
            "last_event_seq": int(events_res.get("last_seq", last_event_seq)),
            "has_more_events": bool(events_res.get("has_more", False)),
        }

    def batch_get_blocks(
        self,
        coords: Iterable[Coord3],
        *,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[Coord3, Optional[Dict[str, Any]]]:
        """Concurrently fetch multiple block coordinates; returns None for not_loaded cells."""
        unique = list(dict.fromkeys((int(c[0]), int(c[1]), int(c[2])) for c in coords))
        if not unique:
            return {}

        def _fetch(coord: Coord3) -> Tuple[Coord3, Optional[Dict[str, Any]]]:
            try:
                env = self.read_json(
                    "POST", "get_block", payload={"pos": [coord[0], coord[1], coord[2]]}, timeout_seconds=timeout_seconds
                )
                return coord, env.get("result")
            except APIError as err:
                if err.code == "not_loaded":
                    return coord, None
                raise

        futures = [self._executor.submit(_fetch, c) for c in unique]
        out: Dict[Coord3, Optional[Dict[str, Any]]] = {}
        for fut in as_completed(futures):
            coord, blk = fut.result()
            out[coord] = blk
        return out

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._sessions_lock:
            for sess in self._sessions:
                try:
                    sess.close()
                except Exception:
                    pass
            self._sessions.clear()


class MinecraftAgentClient:
    """Complete L0 mc-agent-api client with isolated write channel and concurrent read pool."""

    ACTION_MARGIN_SECONDS = 5.0
    PATHFINDING_SECONDS = 60.0
    WRITE_ENDPOINTS = frozenset({
        "chat", "move_to", "look", "posture", "equip", "window/view", "window/act",
        "attack", "dig", "craft", "place", "interact", "use_item", "control", "shoot",
        "drop", "dismount",
    })
    READ_RETRY_ENDPOINTS = frozenset({"get_block", "status", "find_blocks", "inventory", "get_entities", "events"})

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18900/api/v1",
        username: str = "SleepBot",
        password: str = "agent-secret-pass",
        timeout_seconds: float = 15.0,
        recorder: Any = None,
        read_workers: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = self._positive(timeout_seconds, "timeout_seconds")
        self.token: Optional[str] = None
        self._continuous_session_file: Optional[Union[str, Path]] = None
        self._action_session = requests.Session()
        self._session = self._action_session  # compatibility alias
        self.read_pool = AsyncReadPool(
            self.base_url, lambda: self.token, timeout_seconds=self.timeout_seconds, max_workers=read_workers
        )
        self.activity_seq = 0
        self.last_write_at = 0.0
        self.recorder = recorder

    @staticmethod
    def _positive(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise APIError("bad_args", f"{name} must be a positive finite number")
        return float(value)

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _action_timeout(self, deadline_seconds: float) -> float:
        return max(self.timeout_seconds, deadline_seconds + self.ACTION_MARGIN_SECONDS)

    @staticmethod
    def _extract_target_coord(endpoint: str, payload: Optional[Dict[str, Any]]) -> Optional[List[int]]:
        """Extract integer block coordinates from spatial action payloads for heatmap telemetry."""
        if endpoint not in {"dig", "place", "move_to", "interact"} or not isinstance(payload, dict):
            return None
        if "x" in payload and "z" in payload and "y" in payload:
            x, y, z = payload.get("x"), payload.get("y"), payload.get("z")
            if (
                not isinstance(x, bool) and isinstance(x, (int, float))
                and not isinstance(y, bool) and isinstance(y, (int, float))
                and not isinstance(z, bool) and isinstance(z, (int, float))
            ):
                return [int(math.floor(float(x))), int(math.floor(float(y))), int(math.floor(float(z)))]
        for key in ("pos", "ref"):
            seq = payload.get(key)
            if isinstance(seq, (list, tuple)) and len(seq) == 3:
                if all(not isinstance(v, bool) and isinstance(v, (int, float)) for v in seq):
                    return [int(math.floor(float(v))) for v in seq]
        return None

    def _request(self, method: str, endpoint: str, **options: Any) -> Dict[str, Any]:
        if self.recorder is None:
            return self._send_request(method, endpoint, **options)
        started = time.perf_counter()
        ok = False
        err_info: Optional[Dict[str, Any]] = None
        target = self._extract_target_coord(endpoint, options.get("payload"))
        try:
            res = self._send_request(method, endpoint, **options)
            ok = True
            return res
        except APIError as err:
            err_info = {
                "endpoint": endpoint,
                "method": method,
                "code": err.code,
                "message": err.message,
                "action_state": err.details.get("action_state"),
                "duration_ms": round(1000 * (time.perf_counter() - started)),
            }
            if target is not None:
                err_info["target"] = target
            raise
        finally:
            dur = round(1000 * (time.perf_counter() - started))
            try:
                timing_data: Dict[str, Any] = {
                    "endpoint": endpoint,
                    "method": method,
                    "duration_ms": dur,
                    "ok": ok,
                }
                if target is not None:
                    timing_data["target"] = target
                self.recorder.record("INFO", "mc_api", "API_TIMING", timing_data)
                if err_info is not None:
                    self.recorder.record("WARN", "mc_api", "API_ERROR", err_info)
            except Exception:
                pass

    def _send_request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        timeout = self.timeout_seconds if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        kwargs: Dict[str, Any] = {"headers": self.headers if authenticated else {}, "timeout": timeout}
        if payload is not None:
            kwargs["json"] = payload
        if params is not None:
            kwargs["params"] = params

        url = f"{self.base_url}/{endpoint}"
        is_write = endpoint in self.WRITE_ENDPOINTS
        retry_read = endpoint in self.READ_RETRY_ENDPOINTS
        read_deadline = time.monotonic() + timeout if retry_read else None

        try:
            response = self._action_session.request(method, url, **kwargs)
        except requests.Timeout:
            if not is_write:
                raise APIError("request_timeout", f"{endpoint} read request timed out") from None
            raise APIError(
                "request_timeout",
                f"{endpoint} request timed out; server action state is unknown",
                details={"action_state": "unknown"},
            ) from None
        except requests.ConnectionError:
            if retry_read and read_deadline is not None:
                remaining = read_deadline - time.monotonic()
                if remaining <= 0:
                    raise APIError("request_timeout", f"{endpoint} read retry exhausted") from None
                try:
                    self._action_session.close()
                    self._action_session = requests.Session()
                    self._session = self._action_session
                    response = self._action_session.request(method, url, **{**kwargs, "timeout": remaining})
                except requests.Timeout:
                    raise APIError("request_timeout", f"{endpoint} read request timed out") from None
                except requests.RequestException:
                    raise APIError("network_error", f"{endpoint} request failed") from None
            elif is_write:
                raise APIError(
                    "network_error",
                    f"{endpoint} disconnected during write; server action state is unknown",
                    details={"action_state": "unknown"},
                ) from None
            else:
                raise APIError("network_error", f"{endpoint} request failed") from None
        except requests.RequestException:
            details = {"action_state": "unknown"} if is_write else {}
            raise APIError("network_error", f"{endpoint} request failed", details=details) from None

        status_code = response.status_code
        try:
            data = response.json()
        except ValueError:
            raise APIError("invalid_json", f"{endpoint} returned invalid JSON", status_code) from None
        if not isinstance(data, dict):
            raise APIError("invalid_response", f"{endpoint} returned non-object response", status_code)
        if not 200 <= status_code < 300 or data.get("ok") is False:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(error.get("code") or ("http_error" if not 200 <= status_code < 300 else "api_error"))
            message = str(error.get("message") or f"{endpoint} failed (HTTP {status_code})")
            for secret in (self.password, self.token):
                if secret:
                    message = message.replace(secret, "[redacted]")
            details = {k: v for k, v in error.items() if k not in {"code", "message"}}
            if code == "not_connected" and is_write:
                details.update(api_code=code, action_state="unknown", executed=True)
                raise APIError(
                    "action_unconfirmed",
                    f"{endpoint} disconnected before its outcome was confirmed",
                    status_code,
                    details,
                )
            raise APIError(code, message, status_code, details)
        if data.get("ok") is not True:
            raise APIError("invalid_response", f"{endpoint} response missing boolean ok=true", status_code)
        self.activity_seq += 1
        if is_write:
            self.last_write_at = time.monotonic()
        return data

    @staticmethod
    def _result(envelope: Dict[str, Any]) -> Dict[str, Any]:
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise APIError("invalid_response", "API result must be an object")
        return result

    @staticmethod
    def _list(result: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        values = result.get(key)
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise APIError("invalid_response", f"API {key} must be a list of objects")
        return values

    def close(self) -> None:
        self.read_pool.close()
        self._action_session.close()

    # ---- High-Throughput Concurrent Read Pool Helpers ----

    def observe_tick_bundle(self, *, last_event_seq: int = 0, entity_radius: int = 32) -> Dict[str, Any]:
        return self.read_pool.observe_tick_bundle(last_event_seq=last_event_seq, entity_radius=entity_radius)

    def batch_get_blocks(self, coords: Iterable[Coord3]) -> Dict[Coord3, Optional[Dict[str, Any]]]:
        return self.read_pool.batch_get_blocks(coords)

    # ---- Session Management ----

    def _open_session(self) -> None:
        data = self._request(
            "POST", "auth/login", payload={"username": self.username, "password": self.password}, authenticated=False
        )
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise APIError("invalid_response", "Login response has no token")
        self.token = token

    def save_session(self, session_file: Union[str, Path]) -> Optional[Path]:
        if not self.token:
            return None
        path = Path(session_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"base_url": self.base_url, "username": self.username, "token": self.token, "saved_at": time.time()}
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load_session(self, session_file: Union[str, Path]) -> bool:
        path = Path(session_file)
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        if str(data.get("base_url") or "").rstrip("/") != self.base_url or data.get("username") != self.username:
            return False
        token = data.get("token")
        if not isinstance(token, str) or not token:
            return False
        self.token = token
        return True

    def login(self, max_wait_seconds: float = 15.0, *, retry_rate_limit: bool = False, max_login_attempts: int = 3) -> Dict[str, Any]:
        max_wait_seconds = self._positive(max_wait_seconds, "max_wait_seconds")
        attempts = max(1, int(max_login_attempts)) if retry_rate_limit else 1
        if self.token:
            self.logout()
        try:
            deadline = time.monotonic() + max_wait_seconds
            for attempt in range(attempts):
                try:
                    self._open_session()
                    break
                except APIError as error:
                    if not retry_rate_limit or attempt + 1 >= attempts or error.code not in {"rate_limited", "routing_unavailable"}:
                        raise
                    wait_s = error.details.get("retry_after_s") if isinstance(error.details, dict) else None
                    sleep_s = float(wait_s) if isinstance(wait_s, (int, float)) and wait_s > 0 else 2.0
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(remaining, min(65.0, sleep_s + 0.25)))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise APIError("ready_timeout", "Timed out waiting for bot readiness")
                try:
                    status = self._result(self.get_status(timeout_seconds=min(self.timeout_seconds, remaining)))
                except APIError as error:
                    init_err = error.code == "internal" and error.status_code == 200 and "toFixed" in error.message
                    if error.code not in {"not_connected", "not_ready", "wrong_server"} and not init_err:
                        raise
                else:
                    if status.get("ready") is True:
                        return status
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.5, remaining))
        except BaseException:
            if self.token:
                try:
                    self.logout()
                except Exception:
                    self.token = None
            raise

    def attach_or_login(self, session_file: Optional[Union[str, Path]] = None, *, max_wait_seconds: float = 15.0) -> Dict[str, Any]:
        if not self.token and session_file is not None:
            self.load_session(session_file)
        if self.token:
            try:
                status = self._result(self.get_status(timeout_seconds=min(self.timeout_seconds, max_wait_seconds)))
                if status.get("ready") is True:
                    if session_file is not None:
                        self.save_session(session_file)
                    return status
            except APIError as err:
                if err.code not in {"unauthorized", "invalid_token", "not_connected", "wrong_server", "not_ready"}:
                    raise
                self.token = None
        status = self.login(max_wait_seconds=max_wait_seconds)
        if session_file is not None:
            self.save_session(session_file)
        return status

    def logout(self) -> Dict[str, Any]:
        if not self.token:
            return {"ok": True}
        try:
            return self._request("POST", "auth/logout")
        finally:
            self.token = None

    def connect(self) -> Dict[str, Any]:
        return self._request("POST", "connect")

    def disconnect(self) -> Dict[str, Any]:
        return self._request("POST", "disconnect")

    def stop(self, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "stop", timeout_seconds=timeout_seconds)

    # ---- Read Endpoints ----

    def get_status(self, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "status", timeout_seconds=timeout_seconds)

    def inventory(self, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "inventory", timeout_seconds=timeout_seconds)

    def get_inventory(self) -> Dict[str, Any]:
        return self.inventory()

    def get_inventory_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self._result(self.inventory()).get("items", []):
            name = item.get("name")
            if name:
                counts[name] = counts.get(name, 0) + int(item.get("count", 1))
        return counts

    def get_events_page(self, since: int = 0, limit: int = 50, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        response = self._request("GET", "events", params={"since": since, "limit": limit}, timeout_seconds=timeout_seconds)
        self._list(self._result(response), "events")
        return response

    def get_events(self, since: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        return self._result(self.get_events_page(since=since, limit=limit))["events"]

    def get_block(self, pos_or_x: Any, y: Optional[float] = None, z: Optional[float] = None, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        if isinstance(pos_or_x, (list, tuple)) and len(pos_or_x) == 3:
            pos = [pos_or_x[0], pos_or_x[1], pos_or_x[2]]
        elif y is not None and z is not None:
            pos = [pos_or_x, y, z]
        else:
            raise APIError("bad_args", "get_block requires three coordinates or a pos list")
        return self._request("POST", "get_block", payload={"pos": pos}, timeout_seconds=timeout_seconds)

    def get_blocks(self, radius: int = 16, types: Optional[List[str]] = None, limit: int = 64) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"radius": radius, "limit": limit}
        if types is not None:
            payload["types"] = types
        result = self._result(self._request("POST", "find_blocks", payload=payload))
        if types:
            return self._list(result, "blocks")
        blocks = result.get("blocks")
        if not isinstance(blocks, dict):
            raise APIError("invalid_response", "Untyped find_blocks must return summary dict")
        return [{"name": name, "pos": info.get("nearest"), "dist": info.get("dist"), "count": info.get("count")} for name, info in blocks.items()]

    def find_blocks(
        self,
        types: List[str],
        radius: int = 16,
        limit: int = 64,
        *,
        properties: Optional[Dict[str, Any]] = None,
        mature: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"types": types, "radius": radius, "limit": limit}
        if properties is not None:
            payload["properties"] = properties
        if mature is not None:
            payload["mature"] = mature
        return self._list(self._result(self._request("POST", "find_blocks", payload=payload, timeout_seconds=timeout_seconds)), "blocks")

    def get_entities(self, radius: int = 16, types: Optional[List[str]] = None, *, timeout_seconds: Optional[float] = None) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"radius": radius}
        if types is not None:
            payload["types"] = types
        return self._list(self._result(self._request("POST", "get_entities", payload=payload, timeout_seconds=timeout_seconds)), "entities")

    def advancements(self, *, status: str = "all", include_recipes: bool = False, include_criteria: bool = False, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        envelope = self._request(
            "POST",
            "advancements",
            payload={"status": status, "include_recipes": include_recipes, "include_criteria": include_criteria},
            timeout_seconds=timeout_seconds,
        )
        result = self._result(envelope)
        self._list(result, "advancements")
        return envelope

    # ---- Write Action Endpoints ----

    def move_to(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        timeout_ms: int = 15000,
        range: Optional[float] = None,
        stop_on: Optional[List[str]] = None,
        *,
        entity_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        deadline = self._positive(timeout_ms, "timeout_ms") / 1000.0
        payload: Dict[str, Any] = {"timeout": int(timeout_ms)}
        if entity_id is not None:
            payload["entity_id"] = int(entity_id)
        else:
            if x is None or z is None:
                raise APIError("bad_args", "move_to requires x and z")
            payload["x"] = x
            payload["z"] = z
            if y is not None:
                payload["y"] = y
        if range is not None:
            payload["range"] = range
        if stop_on is not None:
            payload["stop_on"] = stop_on
        return self._request("POST", "move_to", payload=payload, timeout_seconds=self._action_timeout(deadline))

    def dig(
        self,
        pos_or_x: Any,
        y: Optional[float] = None,
        z: Optional[float] = None,
        collect: bool = True,
        stop_on: Optional[List[str]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        if isinstance(pos_or_x, (list, tuple)) and len(pos_or_x) == 3:
            if y is not None and type(y) is bool:
                collect = y
            px, py, pz = pos_or_x[0], pos_or_x[1], pos_or_x[2]
        elif y is not None and z is not None:
            px, py, pz = pos_or_x, y, z
        else:
            raise APIError("bad_args", "dig requires three coordinates")
        payload: Dict[str, Any] = {"x": px, "y": py, "z": pz, "collect": collect}
        if stop_on is not None:
            payload["stop_on"] = stop_on
        timeout = self._action_timeout(timeout_ms / 1000.0) if timeout_ms is not None else self._action_timeout(130.0)
        return self._request("POST", "dig", payload=payload, timeout_seconds=timeout)

    def place_block(
        self,
        item: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        *,
        stop_on: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"item": item}
        if x is not None or y is not None or z is not None:
            if x is None or y is None or z is None:
                raise APIError("bad_args", "place_block requires either all of x, y, z or none (autoPlaceSite)")
            payload["x"] = x
            payload["y"] = y
            payload["z"] = z
        if stop_on is not None:
            payload["stop_on"] = stop_on
        return self._request("POST", "place", payload=payload, timeout_seconds=self._action_timeout(self.PATHFINDING_SECONDS))

    def interact(
        self,
        pos: Optional[Sequence[float]] = None,
        entity_id: Optional[int] = None,
        item: Optional[str] = None,
        ref: Optional[Sequence[int]] = None,
        face: Optional[Sequence[int]] = None,
        timeout_seconds: Optional[float] = None,
        stop_on: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if pos is not None:
            payload["pos"] = list(pos)
        if entity_id is not None:
            payload["entity_id"] = entity_id
        if item is not None:
            payload["item"] = item
        if ref is not None:
            payload["ref"] = list(ref)
        if face is not None:
            payload["face"] = list(face)
        if stop_on is not None:
            payload["stop_on"] = stop_on
        t = self._action_timeout(self.PATHFINDING_SECONDS) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "interact", payload=payload, timeout_seconds=t)

    def craft(self, item: str, count: int = 1) -> Dict[str, Any]:
        return self._request("POST", "craft", payload={"item": item, "count": int(count)}, timeout_seconds=self._action_timeout(415.0))

    @staticmethod
    def _build_window_target(
        pos: Optional[Sequence[int]] = None,
        *,
        block: Optional[str] = None,
        entity_id: Optional[int] = None,
        target: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if isinstance(target, dict) and target:
            return dict(target)
        if pos is not None:
            return {"pos": list(pos)}
        if block is not None:
            return {"block": str(block)}
        if entity_id is not None:
            return {"entity_id": int(entity_id)}
        raise APIError("bad_args", "window target requires pos, block, entity_id, or target dict")

    def window_view(
        self,
        pos: Optional[Sequence[int]] = None,
        *,
        block: Optional[str] = None,
        entity_id: Optional[int] = None,
        target: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        tgt = self._build_window_target(pos, block=block, entity_id=entity_id, target=target)
        budget = self._action_timeout(65.0) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "window/view", payload={"target": tgt}, timeout_seconds=budget)

    def window_act(
        self,
        pos: Optional[Sequence[int]] = None,
        ops: Optional[List[Dict[str, Any]]] = None,
        *,
        block: Optional[str] = None,
        entity_id: Optional[int] = None,
        target: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not isinstance(ops, list) or not 1 <= len(ops) <= 8:
            raise APIError("bad_args", "window_act requires 1 to 8 operations")
        tgt = self._build_window_target(pos, block=block, entity_id=entity_id, target=target)
        budget = self._action_timeout(115.0) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "window/act", payload={"target": tgt, "ops": ops}, timeout_seconds=budget)

    def equip(self, item: str, slot: str = "hand", *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "equip", payload={"item": item, "slot": slot}, timeout_seconds=timeout_seconds)

    def use_item(
        self,
        item: str,
        ms: Optional[int] = None,
        *,
        target: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
        source: Optional[str] = None,
        stop_on: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"item": item}
        if ms is not None:
            payload["ms"] = int(ms)
        if target is not None:
            payload["target"] = dict(target)
        if timeout_ms is not None:
            payload["timeout"] = int(timeout_ms)
        if source is not None:
            payload["source"] = str(source)
        if stop_on is not None:
            payload["stop_on"] = stop_on
        return self._request("POST", "use_item", payload=payload, timeout_seconds=self._action_timeout(self.PATHFINDING_SECONDS))

    def posture(self, sneak: bool = False, shield: bool = False, jump: bool = False, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "posture", payload={"sneak": sneak, "shield": shield, "jump": jump}, timeout_seconds=timeout_seconds)

    def look_at(self, x: float, y: float, z: float, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "look", payload={"x": x, "y": y, "z": z}, timeout_seconds=timeout_seconds)

    def control(
        self,
        *,
        forward: bool = False,
        back: bool = False,
        left: bool = False,
        right: bool = False,
        jump: bool = False,
        sneak: bool = False,
        sprint: bool = False,
        yaw: Optional[float] = None,
        pitch: Optional[float] = None,
        ms: Optional[int] = 50,
        hit: Optional[Union[bool, int]] = None,
        hit_interval_ms: Optional[int] = None,
        wait: bool = True,
        timeout_seconds: Optional[float] = None,
        stop_on: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        is_release = (
            wait is False
            and not any((forward, back, left, right, jump, sneak, sprint))
            and not hit
            and yaw is None
            and pitch is None
        )
        if ms is not None:
            if type(ms) is not int or not 50 <= ms <= 3000:
                raise APIError("bad_args", "control ms must be an integer from 50 to 3000")
        elif not is_release:
            raise APIError("bad_args", "control ms is required unless releasing held keys with wait=False")
        if hit is not None and not isinstance(hit, bool):
            if type(hit) is not int or not 1 <= hit <= 20:
                raise APIError("bad_args", "control hit must be a bool or integer from 1 to 20")
        payload: Dict[str, Any] = {
            "forward": forward, "back": back, "left": left, "right": right,
            "jump": jump, "sneak": sneak, "sprint": sprint,
            "wait": wait, "stop_on": ["hurt", "death"] if stop_on is None else list(stop_on),
        }
        if ms is not None:
            payload["ms"] = ms
        if yaw is not None:
            payload["yaw"] = yaw
        if pitch is not None:
            payload["pitch"] = pitch
        if hit is not None:
            payload["hit"] = hit
        if hit_interval_ms is not None:
            payload["hit_interval_ms"] = hit_interval_ms
        budget_ms = (ms if ms is not None else 500) / 1000.0
        budget = self._action_timeout(budget_ms) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "control", payload=payload, timeout_seconds=budget)

    def attack(
        self,
        entity_id: int,
        *,
        timeout_ms: int = 6000,
        max_hits: int = 6,
        stop_on: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        deadline = self._positive(timeout_ms, "timeout_ms") / 1000.0
        effective_stop_on = ["death"] if stop_on is None else list(stop_on)
        return self._request(
            "POST",
            "attack",
            payload={
                "entity_id": int(entity_id),
                "timeout": int(timeout_ms),
                "max_hits": int(max_hits),
                "stop_on": effective_stop_on,
            },
            timeout_seconds=self._action_timeout(deadline + 35.0),
        )

    def shoot(
        self,
        entity_id: Optional[int] = None,
        *,
        pos: Optional[Sequence[float]] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        weapon: Optional[str] = "bow",
        charge_ms: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"stop_on": ["hurt", "death"]}
        if entity_id is not None:
            payload["entity_id"] = int(entity_id)
        elif pos is not None and len(pos) == 3:
            payload["x"] = float(pos[0])
            payload["y"] = float(pos[1])
            payload["z"] = float(pos[2])
        elif x is not None and y is not None and z is not None:
            payload["x"] = float(x)
            payload["y"] = float(y)
            payload["z"] = float(z)
        else:
            raise APIError("bad_args", "shoot requires entity_id, pos, or x/y/z")
        if weapon is not None:
            payload["weapon"] = str(weapon)
        if charge_ms is None:
            charge_ms = 1400 if weapon == "crossbow" else 1100
        payload["charge_ms"] = int(charge_ms)
        budget = self._action_timeout(charge_ms / 1000.0 + 1.0) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "shoot", payload=payload, timeout_seconds=budget)

    def drop(self, item: str, count: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"item": item}
        if count is not None:
            payload["count"] = int(count)
        return self._request("POST", "drop", payload=payload, timeout_seconds=self._action_timeout(15.0))

    def dismount(self) -> Dict[str, Any]:
        return self._request("POST", "dismount")

    def chat(self, message: str) -> Dict[str, Any]:
        return self._request("POST", "chat", payload={"message": message})
