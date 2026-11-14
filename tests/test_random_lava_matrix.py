"""20-Round Automated Nether Portal Speedrun Test Matrix on Randomized Lava Lakes.

Tests BotSleep's autonomous Nether Portal speedrun capability across 20 randomized trials
with varying lake dimensions, organic shoreline noise, varied rim materials,
trees, and 20-block approach paths.

Verification Criteria per Trial:
  1. Complete Nether Portal Frame (10 obsidian blocks).
  2. All 6 interior blocks ignited (nether_portal).
  3. Zero damage taken: Health = 20/20 HP, on_fire = False, pure survival mode.
  4. 100% water containment: Zero water spill outside basin.
  5. Step-by-step player and block telemetry logged to JSON.

Pass Target: >= 90% (>= 18 / 20 successes).
"""

import sys
import os
sys.path.insert(0, os.path.abspath("E:/Program Save/VisualCode/BotSleep"))

import time
import json
import logging
from datetime import datetime
from typing import Dict, Any, List

# Configure logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LavaMatrixTest")

from core.client import MinecraftAgentClient
from core.rcon import RconManager
from skills.lava_lake_generator import LavaLakeGenerator
from skills.portal_speedrun import NetherPortalSpeedrunSkill
from skills.portal_telemetry import PortalTelemetryRecorder


