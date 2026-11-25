# BotSleep v2 Agent Self-Execution Playbook (Internal Runbook)

> **Audience**: Autonomous Coding Agent (Antigravity) executing the `develop-tianya/rewrite-v2` rewrite across turns/sessions.
> **Companion Spec**: [REWRITE_V2_MASTERPLAN.md](REWRITE_V2_MASTERPLAN.md)

---

## 0. Progress Ledger (Update In-Place After Every Phase)

| Phase | Scope | Gate Command | Status | Commit |
|---|---|---|---|---|
| **P0** | Branch, `v2/` skeleton, AST layer guard, `VirtualMcApiServer` simulator | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2 -v` | `DONE` | (see git log) |
| **P1** | `v2/kernel/` (`config`, dual-pool `client`, WAL `storage`/`journal`, `model_gate`) | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2/kernel -v` | `DONE` | (see git log) |
| **P2** | `v2/gateway/` (`VirtualWorldGateway`, `BlockUnreachableTracker`, `Pathfinder3D`, `MultiHopNavigator`, `SurfaceSwimExecutor`) | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2/gateway -v` | `DONE` | (see git log) |
| **P3** | `v2/bt/` (`kernel`, `blackboard`, `watchdog`, `hard_safety`, `local_recovery`, `goal_mount`, `runtime`) | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2/bt -v` | `DONE` | (see git log) |
| **P4** | `v2/primitives/` (8 orthogonal primitives) + `v2/tasks/` (`TaskCatalogue` 886-DAG, stateless recursive tasks, `competition` bridge) | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2/tasks -v` | `IN_PROGRESS` | — |
| **P5** | `v2/planner/` (`DepsPlanner`, `HorizonSelector`, `BM25SkillRetriever`, `SemanticCompactor`, `IntentCompiler`, `SerialModelWorker`) + `skills_kb/*.md` | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2/planner -v` | `PENDING` | — |
| **P6** | Cutover `main.py` (`bot_sleep.py` & `client.py` shims preserved), `v2/observability/`, archive legacy v1 dead modules, sync `docs/` | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2 -v && .\.venv\Scripts\python.exe main.py --check-config` | `PENDING` | — |

---

## 1. Non-Negotiable Agent Prime Directives (Check Before Writing Any Code)

1. **NEVER touch `e:\Program Save\VisualCode\mc-agent-api`**. Read-only inspection only.
2. **Preserve Root Compatibility Shims**: `bot_sleep.py` (`from main import main`) and `client.py` (`from v2.kernel.client import ...`) MUST remain thin forwarding files per `AGENTS.md`.
3. **Zero Runtime Dependencies Added**: Only Python 3.10+ stdlib + `requests` + `PyYAML` (optional `javac`/`java` only for `ninjabrain`).
4. **Strict Single-Direction Layer Imports** (enforced by `tests_v2/test_00_layer_boundaries.py`):
   - `v2.kernel` (L0) imports NOTHING from `v2.{gateway,bt,primitives,tasks,planner,observability}`.
   - `v2.gateway` (L1) imports ONLY from `v2.kernel` (and pure domain constants).
   - `v2.bt` (L2) imports ONLY from `v2.kernel`, `v2.gateway` (and typed protocols).
   - `v2.primitives` & `v2.tasks` (L3) import ONLY from `v2.kernel`, `v2.gateway`, `v2.bt`.
   - `v2.planner` (L4) may import from L0–L3; `v2.observability` is read-only over snapshots/JSONL.
5. **Pure-Memory `stop(bb)` Rule**:
   - `Node.stop(bb)` and `Task.stop(bb)` MUST NEVER perform network I/O (`client.*` calls are strictly forbidden inside `stop()`).
   - Why: When a write returns `action_state="unknown"`, `PendingUnknownGuard` preempts the active goal on the next tick and calls `stop(bb)`. Issuing `client.stop()` or `client.posture()` inside `stop()` would violate the unknown-write freeze invariant.
6. **Single-Write Macro-Tick Contract**:
   - Every L3 leaf execution in one BT tick executes **at most ONE** write endpoint call, preceded by `journal.begin_write(...)` and followed by `journal.resolve_write(...)` + post-condition verification.
   - Multi-block structures (shelter, mineshaft stairs, portal frame) MUST inspect world state via `VirtualWorldGateway` and emit ONE next step per tick—no 180-second internal `while` loops hiding from L2 `HardSafetyBranch`.
7. **Dual HTTP Session Isolation**:
   - Action writes use `client._action_session` (single thread).
   - Read-only parallel calls (`status`, `inventory`, `get_entities`, `events`, `find_blocks`, `get_block`) use `threading.local()` `requests.Session` instances inside `AsyncReadPool`. Never close or mutate `_action_session` from a read worker.
8. **Competition Scoring Honesty**:
   - Keep the exact 5 competition tool names: `select_task`, `update_progress`, `finish_task`, `force_exit_task`, `end_competition`.
   - Server `/advancements` (`status == "completed"`) is the ONLY scoring truth; conditional advancements (54 items) remain unselectable unless explicitly enabled in `eligibility_overrides`. Never report simulation pass as live server pass.

---

## 2. Upstream `mc-agent-api` Ground-Truth Cheat Sheet (Verified from `src/session.js` & `src/dispatch.js`)

Do NOT hallucinate API fields. Always code against these exact upstream contracts:

- **Envelope**: HTTP 200 `{ "ok": true, "ms": 3, "result": {...} }` or `{ "ok": false, "ms": 3, "error": { "code": "...", "message": "...", ...extra } }`. (`/auth/login` and `/auth/logout` put `token` / `ok` at top level without `result`.)
- **`GET /status` -> `result`**:
  - Coordinates: `pos: [x, y, z]` (1-decimal floats, **NOT** `position` dict!).
  - Motion: `speed: float|null`, `velocity: [vx, vy, vz]|null`, `yaw: float`, `on_ground: bool`.
  - Vitals & flags: `alive: bool`, `ready: bool`, `busy: str|null`, `dimension: str`, `health: float`, `food: int`, `saturation: float`, `oxygen: int` (1.21 max is `20`, `-1` taking drown damage), `armor_points: int`, `on_fire: bool`, `in_water: bool`, `submerged: bool`, `in_lava: bool`, `is_sleeping: bool`, `in_vehicle: {id,name}|null`, `sneaking: bool`, `jumping: bool`, `blocking: bool`, `held: {...}|null`, `offhand: {...}|null`, `nearest_hostile: {id,name,dist}|undefined`, `last_death: {pos,t,cause}|undefined`.
- **`GET /inventory` -> `result`**:
  - `items: [{name, count, slot, durability_used?, enchantments?, potion?, custom_name?}]`
  - `armor: {head, chest, legs, feet}` (**note**: `chest`, whereas `/equip` slot accepts `hand` / `off-hand` / `head` / `torso` / `legs` / `feet`—keep `armor.chest <-> torso` mapping!).
  - `offhand`, `held`, `empty_slots: int`.
- **`POST /get_block {"pos": [x, y, z]}` -> `result`**:
  - `{name: str, pos: [x, y, z], properties: dict, diggable: bool, hardness: float, harvest_tool_ok: bool, ...cropInfo}`. Fails with `not_loaded` if chunk unloaded.
- **`POST /find_blocks`**:
  - **Skips `air`, `cave_air`, `void_air` and non-exposed blocks!**
  - With `types: [...]`: `{total: int, blocks: [{name, pos: [x, y, z], dist, properties, ...}]}`.
  - Without `types`: `{radius, scanned, blocks: {block_name: {count, nearest: [x, y, z], dist}}}` (summary dict, NOT list!).
- **`GET /events?since=<seq>&limit=50` -> `result`**:
  - `{since, last_seq, total, has_more, events: [{seq, t, type, ...}]}` where `type` in `hurt` (`source`, `source_entity`, `damage`, `health`), `death` (`pos`, `dropped_items_at`), `pickup` (`item`, `count`), `sleep`, `wake`, `chat`, `kicked`, `disconnected`.
- **`POST /move_to`**:
  - Accepts `{x, y?, z, range=1, timeout=60000, stop_on?}` or `{entity_id, range=2, timeout=60000}`.
  - Upstream `Movements` sets `canDig=false`, `allow1by1towers=false`, `scafoldingBlocks=[]`, `stallMs=8000`, `thinkTimeout=15000`. Upstream **ignores** `allow_partial`!
- **`POST /interact` (Buckets, Beds, Doors, Villagers, Flint & Steel)**:
  - Empty bucket pickup: `{"pos": [x, y, z], "item": "bucket"}` where `pos` is the liquid source block (`level == "0"`). Upstream aims at **`(x+0.5, y+0.85, z+0.5)`**.
  - Full bucket pour (`water_bucket` / `lava_bucket`): `{"pos": [x, y, z], "item": "lava_bucket"}` where `pos` is target air cell over solid block `(x, y-1, z)` (upstream aims at **`(x+0.5, y-0.01, z+0.5)`**).
  - Flint & steel portal ignition: `{"pos": [x, y, z], "item": "flint_and_steel"}` clicking the bottom obsidian inner face / block below portal air.
- **`WRITE_ENDPOINTS` (Require write-ahead `pending_tx` if side-effecting)**:
  - `move_to`, `look`, `posture`, `equip`, `window/view`, `window/act`, `attack`, `dig`, `craft`, `place`, `interact`, `use_item`, `control`, `shoot`, `drop`, `chat`.

---

## 3. Target Directory Architecture & Line Budgets

```text
v2/
├── __init__.py
├── domain.py                      # Pure constants, Coord3/Pos types, block sets, TypedDicts (from core/domain.py + structure_spec.py)
├── kernel/                        # L0: Zero game logic; pure transport, locks, WAL persistence
│   ├── __init__.py
│   ├── config.py                  # Purify core/config.py (YAML + env + CLI overrides)
│   ├── client.py                  # Purify core/client.py + AsyncReadPool (threading.local() read sessions)
│   ├── storage.py                 # UnifiedWorldDB (SQLite WAL: pending_tx, voxel_chunks, landmarks, unreachable, checkpoints)
│   ├── journal.py                 # PendingTransactionJournal + instance_lock()
│   └── model_gate.py              # Purify core/model_gate.py (FIFO lock + 30s transport window + per-provider slot)
├── gateway/                       # L1: Virtualize restricted HTTP into cached 3D world + multi-hop navigation
│   ├── __init__.py
│   ├── unreachable.py             # BlockUnreachableTracker ((dim, pos, action) failure counter + TTL)
│   ├── world_proxy.py             # VirtualWorldGateway (3-source voxel cache + write-through invalidation + exact bucket LOS)
│   ├── pathfinder_3d.py           # Promote core/pathfinder_3d.py + cave_probe.py + avoid_block_breaking predicate
│   ├── surface_swim.py            # SurfaceSwimExecutor (posture jump + bounded control pulses + oxygen guard)
│   └── multihop_navigator.py      # MultiHopNavigator (18-24m relay slicing + Pathfinder3D micro-dig/bridge/pillar step)
├── bt/                            # L2: Zero-dependency reactive Behavior Tree + Blackboard
│   ├── __init__.py
│   ├── blackboard.py              # Namespaced Blackboard + bounded ActivityStream
│   ├── kernel.py                  # Status, Node (pure-memory stop()), PrioritySelector(memory=False), Sequence
│   ├── observe_node.py            # SyncBlackboard (1-RTT concurrent read + /events cursor + VoxelWorld3D ingest)
│   ├── watchdog.py                # Promote core/loop_supervisor.py (action timeout + cross-step oscillation cooldown)
│   ├── goal_mount.py              # ActiveGoalMount (mounts L3 stateless Task tree, enforces 1-write-per-tick)
│   ├── runtime.py                 # BTRuntime tick loop (sleep / starter / daily / competition assembly)
│   └── branches/
│       ├── __init__.py
│       ├── hard_safety.py         # PendingUnknownGuard + Lava/Fire + Drown + ThreatAssessment (with canopy/burrow ground-mob exemption)
│       └── local_recovery.py      # UrgentEat + WaterShoreEscape + CanopyDescend + BurrowExit + AutoSleep + SafeDropPickup
├── primitives/                    # L3a: 8 Orthogonal Atomic Action Primitives (each <= 1 write per call)
│   ├── __init__.py
│   ├── base.py                    # ActionOutcome, PrimitiveContext, write-ahead journal wrapper
│   ├── navigate.py                # NavigatePrimitive
│   ├── dig_collect.py             # DigCollectPrimitive (logs, ores, gravel->flint, foliage, vein finish)
│   ├── place_build.py             # PlaceBuildPrimitive (stations, oriented beds/doors, single-block structure step)
│   ├── craft.py                   # CraftPrimitive (2x2 / 3x3 + 16m table reuse, no duplicate crafting_table)
│   ├── container.py               # ContainerPrimitive (2-3 furnace parallel batch smelt + double chest + loot)
│   ├── fluid.py                   # FluidPrimitive (y+0.85 pickup + y-0.01 pour + occluder clear hint)
│   ├── combat.py                  # CombatPrimitive (melee step, shield guard/release, bow/crossbow, animal hunt)
│   └── utility.py                 # UtilityPrimitive (eat, auto_equip_best, sleep/wake, drop_junk, throw_eye)
├── tasks/                         # L3b: AltoClef Stateless Recursive Tasks ("20 Questions")
│   ├── __init__.py
│   ├── base.py                    # Stateless Task ABC (is_finished, on_tick, pure-memory stop) + MacroSequenceTask
│   ├── catalogue.py               # TaskCatalogue (compiles 886 recipes + mc_tree_crafting + smelt + gather + competition DAG)
│   ├── acquire.py                 # AcquireItemTask(item, count) + SmeltItemTask (auto-pruning recursive solver)
│   ├── explore.py                 # ExploreUntilTask(predicate, max_distance, sector_spiral)
│   ├── mining.py                  # MineResourceTask (cave-first iron + Y=16 / Y=-54 step-driven shaft + return to surface)
│   ├── shelter.py                 # BuildShelterTask (4-orientation StructureSpec idempotent block-by-block completion)
│   ├── nether_portal.py           # EnterNetherTask (water_bucket -> flint_and_steel -> lava_pool -> multihop -> cast & ignite -> cross)
│   ├── stronghold_end.py          # StrongholdLocateTask (2-3 eye TLS/PCA + Ninjabrain) + EndCampaignTask
│   └── competition/               # Competition lifecycle & server advancement verification
│       ├── __init__.py
│       ├── rules.py               # From core/competition_rules.py
│       └── manager.py             # From core/competition_tasks.py + TaskCatalogue route readiness
├── planner/                       # L4: Low-Token DEPS Cognitive Layer (<= 6.6KB per request)
│   ├── __init__.py
│   ├── llm_client.py              # From agent/brain.py (OpenAIBrain + TLS1.2 + primary/overflow routing)
│   ├── model_worker.py            # From core/model_worker.py (non-blocking SerialModelWorker)
│   ├── skill_retriever.py         # Zero-dep BM25 + Blackboard tag retriever over skills_kb/*.md
│   ├── deps_explainer.py          # 3-line structured failure root-cause + alternative horizon synthesis
│   ├── horizon_selector.py        # Cost estimator + speedrun gate (iron_pickaxe / 4 iron -> prioritize EnterNetherTask)
│   ├── compactor.py               # Pi/OMP SemanticCompactor (ProgressCard + last 2 complete tool pairs)
│   ├── intent_compiler.py         # Purify core/intent_compiler.py + single-source tool schema generator
│   └── deps_planner.py            # Main L4 planner + AsyncLLMReplanNode
└── observability/                 # Read-only flight recorder, HTTP monitor & chat debugger
    ├── __init__.py
    ├── logger.py                  # FlightRecorder + BT preemption & Blackboard events
    ├── monitor.py                 # Read-only dashboard (BT active branch, cache hit %, token ratio, competition board)
    ├── monitor_tail.py            # Offline JSONL replay tailer
    └── chat_debugger.py           # Whitelisted #stop / #status / #resume / #task mount
```

---

## 4. Phase-by-Phase Execution Instructions

### Phase 0: Workspace Skeleton, Layer Linter & `VirtualMcApiServer`
1. Ensure `v2/` subpackages, `skills_kb/`, and `tests_v2/` exist with `tests_v2/test_00_layer_boundaries.py` enforcing AST import direction.
2. Build `tests_v2/sim/virtual_mc_api.py` (`VirtualMcApiServer`):
   - In-memory 3D voxel dictionary + player physics state (`pos`, `health`, `food`, `oxygen`, `dimension`, `inventory`, `advancements`, `events`).
   - Faithfully reproduces upstream quirks: `find_blocks` skips air & non-exposed blocks; `interact(item="bucket")` raycasts to `(x+0.5, y+0.85, z+0.5)` and fails if occluded; `move_to` fails with `path_not_found` across walls/chasms (`canDig=false`) but has NO 32-block artificial error; `dig` updates voxel & drops/gains items.
3. Add baseline scenario fixtures in `tests_v2/sim/scenarios.py`:
   - `scenario_v31_64m_lava_portal`: Player with iron_pickaxe + water_bucket + gravel + iron_ingot, lava pool at distance 54m.
   - `scenario_v31_sloped_water_bank`: Water source recessed 1 block below grass bank lip causing `y+0.85` occlusion from flat shore.
   - `scenario_v30_deep_pit_and_canopy`: Player at `y=43` shaft bottom; player at `y=78` canopy with zombie on ground `y=64`.

### Phase 1: L0 Kernel (`v2/kernel/`)
1. Port `core/config.py` → `v2/kernel/config.py` cleanly.
2. Build `v2/kernel/storage.py` (`UnifiedWorldDB` with SQLite WAL, `busy_timeout=5000`, thread-serialized writer lock) and `v2/kernel/journal.py` (`PendingTransactionJournal` + `instance_lock`).
3. Port `core/model_gate.py` → `v2/kernel/model_gate.py` keeping 100% of FIFO queue + 30s transport expiry + operator `acknowledge_provider_idle`.
4. Port `core/client.py` → `v2/kernel/client.py` adding `AsyncReadPool` (`threading.local()` `requests.Session` per reader thread, `observe_tick_bundle`, `batch_get_blocks`).
5. Write `tests_v2/kernel/` covering WAL concurrency, write-ahead unknown freeze, read-pool session isolation, and `ModelRequestGate` multi-thread serialization.

### Phase 2: L1 Virtual World Gateway (`v2/gateway/`)
1. Implement `BlockUnreachableTracker` (`v2/gateway/unreachable.py`) backed by `UnifiedWorldDB`.
2. Adapt `VoxelWorld3D` (`core/world_3d.py`) + `VirtualWorldGateway` (`v2/gateway/world_proxy.py`):
   - Ingest `find_blocks` typed results (`SCAN`), `get_block` (`EXACT`), and player stance (`INFERRED_PASSABLE`).
   - Write-through invalidation on `dig`/`place` and `3x3x3` invalidation on `unknown`.
   - Exact `_ray_cells` line-of-sight solver for `y+0.85` liquid pickup & `y-0.01` pour, returning either valid stance or single removable occluder block.
3. Port `core/cave_probe.py` + `core/pathfinder_3d.py` → `v2/gateway/pathfinder_3d.py`, adding `avoid_block_breaking` predicate.
4. Implement `SurfaceSwimExecutor` (`v2/gateway/surface_swim.py`) and `MultiHopNavigator` (`v2/gateway/multihop_navigator.py`).
5. Verify against `VirtualMcApiServer`: 64m obstacle/river navigation + `<= 4` HTTP reads per step on cached terrain.

### Phase 3: L2 Reactive Behavior Tree (`v2/bt/`)
1. Implement `Blackboard`, `Status`, `Node` (enforcing pure-memory `stop()`), `PrioritySelector`, `Sequence`.
2. Implement `SyncBlackboard`, `LoopWatchdog` (from `core/loop_supervisor.py`), `HardSafetyBranch` (from `core/safety_arbiter.py` + canopy/burrow unreachable ground-mob exemption), `LocalRecoveryBranch`, `ActiveGoalMount`, and `BTRuntime`.
3. Verify preemption invariants in `tests_v2/bt/`:
   - Active goal mid-progress -> skeleton enters threat range -> `HardSafetyBranch` preempts (`stop()` issues 0 HTTP writes) -> threat cleared -> stateless goal resumes cleanly from updated blackboard.
   - Leaf action raises `unknown` write -> `PendingUnknownGuard` freezes tree on next tick -> zero subsequent writes emitted.

### Phase 4: L3 Primitives, `TaskCatalogue` & Stateless Recursive Tasks (`v2/primitives/` & `v2/tasks/`)
1. Implement the 8 orthogonal primitives in `v2/primitives/` wrapping `PendingTransactionJournal` + `VirtualWorldGateway` post-condition verification.
2. Build `TaskCatalogue` (`v2/tasks/catalogue.py`) compiling `data/recipes.json` (886 items) + `data/mc_tree_crafting.json` + smelting rules + mining Y-bands + `data/competition/rules_v2.json`.
3. Implement stateless `AcquireItemTask`, `ExploreUntilTask`, `MineResourceTask`, `BuildShelterTask`, `EnterNetherTask`, `StrongholdLocateTask`, and `v2/tasks/competition/`.
4. Run full end-to-end simulation in `tests_v2/tasks/`: empty-handed spawn -> wood -> stone pickaxe -> 2-furnace batch iron smelt -> `iron_pickaxe` + `water_bucket` + `flint_and_steel` -> 54m lava pool multi-hop -> cast & ignite portal -> `the_nether`!

### Phase 5: L4 DEPS Planner, `skills_kb/*.md` & Semantic Compaction (`v2/planner/`)
1. Author 15 concise (`300–500` Chinese chars) `skills_kb/*.md` strategy cards (following `AGENTS.md` Harness AI Skill spec).
2. Implement `BM25SkillRetriever`, `DepsExplainer`, `HorizonSelector` (with `PORTAL_GATE_READY` speedrun priority), `SemanticCompactor` (Pi/OMP `ProgressCard` + last 2 tool pairs), `IntentCompiler`, and `DepsPlanner` + `SerialModelWorker`.
3. Assert in `tests_v2/planner/` that serialized request payloads stay `<= 6,600` bytes across 50+ simulated turns while preserving valid tool_call/tool_result pairing.

### Phase 6: Cutover, Observability, Legacy Cleanup & Final Verification
1. Wire `main.py` to `v2` (`sleep`, `survival/starter`, `survival/daily`, `survival/competition`), keeping `bot_sleep.py`, `client.py`, `scripts/botctl.py`, and `scripts/multi_bot_runner.py` working seamlessly.
2. Upgrade `v2/observability/monitor.py` & `debug/monitor.html` with live BT active path, cache hit ratio, and prompt/completion token ratio.
3. Move superseded `v1` procedural modules/tests to `legacy/` (or delete dead code per `REWRITE_V2_MASTERPLAN.md` Section 5 once all `tests_v2` pass).
4. Update `docs/ARCHITECTURE.md`, `docs/SKILL_SPEC.md`, and `docs/TESTING.md` (strictly distinguishing offline simulation pass vs live server verification).
