"""Test Crafting Skill: Craft Wooden Pickaxe end-to-end"""

import sys
import os
import json
import logging

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.client import MinecraftAgentClient
from core.memory import MemoryEngine
from skills.crafting import CraftItemSkill

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

def main():
    if os.environ.get("BOTSLEEP_RUN_LIVE_TESTS") != "1":
        raise SystemExit("Live RCON crafting fixture disabled; use a disposable world and set BOTSLEEP_RUN_LIVE_TESTS=1")
    if os.environ.get("MC_AGENT_USER", "SleepBot") != "SleepBot":
        raise SystemExit("This fixed fixture requires MC_AGENT_USER=SleepBot")
    from mcrcon import MCRcon
    client = MinecraftAgentClient(base_url=os.environ["MC_AGENT_API"], username="SleepBot",
                                   password=os.environ["MC_AGENT_PASSWORD"])
    rcon_password = os.environ["MC_RCON_PASSWORD"]
    print("=== Step 1: Connecting RCON and providing test materials (8 oak_log) ===")
    with MCRcon(os.environ.get("MC_RCON_HOST", "127.0.0.1"), rcon_password,
                port=int(os.environ.get("MC_RCON_PORT", "25575"))) as mcr:
        # Clear inventory to have a pristine test slate
        mcr.command("clear SleepBot")
        mcr.command("setblock -8 63 -3 air")
        res = mcr.command("give SleepBot oak_log 8")
        print("RCON Give response:", res)

    print("\n=== Step 2: Logging in SleepBot via mc-agent-api ===")
    client.login()

    init_inv = client.get_inventory_counts()
    print("Initial Inventory:", init_inv)
    assert init_inv.get("oak_log", 0) >= 8, f"Expected 8 oak_log, found {init_inv}"

    print("\n=== Step 3: Executing CraftItemSkill for 'wooden_pickaxe' ===")
    memory = MemoryEngine()
    craft_skill = CraftItemSkill(client, memory=memory)

    res = craft_skill.execute(item="wooden_pickaxe", count=1, retrieve_table=True)
    print("\n=== Step 4: Crafting Execution Result ===")
    print("Success:", res.success)
    print("Message:", res.message)
    print("Data:", json.dumps(res.data, indent=2))

    print("\n=== Step 5: Final Inventory Verification ===")
    final_inv = client.get_inventory_counts()
    print("Final Inventory:", final_inv)
    assert final_inv.get("wooden_pickaxe", 0) >= 1, f"Expected wooden_pickaxe in inventory! Got: {final_inv}"
    print("\n[SUCCESS] Wooden Pickaxe crafted successfully and verified in inventory!")

if __name__ == "__main__":
    main()
