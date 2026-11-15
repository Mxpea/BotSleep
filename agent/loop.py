"""Legacy Agent Main Decision Loop Skeleton (NOT the active survival loop).

NOTE: This module is a historical prototype kept only for backward compatibility.
The active starter, daily, and competition survival loops are driven by
`main.py` -> `core/survival_harness.py` (`SurvivalHarness`), `agent/llm_planner.py`,
`agent/survival_planner.py`, and `agent/daily_survival.py`. Do not infer overall
project status from the TODO placeholders in this file.
"""

import time
from typing import Dict, Any, Optional
from core.client import MinecraftAgentClient
from core.telemetry import TelemetryManager
from .brain import OpenAIBrain


class AgentLoop:
    def __init__(
        self,
        client: MinecraftAgentClient,
        brain: OpenAIBrain,
        telemetry: TelemetryManager,
    ):
        self.client = client
        self.brain = brain
        self.telemetry = telemetry
        self.running = False

    def start(self) -> None:
        """Starts main observation-thought-action cycle."""
        self.running = True
        while self.running:
            try:
                # 1. Observe
                status = self.telemetry.poll()

                # 2. Think (LLM inference when conditions or triggers met)
                # TODO: Construct context prompt & call self.brain.chat_completion()

                # 3. Act (Execute returned tool calls)
                # TODO: Dispatch tool calls to skills

                time.sleep(2.0)
            except KeyboardInterrupt:
                self.stop()
            except Exception as e:
                time.sleep(1.0)

    def stop(self) -> None:
        self.running = False
