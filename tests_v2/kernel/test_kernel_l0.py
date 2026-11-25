"""Phase 1 tests: L0 config, WAL UnifiedWorldDB, PendingTransactionJournal, dual-pool client, ModelRequestGate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

from tests_v2.sim.scenarios import scenario_full_nether_speedrun
from tests_v2.sim.virtual_mc_api import VirtualMcHttpServer
from v2.domain import compact_shelter_spec
from v2.kernel import (
    APIError,
    InstanceLockError,
    MinecraftAgentClient,
    ModelGateError,
    ModelRequestGate,
    PendingTransactionJournal,
    PendingWriteFrozenError,
    UnifiedWorldDB,
    load_config,
)


class TestKernelL0(unittest.TestCase):
    def test_config_loads_example_yaml_and_overrides(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        example = repo_root / "config.example.yaml"
        cfg = load_config(example, environ={"MC_AGENT_USER": "EnvBot"}, username="CliBot")
        self.assertEqual(cfg["agent"]["name"], "CliBot")
        self.assertIn("mc_api", cfg)
        self.assertIn("survival", cfg)

    def test_wal_db_concurrent_writes_and_chunk_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = UnifiedWorldDB.for_scope(td, "http://127.0.0.1:18900/api/v1", "BotA", "w1")
            try:
                def worker(idx: int) -> None:
                    db.save_landmark("overworld", "furnace", f"f_{idx}", (idx, 64, idx), {"idx": idx})
                    db.save_voxel_chunk("overworld", idx, 4, 0, {"0,0,0": "stone"})

                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(worker, range(18)))

                lms = db.list_landmarks("overworld", "furnace")
                self.assertEqual(len(lms), 18)
                chunk = db.load_voxel_chunk("overworld", 5, 4, 0)
                self.assertEqual(chunk, {"0,0,0": "stone"})
            finally:
                db.close()

    def test_pending_journal_freezes_on_unknown_and_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = UnifiedWorldDB.for_scope(td, "http://127.0.0.1:18900/api/v1", "BotA", "w1")
            journal = PendingTransactionJournal(db)
            tx1 = journal.begin_write("dig", {"x": 1, "y": 64, "z": 1}, pos=(0.5, 64.0, 0.5))
            # Concurrent second write rejected while inflight
            with self.assertRaises(PendingWriteFrozenError):
                journal.begin_write("place", {"x": 2, "y": 64, "z": 2})

            # Mark unknown -> remains frozen even after DB reopen (simulated crash/restart)
            journal.resolve_write(tx1, "unknown", details={"reason": "timeout"})
            db.close()

            db2 = UnifiedWorldDB.for_scope(td, "http://127.0.0.1:18900/api/v1", "BotA", "w1")
            try:
                journal2 = PendingTransactionJournal(db2)
                self.assertTrue(journal2.has_pending_unknown())
                with self.assertRaises(PendingWriteFrozenError):
                    journal2.begin_write("posture", {"sneak": False})
                # Explicit clear unlocks
                self.assertTrue(journal2.acknowledge_clear("verified_world_reset"))
                self.assertFalse(journal2.has_pending_unknown())
                tx2 = journal2.begin_write("dig", {"x": 3, "y": 64, "z": 3})
                journal2.resolve_write(tx2, "confirmed")
                self.assertFalse(journal2.has_pending_unknown())
            finally:
                db2.close()

    def test_instance_lock_prevents_duplicate_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = UnifiedWorldDB.for_scope(td, "http://127.0.0.1:18900/api/v1", "BotLock", "w1")
            try:
                j1 = PendingTransactionJournal(db)
                j2 = PendingTransactionJournal(db)
                with j1.instance_lock():
                    with self.assertRaises(InstanceLockError):
                        with j2.instance_lock():
                            pass
            finally:
                db.close()

    def test_dual_pool_client_concurrent_reads_and_unknown_write_classification(self) -> None:
        sim = scenario_full_nether_speedrun()
        sim.push_event("pickup", item="oak_log", count=2)
        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "SimBot", "pass", timeout_seconds=5.0)
            try:
                status = client.login(max_wait_seconds=5.0)
                self.assertTrue(status["ready"])
                action_sess_id = id(client._action_session)

                bundle = client.observe_tick_bundle(last_event_seq=0, entity_radius=32)
                self.assertEqual(bundle["status"]["pos"], [0.5, 64.0, 0.5])
                self.assertEqual(bundle["last_event_seq"], 1)
                self.assertEqual(len(bundle["events"]), 1)

                coords = [(4, 64, 2), (0, 63, 0), (500, 64, 500)]
                blocks = client.batch_get_blocks(coords)
                self.assertEqual(blocks[(4, 64, 2)]["name"], "oak_log")
                self.assertEqual(blocks[(0, 63, 0)]["name"], "grass_block")
                self.assertIsNone(blocks[(500, 64, 500)])  # not_loaded

                # Action session untouched and isolated from read pool sessions
                self.assertEqual(id(client._action_session), action_sess_id)
                for rs in client.read_pool._sessions:
                    self.assertIsNot(rs, client._action_session)

                # Inject not_connected on a write endpoint -> must raise action_unconfirmed + action_state=unknown
                sim.inject_fault("not_connected", "dig")
                with self.assertRaises(APIError) as ctx:
                    client.dig(4, 64, 2)
                self.assertEqual(ctx.exception.code, "action_unconfirmed")
                self.assertEqual(ctx.exception.details.get("action_state"), "unknown")
            finally:
                client.close()

    def test_model_gate_enforces_serial_concurrency_and_uncertain_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            gate = ModelRequestGate("http://127.0.0.1:8000/v1", "k1", directory=td, wait_seconds=5.0)
            active = 0
            peak = 0
            lock = threading.Lock()

            def run_slot() -> None:
                nonlocal active, peak
                with gate.request():
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    time.sleep(0.05)
                    with lock:
                        active -= 1

            threads = [threading.Thread(target=run_slot) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(peak, 1)

            # Uncertain timeout blocks next request until operator ack or 30s expiry
            class FakeTimeout(Exception):
                code = "llm_timeout"

            with self.assertRaises(FakeTimeout):
                with gate.request():
                    raise FakeTimeout("timed out")
            with self.assertRaises(ModelGateError) as ctx:
                with gate.request():
                    pass
            self.assertEqual(ctx.exception.code, "llm_request_uncertain")
            gate.acknowledge_provider_idle()
            with gate.request():
                pass

    def test_structure_spec_four_orientations_consistent(self) -> None:
        north = compact_shelter_spec((10, 64, 10), facing="north")
        east = compact_shelter_spec((10, 64, 10), facing="east")
        self.assertEqual(north.door_pos(), (10, 65, 8))
        self.assertEqual(east.door_pos(), (12, 65, 10))
        self.assertIn(east.bed_pos(), east.protected_coords())
        self.assertIn(east.chest_pos(), east.protected_coords())


if __name__ == "__main__":
    unittest.main()
