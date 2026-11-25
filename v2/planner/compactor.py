"""L4 Pi/OMP SemanticCompactor: folds older turns into a structured ProgressCard + last 2 complete tool pairs.

Invariants:
- NEVER splits an assistant `tool_calls` message from its matching `tool` result message!
- Bounded memory size even across 50+ planning turns so total prompt stays <= 6,600 bytes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
from typing import Any, Deque, Dict, List


@dataclass
class ProgressCard:
    completed_goals: List[str] = field(default_factory=list)
    failed_goals: List[str] = field(default_factory=list)
    key_milestones: List[str] = field(default_factory=list)
    turns_compacted: int = 0

    def to_compact_text(self, *, max_bytes: int = 450) -> str:
        done = ",".join(self.completed_goals[-6:]) or "none"
        fail = ",".join(self.failed_goals[-4:]) or "none"
        mile = ",".join(self.key_milestones[-6:]) or "none"
        txt = f"PROGRESS_CARD(turns={self.turns_compacted}): done=[{done}] failed=[{fail}] milestones=[{mile}]"
        enc = txt.encode("utf-8")
        if len(enc) > max_bytes:
            return enc[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."
        return txt


class SemanticCompactor:
    """Maintains a ProgressCard + exact last-K complete (assistant tool_call, tool result) pairs."""

    def __init__(self, *, keep_pairs: int = 2) -> None:
        self.keep_pairs = max(1, int(keep_pairs))
        self.card = ProgressCard()
        self._pairs: Deque[tuple[Dict[str, Any], Dict[str, Any]]] = deque()

    def record_milestone(self, milestone: str) -> None:
        if milestone and milestone not in self.card.key_milestones:
            self.card.key_milestones.append(milestone)

    def record_tool_turn(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result_summary: Dict[str, Any],
        succeeded: bool = True,
    ) -> None:
        """Append one atomic (assistant tool_call, tool result) pair and compact oldest pairs into ProgressCard."""
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }],
        }
        compact_res = json.dumps(result_summary, ensure_ascii=False)
        if len(compact_res) > 220:
            compact_res = compact_res[:217] + "..."
        tool_msg: Dict[str, Any] = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name,
            "content": compact_res,
        }
        self._pairs.append((assistant_msg, tool_msg))

        # Compact excess pairs into ProgressCard
        while len(self._pairs) > self.keep_pairs:
            old_asst, old_tool = self._pairs.popleft()
            self.card.turns_compacted += 1
            tc = old_asst["tool_calls"][0]["function"]
            label = f"{tc['name']}({tc['arguments'][:36]})"
            if '"ok": false' in old_tool["content"].lower() or "fail" in old_tool["content"].lower():
                self.card.failed_goals.append(label)
            else:
                self.card.completed_goals.append(label)

        if not succeeded:
            self.card.failed_goals.append(f"{tool_name}")

    def history_messages(self) -> List[Dict[str, Any]]:
        """Return valid paired history messages: NEVER an orphan tool_call or orphan tool result."""
        msgs: List[Dict[str, Any]] = []
        for asst_msg, tool_msg in self._pairs:
            msgs.append(asst_msg)
            msgs.append(tool_msg)
        return msgs
