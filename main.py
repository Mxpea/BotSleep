"""BotSleep - Autonomous Minecraft AI Agent Main Entrypoint"""

import argparse
import sys
import time
from core.client import MinecraftAgentClient
from core.telemetry import TelemetryManager, ReflexDaemon
from core.radar import BlockRadar
from core.memory import MemoryEngine
from skills.exploration import ContinuousRadarSkill
from agent.brain import OpenAIBrain
from agent.loop import AgentLoop
from debug.chat_debugger import ChatDebugger
from debug.logger import FlightRecorder


def parse_args():
    parser = argparse.ArgumentParser(description="BotSleep - Autonomous Minecraft AI Agent")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--debug", action="store_true", default=True, help="Enable in-game chat debugging")
    parser.add_argument("--radar", action="store_true", help="Start continuous exploration radar on launch")
    parser.add_argument("--username", default="SleepBot", help="Minecraft bot username")
    return parser.parse_args()


def main():
    args = parse_args()
    print(f"=== Starting BotSleep (Bot: {args.username}) ===")

    recorder = FlightRecorder()
    recorder.record("INFO", "main", "STARTUP", {"username": args.username, "radar_auto": args.radar})

    client = MinecraftAgentClient(
        base_url="http://127.0.0.1:18900/api/v1",
        username=args.username,
        password="agent-secret-pass",
    )

    try:
        print("Connecting bot to Minecraft server via mc-agent-api...")
        status = client.login()
        print(f"Bot '{args.username}' online at {status['pos']} in dimension '{status['dimension']}'")
        client.chat(f"{args.username} is online! Type #help or #radar in chat for commands.")

        memory = MemoryEngine()
        radar = BlockRadar(client)
        radar_skill = ContinuousRadarSkill(client, memory=memory, radar=radar)

        if args.radar:
            radar_skill.enable()
            print("[Radar] Continuous exploration radar enabled.")

        debugger = ChatDebugger(client, radar_skill=radar_skill, memory=memory) if args.debug else None

        print("BotSleep running. Press Ctrl+C to stop.")
        while True:
            if debugger:
                debugger.poll_and_dispatch()
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nStopping BotSleep...")
        client.chat(f"{args.username} logging off. Bye!")
        client.logout()
        recorder.record("INFO", "main", "SHUTDOWN", {"reason": "KeyboardInterrupt"})
    except Exception as e:
        print(f"Error: {e}")
        recorder.record("ERROR", "main", "CRASH", {"error": str(e)})
        client.logout()


if __name__ == "__main__":
    main()
