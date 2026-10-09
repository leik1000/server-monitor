import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.account_pool import AccountPool


class ApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pool = AccountPool(Path(temp.name) / "pool.sqlite3")
        pool.initialize()
        for name, value in [("cookie_pool", pool), ("SYSTEM_USERNAME", ""), ("SYSTEM_PASSWORD", ""),
                            ("load_targets", lambda: [main.Target(id="test", name="test", base_url="http://example.test")])]:
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        # No lifespan: do not contact real configured targets or start real workers.
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_document_upload_and_background_job_contract(self):
        result = self.client.post("/api/cookies/import", json={"documents": [
            {"name": "account.json", "text": '{"email":"test@example.test","cookie":"session=abc","password":"secret"}'},
            {"name": "bad.json", "text": "{broken"},
        ]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["imported_count"], 1)
        self.assertEqual(result.json()["invalid_count"], 1)
        response = self.client.post("/api/import-jobs", json={"allocations": [{"target_id": "test", "count": 1}], "batch_size": 1})
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_ids"][0]
        details = self.client.get("/api/import-jobs/" + job_id).json()
        self.assertNotIn("session=abc", str(details))
        self.assertNotIn("secret", str(details))
        self.assertEqual(self.client.get("/api/cookies").json()["available"], 0)
        self.assertEqual(self.client.post(f"/api/import-jobs/{job_id}/cancel").status_code, 200)
        self.assertEqual(self.client.get("/api/cookies").json()["available"], 1)

    def test_authentication_and_validation(self):
        with patch.object(main, "SYSTEM_USERNAME", "admin"), patch.object(main, "SYSTEM_PASSWORD", "password"):
            self.assertEqual(self.client.get("/api/cookies").status_code, 401)
            self.assertEqual(self.client.post("/api/import-jobs", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/import-jobs", json={"allocations": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/cookies/import", content="not JSON").status_code, 400)
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
