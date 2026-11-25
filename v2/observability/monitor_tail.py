"""L4/Observability offline JSONL FlightRecorder tailer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def read_flight_events(log_path: Path | str, *, limit: int = 100) -> List[Dict[str, Any]]:
    path = Path(log_path)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except ValueError:
            continue
    return rows[-limit:]
