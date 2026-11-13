"""Flight Recorder Structured Logger for BotSleep Actions and Telemetry."""

import json
import os
import time
from typing import Dict, Any, Optional


class FlightRecorder:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        date_str = time.strftime("%Y%m%d")
        self.log_file = os.path.join(self.log_dir, f"flight_{date_str}.jsonl")

    def record(self, level: str, module: str, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Appends a structured JSONL record to the flight log."""
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "level": level,
            "module": module,
            "event": event,
            "data": data or {},
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
