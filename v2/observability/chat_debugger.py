"""L4/Observability ChatDebugger: allowlisted #stop / #resume / #status / #task handler on Blackboard events."""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional

from v2.bt.blackboard import Blackboard


class ChatDebugger:
    """Zero-extra-poll chat command dispatcher consuming Blackboard.recent_events."""

    def __init__(
        self,
        *,
        prefix: str = "#",
        allowed_usernames: Iterable[str] = (),
        on_stop: Optional[Callable[[], None]] = None,
        on_resume: Optional[Callable[[], None]] = None,
        on_status: Optional[Callable[[], str]] = None,
        on_task: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.prefix = prefix
        self.allowed = {u.lower() for u in allowed_usernames}
        self.on_stop = on_stop
        self.on_resume = on_resume
        self.on_status = on_status
        self.on_task = on_task

    def process_events(self, bb: Blackboard) -> List[str]:
        handled: List[str] = []
        for evt in bb.recent_events:
            if evt.get("type") != "chat":
                continue
            sender = str(evt.get("username") or evt.get("sender") or "").lower()
            if self.allowed and sender and sender not in self.allowed:
                continue
            msg = str(evt.get("message") or "").strip()
            if not msg.startswith(self.prefix):
                continue
            cmd_line = msg[len(self.prefix):].strip()
            parts = cmd_line.split(None, 1)
            if not parts:
                continue
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd == "stop":
                bb.paused = True
                if self.on_stop:
                    self.on_stop()
                handled.append("stop")
            elif cmd == "resume":
                bb.paused = False
                if self.on_resume:
                    self.on_resume()
                handled.append("resume")
            elif cmd == "status":
                if self.on_status:
                    self.on_status()
                handled.append("status")
            elif cmd == "task" and arg:
                if self.on_task:
                    self.on_task(arg)
                handled.append(f"task:{arg}")
        return handled
