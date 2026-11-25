"""Read-only operator CLI for inspecting BotSleep survival checkpoints and model locks.

Never connects to Minecraft, mc-agent-api, RCON, or LLM endpoints, and never
clears unknown pending actions or model request locks. Use this tool before
replacing a running process to verify that no instance holds the survival lock,
no game action is pending, and the model request gate is idle.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from v2.kernel.config import ConfigError, load_config
from v2.kernel.journal import InstanceLockError, PendingTransactionJournal
from v2.kernel.model_gate import ModelGateError, ModelRequestGate
from v2.kernel.storage import UnifiedWorldDB

try:
    from core.survival_state import SurvivalJournal as _LegacySurvivalJournal
except ImportError:
    _LegacySurvivalJournal = None  # type: ignore[assignment]

DEFAULT_CONFIG_PATH = "config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect BotSleep survival state and model request gate without network I/O."
    )
    parser.add_argument("-c", "--config", default=None, help="Path to YAML configuration file")
    parser.add_argument("-u", "--username", default=None, help="Override configured agent username")
    parser.add_argument(
        "--gate-dir",
        default=None,
        help="Optional override directory for ModelRequestGate state (used by offline tests)",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "check-ready-to-replace"),
        help="Subcommand: 'status' prints state JSON; 'check-ready-to-replace' exits 0 iff safe to replace",
    )
    return parser


def inspect_runtime(
    config: Dict[str, Any],
    *,
    gate_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspect local survival checkpoint, instance lock, and model gate state."""
    endpoint = config["mc_api"]["base_url"]
    username = config["agent"]["name"]
    survival = config["survival"]
    world_id = survival["world_id"]
    objective = survival.get("objective", "starter")

    # Inspect v2 UnifiedWorldDB / PendingTransactionJournal (without mutating state)
    v2_db_path = UnifiedWorldDB.scope_db_path(survival["state_dir"], endpoint, username, world_id)
    v2_lock_path = v2_db_path.with_suffix(".instance.lock")
    v2_locked = False
    v2_pending = None
    if v2_lock_path.exists():
        # Non-blocking probe on existing lock file
        probe_db = UnifiedWorldDB(v2_db_path, api_base_url=endpoint, username=username, world_id=world_id)
        try:
            probe_journal = PendingTransactionJournal(probe_db)
            try:
                with probe_journal.instance_lock():
                    v2_locked = False
            except InstanceLockError:
                v2_locked = True
            v2_pending = probe_journal.get_pending()
        finally:
            probe_db.close()
    elif v2_db_path.is_file():
        probe_db = UnifiedWorldDB(v2_db_path, api_base_url=endpoint, username=username, world_id=world_id)
        try:
            v2_pending = PendingTransactionJournal(probe_db).get_pending()
        finally:
            probe_db.close()

    # Also inspect legacy v1 SurvivalJournal if present so old checkpoints/locks are never overlooked
    legacy_instance: Dict[str, Any] = {"locked": False}
    legacy_pending = None
    legacy_dir = str(v2_db_path.parent)
    legacy_updated_at = None
    if _LegacySurvivalJournal is not None:
        journal = _LegacySurvivalJournal(
            survival["state_dir"],
            endpoint=endpoint,
            username=username,
            world_id=world_id,
        )
        legacy_instance = journal.instance_status()
        legacy_pending = journal.state.get("pending")
        legacy_dir = str(journal.directory)
        legacy_updated_at = journal.state.get("updated_at")

    instance = {
        **legacy_instance,
        "locked": bool(v2_locked or legacy_instance.get("locked", False)),
        "v2_locked": v2_locked,
    }
    pending = v2_pending if v2_pending is not None else legacy_pending

    llm_cfg = config.get("llm", {})
    if llm_cfg.get("enabled"):
        gate_kwargs: Dict[str, Any] = {}
        if gate_dir is not None:
            gate_kwargs["directory"] = gate_dir
        gate = ModelRequestGate(
            llm_cfg.get("base_url", ""),
            llm_cfg.get("api_key", ""),
            **gate_kwargs,
        )
        gate_snapshot = {"enabled": True, **gate.snapshot()}
    else:
        gate_snapshot = {"enabled": False, "state": "idle"}

    block_reasons: List[str] = []
    if instance["locked"]:
        block_reasons.append("instance_already_running")
    if pending is not None:
        block_reasons.append("pending_game_action_requires_resume")
    gate_state = gate_snapshot.get("state")
    if gate_state in ("inflight", "in_flight"):
        block_reasons.append("model_gate_in_flight")
    elif gate_state == "uncertain":
        if gate_snapshot.get("error_code") in {"llm_timeout", "llm_network_error"}:
            block_reasons.append("model_gate_transport_cooldown")
        else:
            block_reasons.append("model_gate_uncertain_requires_acknowledge_model_idle")
    elif gate_state not in ("idle", None):
        block_reasons.append(f"model_gate_{gate_state}")

    return {
        "scope": {
            "mode": config["agent"]["mode"],
            "objective": objective,
            "endpoint": endpoint,
            "username": username,
            "world_id": world_id,
        },
        "instance": instance,
        "journal": {
            "directory": legacy_dir,
            "v2_db": str(v2_db_path),
            "updated_at": legacy_updated_at,
            "pending": pending,
            "v2_pending": v2_pending,
        },
        "model_gate": gate_snapshot,
        "ready_to_replace": len(block_reasons) == 0,
        "block_reasons": block_reasons,
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or os.environ.get("BOTSLEEP_CONFIG") or DEFAULT_CONFIG_PATH
    try:
        config = load_config(config_path, username=args.username)
        report = inspect_runtime(config, gate_dir=args.gate_dir)
    except (ConfigError, ModelGateError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.action == "check-ready-to-replace" and not report["ready_to_replace"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