def run_single_trial(
    trial_idx: int,
    cx: int,
    cy: int,
    cz: int,
    client: MinecraftAgentClient,
    rcon: RconManager,
) -> Dict[str, Any]:
    """Runs a single speedrun trial on a freshly generated randomized lava lake."""
    trial_id = f"matrix_trial_{trial_idx:02d}"
    logger.info(f"\n{'='*75}\n🏁 STARTING TRIAL #{trial_idx:02d} / 20 at [{cx}, {cy}, {cz}] (ID: {trial_id})\n{'='*75}")

    # 1. Generate randomized lava lake & terrain
    gen = LavaLakeGenerator(rcon)
    seed = trial_idx * 137 + 42
    lake_meta = gen.generate_random_lake(
        center_x=cx,
        center_y=cy,
        center_z=cz,
        seed=seed,
        create_trees=True,
    )

    start_pos = lake_meta["start_20_blocks"]
    approach_shore = lake_meta["approach_shore"]
    baseline = lake_meta["casting_baseline"]
    x0, y0, z0 = baseline["x0"], baseline["y0"], baseline["z0"]

    # 2. Initialize Telemetry Recorder
    telemetry = PortalTelemetryRecorder(client, trial_id=trial_id)
    skill = NetherPortalSpeedrunSkill(client, telemetry=telemetry)

    # 3. Setup SleepBot 20 blocks away in pure survival mode
    rcon.command("clear SleepBot")
    rcon.command("effect clear SleepBot")
    rcon.command("gamemode survival SleepBot")
    rcon.command(f"tp SleepBot {start_pos[0]+0.5} {cy+1.0} {start_pos[2]+0.5} 0 0")
    rcon.command("effect give SleepBot instant_health 1 10 true")
    rcon.command("give SleepBot cobblestone 32")
    rcon.command("give SleepBot water_bucket 2")
    rcon.command("give SleepBot bucket 1")
    rcon.command("give SleepBot lava_bucket 6")
    rcon.command("give SleepBot diamond_pickaxe 1")
    rcon.command("give SleepBot flint_and_steel 1")
    time.sleep(1.0)

    # Wait for bot to be ready after teleport
    for _ in range(30):
        st = client.get_status().get("result", {})
        pos = st.get("pos", [])
        if st.get("ready") is True and pos and pos[1] >= cy:
            break
        time.sleep(0.3)

    init_st = client.get_status().get("result", {})
    init_hp = init_st.get("health", 0)
    logger.info(f"SleepBot ready at start: Pos={init_st.get('pos')}, HP={init_hp}")
    rcon.command(f'tellraw @a {{"text":"[SleepBot] 正在进行第 {trial_idx}/20 场随机地形速通测试...","color":"gold"}}')

    trial_start_time = time.time()
    failure_reason = None

    try:
        # 4. Phase 1: Approach from 20 blocks away to lake shore / pier
        mid_z = (start_pos[2] + approach_shore[2]) // 2
        telemetry.record_step(
            step_id="approach_midway",
            description="行进至前进步道中段",
            target_pos=[start_pos[0], cy, mid_z],
            action_func=lambda: client.move_to(start_pos[0] + 0.5, None, mid_z, range=1.5),
        )
        telemetry.record_step(
            step_id="approach_shore",
            description="行进至岩浆湖岸边",
            target_pos=approach_shore,
            action_func=lambda: client.move_to(approach_shore[0] + 0.5, None, approach_shore[2] - 0.5, range=1.2),
        )
        telemetry.record_step(
            step_id="approach_pier",
            description="走到浇筑岸台边缘直面基线",
            target_pos=[x0 + 2, y0, z0 - 1],
            action_func=lambda: client.move_to(x0 + 1.6, None, z0 - 0.8, range=0.8),
        )

        # Ensure SleepBot arrived near pier before attempting Step 1
        st_pier = client.get_status().get("result", {})
        p_pier = st_pier.get("pos", [])
        dist_pier = ((p_pier[0] - (x0 + 1.6))**2 + (p_pier[2] - (z0 - 0.8))**2)**0.5 if p_pier else 99
        if dist_pier > 2.0:
            logger.warning(f"Bot slightly off pier (dist={dist_pier:.2f}), retrying move to pier...")
            client.move_to(x0 + 1.6, None, z0 - 0.8, range=0.8)
            time.sleep(0.4)

        # 5. Phase 2: Execute Step 1 (Place stone, place water, move to red sand)
        res1 = skill.execute_step_1(
            start_sand_pos=[x0 + 1, y0 + 1, z0 - 1],
            stone_pos=[x0 + 2, y0, z0],
            water_pos=[x0 + 1, y0, z0],
            red_sand_pos=[x0 + 1.6, y0 + 1.0, z0 + 1.0],
        )
        if not res1.success:
            raise RuntimeError(f"Step 1 failed: {res1.message}")

        # 6. Phase 3: Execute Step 2 (Dig stone, verify/cast bottom obsidian, scoop bottom water)
        res2 = skill.execute_step_2(
            stone_pos=[x0 + 2, y0, z0],
            base_positions=[[x0 + 1, y0 - 1, z0], [x0 + 2, y0 - 1, z0]],
            red_sand_pos=[x0 + 1.6, y0 + 1.0, z0 + 1.0],
            water_placement_pos=[x0 + 1, y0, z0],
        )
        if not res2.success:
            raise RuntimeError(f"Step 2 failed: {res2.message}")

        # 7. Phase 4: Build Scaffolding & Water Curtain
        res_scaff = skill.build_scaffolding(x0=x0, y0=y0, z0=z0)
        if not res_scaff.success:
            raise RuntimeError(f"Scaffolding failed: {res_scaff.message}")

        # 8. Phase 5: Execute Step 3 (6 Obsidian pours, scoop water, clear puddle, ignite)
        res3 = skill.execute_step_3(x0=x0, y0=y0, z0=z0, temp_stand_pos=None)
        if not res3.success:
            raise RuntimeError(f"Step 3 failed: {res3.message}")

    except Exception as e:
        logger.error(f"❌ [TRIAL {trial_idx:02d} FAILED] Exception: {e}")
        failure_reason = str(e)

    trial_duration = round(time.time() - trial_start_time, 2)

    # 9. Verification Checks
    final_st = client.get_status().get("result", {})
    final_hp = final_st.get("health", 0.0)
    final_fire = final_st.get("on_fire", False)

    # Check 10 Frame Obsidian Blocks
    frame_coords = [
        [x0 + 1, y0 - 1, z0], [x0 + 2, y0 - 1, z0],
        [x0 + 0, y0 + 1, z0], [x0 + 0, y0 + 2, z0],
        [x0 + 3, y0 + 1, z0], [x0 + 3, y0 + 2, z0],
        [x0 + 1, y0 + 3, z0], [x0 + 2, y0 + 3, z0],
        [x0 + 0, y0 + 0, z0], [x0 + 3, y0 + 0, z0],
    ]
    frame_blocks = [client.get_block(c[0], c[1], c[2]).get("result", {}).get("name") for c in frame_coords]
    frame_obsidian_count = sum(1 for b in frame_blocks if b == "obsidian")

    # Check 6 Interior Portal Blocks
    interior_coords = [
        [x0 + 1, y0 + 0, z0], [x0 + 1, y0 + 1, z0], [x0 + 1, y0 + 2, z0],
        [x0 + 2, y0 + 0, z0], [x0 + 2, y0 + 1, z0], [x0 + 2, y0 + 2, z0],
    ]
    interior_blocks = [client.get_block(c[0], c[1], c[2]).get("result", {}).get("name") for c in interior_coords]
    portal_blocks_count = sum(1 for b in interior_blocks if b == "nether_portal")

    # Check Water Spill
    water_spill = False
    spill_blocks = []
    for dy in range(4):
        for dx in range(-1, 5):
            for dz in range(-2, 4):
                bn = client.get_block(x0 + dx, y0 + dy, z0 + dz).get("result", {}).get("name")
                if bn in ["water", "flowing_water"]:
                    water_spill = True
                    spill_blocks.append({"pos": [x0 + dx, y0 + dy, z0 + dz], "block": bn})

    # Evaluate Pass/Fail
    passed = (
        failure_reason is None
        and portal_blocks_count == 6
        and frame_obsidian_count == 10
        and final_hp == 20.0
        and not final_fire
        and not water_spill
    )

    if not passed and not failure_reason:
        reasons = []
        if portal_blocks_count < 6:
            reasons.append(f"Portal blocks {portal_blocks_count}/6")
        if frame_obsidian_count < 10:
            reasons.append(f"Frame obsidian {frame_obsidian_count}/10")
        if final_hp < 20.0:
            reasons.append(f"HP damaged ({final_hp}/20)")
        if final_fire:
            reasons.append("Bot caught fire")
        if water_spill:
            reasons.append(f"Water spill detected ({len(spill_blocks)} blocks)")
        failure_reason = ", ".join(reasons)

    # Dump Telemetry JSON
    telemetry_path = telemetry.dump_json()

    outcome_str = "PASSED" if passed else "FAILED"
    logger.info(
        f"\n{'='*75}\n"
        f"📊 TRIAL #{trial_idx:02d} RESULT: [{outcome_str}]\n"
        f"  Duration: {trial_duration}s\n"
        f"  Portal Blocks: {portal_blocks_count} / 6\n"
        f"  Obsidian Frame: {frame_obsidian_count} / 10\n"
        f"  Health: {final_hp} / 20 HP (OnFire={final_fire})\n"
        f"  Water Spill: {len(spill_blocks)} blocks\n"
        f"  Failure Reason: {failure_reason or 'None'}\n"
        f"  Telemetry Log: {telemetry_path}\n"
        f"{'='*75}\n"
    )

    rcon_msg = (
        f'[SleepBot] 第 {trial_idx}/20 场测试: {"✅ 成功" if passed else "❌ 失败"} '
        f'(耗时 {trial_duration}s, HP={final_hp})'
    )
    rcon_color = "green" if passed else "red"
    rcon.command(f'tellraw @a {{"text":"{rcon_msg}","color":"{rcon_color}"}}')

    return {
        "trial_idx": trial_idx,
        "trial_id": trial_id,
        "passed": passed,
        "duration_s": trial_duration,
        "center": [cx, cy, cz],
        "rx": lake_meta["rx"],
        "rz": lake_meta["rz"],
        "portal_blocks_count": portal_blocks_count,
        "frame_obsidian_count": frame_obsidian_count,
        "final_hp": final_hp,
        "final_fire": final_fire,
        "water_spill": water_spill,
        "spill_blocks": spill_blocks,
        "failure_reason": failure_reason,
        "telemetry_path": telemetry_path,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=20, help="Number of trials to run (default 20)")
    parser.add_argument("--start", type=int, default=1, help="Starting trial index")
    args = parser.parse_args()

    rcon = RconManager("127.0.0.1", 25575, "agent-rcon-pass")
    rcon.connect()

    client = MinecraftAgentClient("http://127.0.0.1:18900/api/v1")
    client.login()
    time.sleep(1.0)

    TOTAL_TRIALS = args.trials
    results: List[Dict[str, Any]] = []

    logger.info(f"🚀 Starting {TOTAL_TRIALS}-Round Nether Portal Speedrun Automated Test Matrix...")
    start_matrix_time = time.time()

    for offset in range(TOTAL_TRIALS):
        trial_idx = args.start + offset
        # Compute coordinates on an 80-block grid
        row = (trial_idx - 1) // 5
        col = (trial_idx - 1) % 5
        cx = 100 + col * 80
        cz = 100 + row * 80
        cy = 64

        trial_res = run_single_trial(
            trial_idx=trial_idx,
            cx=cx,
            cy=cy,
            cz=cz,
            client=client,
            rcon=rcon,
        )
        results.append(trial_res)

        # Brief cooldown between trials
        time.sleep(1.5)

    total_matrix_time = round(time.time() - start_matrix_time, 2)
    pass_count = sum(1 for r in results if r["passed"])
    fail_count = TOTAL_TRIALS - pass_count
    pass_rate = round((pass_count / TOTAL_TRIALS) * 100, 1)

    logger.info("\n" + "=" * 80)
    logger.info(f"🏆 20-ROUND TEST MATRIX SUMMARY (Total Time: {total_matrix_time}s)")
    logger.info(f"  Passed: {pass_count} / {TOTAL_TRIALS}")
    logger.info(f"  Failed: {fail_count} / {TOTAL_TRIALS}")
    logger.info(f"  Pass Rate: {pass_rate}% (Target: >= 90.0%)")
    logger.info("=" * 80)

    # Print summary table
    print("\n| Trial | Result | Time | HP | Frame | Portal | Lake Rx/Rz | Reason |")
    print("|-------|--------|------|----|-------|--------|------------|--------|")
    for r in results:
        status_sym = "✅ PASS" if r["passed"] else "❌ FAIL"
        reason_str = r["failure_reason"] or "-"
        print(f"| {r['trial_idx']:02d} | {status_sym} | {r['duration_s']}s | {r['final_hp']} | {r['frame_obsidian_count']}/10 | {r['portal_blocks_count']}/6 | {r['rx']}x{r['rz']} | {reason_str} |")

    # Save summary report
    summary_report_path = os.path.abspath("logs/telemetry/matrix_summary.json")
    with open(summary_report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "total_trials": TOTAL_TRIALS,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": pass_rate,
            "total_matrix_time_s": total_matrix_time,
            "results": results,
        }, f, ensure_ascii=False, indent=2)

    logger.info(f"\n📄 Complete test matrix report written to: {summary_report_path}")

    rcon_summary = (
        f'tellraw @a {{"text":"[SleepBot] 🏆 20轮自动化测试完成！通过率: {pass_rate}% ({pass_count}/{TOTAL_TRIALS})","color":"gold","bold":true}}'
    )
    rcon.command(rcon_summary)

    if pass_rate >= 90.0:
        logger.info("🎉 TARGET ACHIEVED: Pass rate >= 90%!")
        sys.exit(0)
    else:
        logger.error(f"❌ Target not met: {pass_rate}% < 90%")
        sys.exit(1)


if __name__ == "__main__":
    main()
