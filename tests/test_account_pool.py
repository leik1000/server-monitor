import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.account_pool import AccountPool, normalize_account, parse_document
from app.account_dispatch import Dispatcher


def target(identifier="server-a"):
    return SimpleNamespace(id=identifier, name=identifier, enabled=True, base_url="http://" + identifier)


class PoolFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pool = AccountPool(Path(self.temp.name) / "pool.sqlite3")
        self.pool.initialize()

    def seed(self, count):
        return self.pool.import_items([{"email": f"user{i}@example.test", "cookie": f"session={i}"} for i in range(count)])


class PoolTests(PoolFixture):
    def test_assigned_summary_uses_beijing_calendar_day(self):
        start = datetime.fromisoformat("2026-10-09T00:00:00+08:00").timestamp()
        timestamps = [start - 1, start, start + 3600, start + 86399, start + 86400]
        with self.pool.db() as conn:
            conn.executemany("INSERT INTO delivered VALUES (?,?,?,?,?)",
                             [(str(i), str(i), "server-a", "profile", ts)
                              for i, ts in enumerate(timestamps)])
        with patch("app.account_pool.time.time", return_value=start + 7200):
            self.assertEqual(self.pool.summary()["assigned"], 3)
        with patch("app.account_pool.time.time", return_value=start + 86400):
            self.assertEqual(self.pool.summary()["assigned"], 1)
        with patch("app.account_pool.time.time", return_value=start + 172800):
            self.assertEqual(self.pool.summary()["assigned"], 0)
        with self.pool.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM delivered").fetchone()[0], 5)

    def test_account_file_and_export_formats(self):
        account = {"email": "USER@example.test", "password": "must-not-persist", "cookie": "session=primary",
                   "_usage": {"cookie_header": "session=fallback"}, "session_cookies": {"session": "last-fallback"}}
        rows = parse_document("\ufeff" + json.dumps({"items": [account]}))
        self.assertEqual(self.pool.import_items(rows)["imported_count"], 1)
        with self.pool.db() as conn:
            row = dict(conn.execute("SELECT * FROM accounts").fetchone())
        self.assertEqual(row["cookie"], "session=primary")
        self.assertNotIn("password", row)
        self.assertEqual(normalize_account({"session_cookies": {"session": "x"}})["cookie"], "session=x")
        self.assertEqual(normalize_account({"_usage": {"cookie_header": "session=y"}})["cookie"], "session=y")
        self.assertEqual(parse_document("a=1\nb=2"), ["a=1", "b=2"])

    def test_dedup_identity_and_cookie_order_and_invalid(self):
        result = self.pool.import_items([
            {"email": "a@example.test", "cookie": "a=1; b=2"},
            {"email": "A@example.test", "cookie": "a=changed"},
            "b=2; a=1", "not-a-cookie", {"password": "secret"},
        ])
        self.assertEqual((result["imported_count"], result["duplicate_count"], result["invalid_count"]), (1, 2, 2))

    def test_reservation_is_atomic_under_concurrent_requests(self):
        self.seed(100)
        def reserve(i):
            return self.pool.create_jobs([(target(str(i)), 10)])
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(reserve, range(10)))
        self.assertEqual(sum(r["total"] for r in results), 100)
        with self.pool.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(DISTINCT account_id) FROM job_items").fetchone()[0], 100)
        with self.assertRaises(ValueError):
            self.pool.create_jobs([(target(), 1)])
        self.assertEqual(self.pool.summary()["available"], 0)

    def test_multi_target_shortage_rolls_back_all(self):
        self.seed(2)
        with self.assertRaises(ValueError):
            self.pool.create_jobs([(target("a"), 2), (target("b"), 1)])
        self.assertEqual(self.pool.list_jobs(), [])
        self.assertEqual(self.pool.summary()["available"], 2)

    def test_partial_receipts_delete_only_confirmed_accounts(self):
        self.seed(4)
        self.pool.create_jobs([(target(), 4)])
        job = self.pool.claim()
        first, second, third, fourth = job["items"]
        self.pool.finish(job, [
            {"import_key": first["id"], "persisted": True, "profile_id": "p1"},
            {"import_key": second["id"], "persisted": True, "profile_id": "p2", "already_exists": True},
            {"import_key": third["id"], "persisted": False, "retryable": False},
        ])
        summary = self.pool.summary()
        self.assertEqual((summary["total"], summary["assigned"], summary["failed"], summary["unknown"]), (2, 2, 1, 1))
        with self.pool.db() as conn:
            self.assertIsNone(conn.execute("SELECT cookie FROM accounts WHERE id=?", (first["account_id"],)).fetchone())
        self.assertEqual(self.seed(4)["duplicate_count"], 4)

    def test_parallelism_and_server_serialization(self):
        self.seed(4)
        self.pool.create_jobs([(target("a"), 1), (target("b"), 1), (target("c"), 1)])
        self.pool.create_jobs([(target("a"), 1)])
        jobs = [self.pool.claim(3) for _ in range(3)]
        self.assertEqual(len({j["target_id"] for j in jobs}), 3)
        self.assertIsNone(self.pool.claim(3))

    def test_crash_recovery_and_stale_worker_cannot_finalize(self):
        self.seed(1)
        self.pool.create_jobs([(target(), 1)])
        old = self.pool.claim()
        with self.pool.db() as conn:
            conn.execute("UPDATE jobs SET lease_until=0")
        restarted = AccountPool(self.pool.path)
        new = restarted.claim()
        self.assertEqual(new["items"][0]["id"], old["items"][0]["id"])
        receipt = [{"import_key": new["items"][0]["id"], "persisted": True, "profile_id": "p"}]
        restarted.finish(old, receipt)
        self.assertEqual(restarted.summary()["total"], 1)
        restarted.finish(new, receipt)
        self.assertEqual(restarted.summary()["total"], 0)

    def test_unknown_stays_pinned_and_retry_keeps_key(self):
        self.seed(1)
        created = self.pool.create_jobs([(target(), 1)])
        keys = []
        for _ in range(3):
            job = self.pool.claim()
            keys.append(job["items"][0]["id"])
            self.pool.finish(job, error="timeout")
            with self.pool.db() as conn:
                conn.execute("UPDATE jobs SET retry_at=0")
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(self.pool.list_jobs()[0]["status"], "attention")
        self.pool.action(created["job_ids"][0], "cancel")
        self.assertEqual(self.pool.summary()["available"], 0)
        self.pool.action(created["job_ids"][0], "retry")
        self.assertEqual(self.pool.claim()["items"][0]["id"], keys[0])

    def test_cancel_releases_only_unsent_rows(self):
        self.seed(3)
        created = self.pool.create_jobs([(target(), 3)], batch_size=1)
        job = self.pool.claim()
        self.pool.finish(job, error="timeout")
        self.pool.action(created["job_ids"][0], "cancel")
        self.assertEqual(self.pool.summary()["available"], 2)
        self.assertEqual(self.pool.summary()["unknown"], 1)

    def test_duplicate_receipts_are_not_success(self):
        self.seed(1)
        self.pool.create_jobs([(target(), 1)])
        job = self.pool.claim()
        r = {"import_key": job["items"][0]["id"], "persisted": True, "profile_id": "p"}
        self.pool.finish(job, [r, r])
        self.assertEqual(self.pool.summary()["unknown"], 1)

    def test_legacy_migration_once(self):
        legacy = Path(self.temp.name) / "cookies.json"
        legacy.write_text(json.dumps({"cookies": [
            {"cookie": "s=1", "status": "assigned", "target_id": "a"},
            {"cookie": "s=2", "status": "assigning", "target_id": "a"},
            {"cookie": "s=3", "status": "available"},
        ]}), encoding="utf-8")
        self.pool.initialize(legacy)
        self.pool.initialize(legacy)
        result = self.pool.summary()
        self.assertEqual((result["assigned"], result["legacy_review"], result["available"]), (1, 1, 1))

    def test_ten_thousand_accounts_split_without_truncating(self):
        self.seed(10000)
        result = self.pool.create_jobs([(target(), 10000)], batch_size=50)
        self.assertEqual(result["total"], 10000)
        self.assertEqual(len(self.pool.claim()["items"]), 50)
        self.assertEqual(self.pool.list_jobs()[0]["total"], 10000)


