import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app import main
from app.account_pool import AccountPool


class AutoRefillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.pool = AccountPool(Path(temp.name) / "pool.sqlite3")
        self.pool.initialize()
        self.pool.import_items([{"cookie": f"session={i}"} for i in range(50)])
        self.target = main.Target(id="test", name="Test", base_url="http://example.test",
                                  refill_enabled=True, refill_threshold=100, refill_target=100)
        self.state = main.MonitorState()
        self.client = Mock()
        self.state.clients["test"] = self.client
        self.active, self.pending, self.total = 80, 0, 999
        self.client.get_json = AsyncMock(side_effect=self.metrics)
        for name, value in [("cookie_pool", self.pool), ("load_targets", lambda: [self.target]),
                            ("target_config_lock", asyncio.Lock())]:
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def metrics(self, path):
        if "refresh-profiles" in path:
            return {"pool_summary": {"pending": self.pending}}
        return {"summary": {"active": self.active, "pending": self.pending, "total": self.total}}

    async def test_available_not_total_triggers_and_pending_deducted(self):
        self.pending = 5
        result = await self.state.refill("test")
        self.assertEqual(result["total"], 15)
        self.assertEqual(self.pool.list_jobs()[0]["source"], "auto")
        self.assertEqual(self.pool.summary()["available"], 35)

    async def test_equal_and_above_threshold_never_refill(self):
        for active in [100, 101]:
            self.active = active
            self.assertEqual((await self.state.refill("test"))["status"], "satisfied")
        self.assertEqual(self.pool.list_jobs(), [])

    async def test_one_below_threshold_refills_one(self):
        self.active = 99
        self.assertEqual((await self.state.refill("test"))["total"], 1)

    async def test_wait_for_activation_instead_of_repeated_import(self):
        self.pending = 20
        self.assertEqual((await self.state.refill("test"))["status"], "activating")
        self.assertEqual(self.pool.list_jobs(), [])

    async def test_pool_shortage_partially_fills_and_empty_waits(self):
        self.active = 10
        result = await self.state.refill("test")
        self.assertEqual(result["total"], 50)
        self.assertTrue(result["shortage"])
        self.pool.clear()
        result = await self.state.refill("test")
        self.assertEqual(result["status"], "empty")
        self.assertIn("等待导入", result["message"])

    async def test_existing_manual_or_attention_job_blocks_auto(self):
        self.pool.create_jobs([(self.target, 2)])
        self.assertEqual((await self.state.refill("test"))["status"], "running")
        with self.pool.db() as conn:
            conn.execute("UPDATE jobs SET status='attention'")
        self.assertEqual((await self.state.refill("test"))["status"], "running")
        self.assertEqual(len(self.pool.list_jobs()), 1)

    async def test_disabled_and_legacy_count_mode_do_not_allocate(self):
        self.target = replace(self.target, refill_enabled=False)
        self.assertEqual((await self.state.refill("test"))["status"], "disabled")
        self.target = replace(self.target, refill_enabled=True, refill_mode="count", refill_count=5)
        self.assertEqual((await self.state.refill("test"))["status"], "configuration")
        self.client.get_json.assert_not_called()

    async def test_disable_during_network_check_prevents_job_creation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(path):
            entered.set()
            await release.wait()
            return await self.metrics(path)

        self.client.get_json.side_effect = delayed
        task = asyncio.create_task(self.state.refill("test"))
        await entered.wait()
        async with main.target_config_lock:
            self.target = replace(self.target, refill_enabled=False)
        release.set()
        self.assertEqual((await task)["status"], "changed")
        self.assertEqual(self.pool.list_jobs(), [])

    async def test_missing_or_invalid_metrics_never_means_zero(self):
        for active in [None, "bad", -1, True]:
            self.active = active
            await self.state.safe_auto_refill("test")
            self.assertEqual(self.state.refill_status["test"]["status"], "error")
        self.assertEqual(self.pool.list_jobs(), [])

    async def test_scheduler_offline_cooldown_and_strict_boundary(self):
        with patch.object(self.state, "schedule_refill") as schedule:
            for item in [{"id": "test", "online": False},
                         {"id": "test", "online": True, "token_summary": {"active": 100}}]:
                await self.state.auto_refill([item])
            schedule.assert_not_called()
            item = {"id": "test", "online": True, "token_summary": {"active": 99, "total": 1000}}
            await self.state.auto_refill([item])
            schedule.assert_called_once_with("test")
            schedule.reset_mock()
            self.state._last_refill_at["test"] = main.time.time()
            await self.state.auto_refill([item])
            schedule.assert_not_called()

    async def test_scheduled_checks_do_not_duplicate_tasks(self):
        self.state.schedule_refill("test")
        first = self.state._refill_tasks["test"]
        self.state.schedule_refill("test")
        self.assertIs(first, self.state._refill_tasks["test"])
        await first
        self.assertEqual(len(self.pool.list_jobs()), 1)


class RefillSettingsApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = main.MonitorState()
        self.state.schedule_refill = Mock()
        self.state.snapshot["targets"] = [{"id": "test", "online": False}]
        for name, value in [("CONFIG_PATH", Path(temp.name) / "targets.json"),
                            ("monitor_state", self.state), ("target_config_lock", asyncio.Lock()),
                            ("SYSTEM_USERNAME", ""), ("SYSTEM_PASSWORD", "")]:
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        main.save_targets([main.Target(id="test", name="Test", base_url="http://example.test",
                                      username="user", password="secret", note="keep")])
        self.client = TestClient(main.app)  # Do not start real monitoring/dispatch workers.
        self.addCleanup(self.client.close)
        self.url = "/api/targets/test/refill-settings"

    def test_enable_persist_disable_and_snapshot(self):
        response = self.client.post(self.url, json={"enabled": True, "threshold": 100})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password", response.json()["target"])
        target = main.load_targets()[0]
        self.assertEqual((target.refill_enabled, target.refill_threshold, target.refill_target), (True, 100, 100))
        self.assertEqual((target.password, target.note), ("secret", "keep"))
        self.state.schedule_refill.assert_called_once_with("test")
        status = self.client.get("/api/status").json()["targets"][0]
        self.assertTrue(status["refill_enabled"])
        response = self.client.post(self.url, json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        target = main.load_targets()[0]
        self.assertFalse(target.refill_enabled)
        self.assertEqual(target.refill_threshold, 100)
        self.assertFalse(self.client.get("/api/status").json()["targets"][0]["refill_enabled"])

    def test_invalid_thresholds_and_auth(self):
        for threshold in [None, 0, -1, 100001, 1.5, True, "100"]:
            response = self.client.post(self.url, json={"enabled": True, "threshold": threshold})
            self.assertEqual(response.status_code, 400, threshold)
        self.assertEqual(self.client.post(self.url, json={"enabled": "false"}).status_code, 400)
        self.assertEqual(self.client.post("/api/targets/missing/refill-settings", json={"enabled": False}).status_code, 404)
        self.assertFalse(main.load_targets()[0].refill_enabled)
        with patch.object(main, "SYSTEM_USERNAME", "admin"), patch.object(main, "SYSTEM_PASSWORD", "password"):
            self.assertEqual(self.client.post(self.url, json={"enabled": False}).status_code, 401)

    def test_disabled_server_can_turn_off_but_not_enable(self):
        main.save_targets([replace(main.load_targets()[0], enabled=False, refill_enabled=True)])
        self.assertEqual(self.client.post(self.url, json={"enabled": True, "threshold": 10}).status_code, 400)
        self.assertEqual(self.client.post(self.url, json={"enabled": False}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