class DispatcherTests(PoolFixture):
    def test_timeout_after_remote_commit_is_reconciled_without_resend(self):
        self.seed(1)
        self.pool.create_jobs([(target(), 1)])
        receipts, imports = {}, []

        class Client:
            async def get_json(self, path):
                return {"version": 1, "durable_receipts": True}

            async def post_json(self, path, body):
                if path.endswith("dispatch-receipts"):
                    return {"items": [receipts[k] for k in body["keys"] if k in receipts]}
                imports.append(body)
                for item in body["items"]:
                    receipts[item["import_key"]] = {"import_key": item["import_key"], "persisted": True, "profile_id": "p"}
                raise httpx.ReadTimeout("synthetic timeout")

            async def close(self):
                pass

        dispatcher = Dispatcher(self.pool, lambda: [target()], lambda t: Client())
        asyncio.run(dispatcher.deliver(self.pool.claim()))
        self.assertEqual(self.pool.summary()["unknown"], 1)
        with self.pool.db() as conn:
            conn.execute("UPDATE jobs SET retry_at=0")
        asyncio.run(dispatcher.deliver(self.pool.claim()))
        self.assertEqual(len(imports), 1)
        self.assertEqual(self.pool.summary()["total"], 0)

    def test_target_address_change_never_sends_to_new_server(self):
        self.seed(1)
        self.pool.create_jobs([(target(), 1)])
        changed = target()
        changed.base_url = "http://different-server"
        def forbidden(t):
            self.fail("must not construct a client for a different destination")
        dispatcher = Dispatcher(self.pool, lambda: [changed], forbidden)
        asyncio.run(dispatcher.deliver(self.pool.claim()))
        self.assertEqual(self.pool.summary()["unknown"], 1)


if __name__ == "__main__":
    unittest.main()
