"""Transactional inventory and durable delivery jobs. No network inside DB transactions."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


def cookie_header(value):
    if isinstance(value, str):
        value = value.strip()
        return value[7:].strip() if value.lower().startswith("cookie:") else value
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items() if v is not None)
    if isinstance(value, list):
        return "; ".join(f"{v['name']}={v['value']}" for v in value
                         if isinstance(v, dict) and "name" in v and "value" in v)
    return ""


def normalize_account(item):
    email, name = "", ""
    if isinstance(item, dict):
        email = str(item.get("email") or item.get("_bind_email") or "").strip().lower()
        name = str(item.get("name") or email or "").strip()[:320]
        usage = item.get("_usage") if isinstance(item.get("_usage"), dict) else {}
        # Match the existing Luma importer: explicit cookie, then exported fallback.
        value = item.get("cookie") or item.get("cookies") or usage.get("cookie_header") or item.get("session_cookies")
    else:
        value = item
    cookie = cookie_header(value)
    if not cookie or len(cookie) > 65536 or any(ord(c) < 32 for c in cookie):
        raise ValueError("Cookie 为空、过长或包含控制字符")
    pairs = []
    for part in cookie.split(";"):
        if not part.strip():
            continue
        key, separator, val = part.strip().partition("=")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ValueError("Cookie 格式应为 name=value; name2=value2")
        pairs.append((key, val))
    if not pairs:
        raise ValueError("没有 Cookie 字段")
    # Sort only the fingerprint; preserve the exact credential sent on all retries.
    fingerprint = hashlib.sha256(json.dumps(sorted(pairs), ensure_ascii=False).encode()).hexdigest()
    identity = hashlib.sha256(("email:" + email).encode()).hexdigest() if email else fingerprint
    return {"cookie": cookie, "name": name or f"account-{fingerprint[:8]}",
            "fingerprint": fingerprint, "identity": identity}


def parse_document(text):
    text = text.lstrip("\ufeff").strip()
    if not text:
        return []
    if text.startswith(("{", "[")):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("JSON 格式错误") from None
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return value["items"]
        return value if isinstance(value, list) else [value]
    return [line.strip() for line in text.splitlines() if line.strip()]


class AccountPool:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def initialize(self, legacy_path=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                    identity TEXT UNIQUE NOT NULL, name TEXT NOT NULL, cookie TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'available', created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS accounts_state ON accounts(state,created_at);
                CREATE TABLE IF NOT EXISTS delivered (
                    fingerprint TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                    target_id TEXT NOT NULL, profile_id TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL, base_url TEXT NOT NULL, batch_size INTEGER NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS jobs_schedule ON jobs(status,retry_at,lease_until);
                CREATE TABLE IF NOT EXISTS job_items (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    account_id TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    profile_id TEXT NOT NULL DEFAULT '', already_exists INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS items_job_state ON job_items(job_id,state);
            """)
        if legacy_path and Path(legacy_path).exists():
            self.migrate(Path(legacy_path))

    @staticmethod
    def _insert(conn, account, state="available"):
        for table in ("accounts", "delivered"):
            if conn.execute(f"SELECT 1 FROM {table} WHERE fingerprint=? OR identity=?",
                            (account["fingerprint"], account["identity"])).fetchone():
                return False
        conn.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)",
                     (uuid.uuid4().hex, account["fingerprint"], account["identity"],
                      account["name"], account["cookie"], state, time.time()))
        return True

    def migrate(self, path):
        with self.db() as conn:
            if conn.execute("SELECT 1 FROM metadata WHERE key='legacy_migrated'").fetchone():
                return
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            rows = raw.get("cookies", []) if isinstance(raw, dict) else raw
            for row in rows:
                account = normalize_account(row)
                state = row.get("status", "available")
                if state == "assigned":
                    conn.execute("INSERT OR IGNORE INTO delivered VALUES (?,?,?,?,?)",
                                 (account["fingerprint"], account["identity"], row.get("target_id", ""), "legacy", time.time()))
                else:
                    self._insert(conn, account, "available" if state == "available" else "legacy_review")
            conn.execute("INSERT INTO metadata VALUES ('legacy_migrated','1')")
        # Keep the original as a migration backup, never silently re-import it.

    def import_items(self, items):
        added, duplicate, errors = 0, 0, []
        with self.db() as conn:
            for index, item in enumerate(items, 1):
                try:
                    account = normalize_account(item)
                    if self._insert(conn, account):
                        added += 1
                    else:
                        duplicate += 1
                except ValueError as exc:
                    errors.append({"index": index, "error": str(exc)})
        return {"imported_count": added, "duplicate_count": duplicate,
                "invalid_count": len(errors), "errors": errors[:100]}

    def summary(self):
        with self.db() as conn:
            states = dict(conn.execute("SELECT state,COUNT(*) FROM accounts GROUP BY state").fetchall())
            items = dict(conn.execute("SELECT state,COUNT(*) FROM job_items WHERE state IN ('unknown','failed') GROUP BY state").fetchall())
            assigned = conn.execute("SELECT COUNT(*) FROM delivered").fetchone()[0]
        return {"total": sum(states.values()), "available": states.get("available", 0),
                "assigning": states.get("reserved", 0) - items.get("unknown", 0) - items.get("failed", 0),
                "unknown": items.get("unknown", 0), "failed": items.get("failed", 0),
                "legacy_review": states.get("legacy_review", 0), "assigned": assigned}

    def create_jobs(self, allocations, batch_size=50, source="manual"):
        if not 1 <= batch_size <= 100 or not allocations:
            raise ValueError("批大小应为 1–100，至少选择一台服务器")
        ids = [target.id for target, _ in allocations]
        if len(set(ids)) != len(ids) or any(not 1 <= count <= 100000 for _, count in allocations):
            raise ValueError("服务器不可重复，导入数量应为 1–100000")
        group_id = uuid.uuid4().hex
        job_ids = []
        with self.db() as conn:
            total = sum(count for _, count in allocations)
            available = conn.execute("SELECT COUNT(*) FROM accounts WHERE state='available'").fetchone()[0]
            if total > available:
                raise ValueError(f"号池不足：需要 {total}，可分配 {available}")
            for target, count in allocations:
                # Avoid duplicate auto-refill while pending activation is reflected later.
                if source == "auto" and conn.execute("SELECT 1 FROM jobs WHERE target_id=? AND status IN ('queued','running','attention')", (target.id,)).fetchone():
                    raise ValueError("该服务器已有未完成的导入任务")
                job_id = uuid.uuid4().hex
                now = time.time()
                conn.execute("""INSERT INTO jobs
                    (id,group_id,target_id,target_name,base_url,batch_size,source,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (job_id, group_id, target.id, target.name, target.base_url.rstrip("/"), batch_size, source, now, now))
                rows = conn.execute("SELECT id,name FROM accounts WHERE state='available' ORDER BY created_at,id LIMIT ?", (count,)).fetchall()
                conn.executemany("UPDATE accounts SET state='reserved' WHERE id=?", [(row["id"],) for row in rows])
                conn.executemany("INSERT INTO job_items (id,job_id,account_id,name) VALUES (?,?,?,?)",
                                 [(uuid.uuid4().hex, job_id, row["id"], row["name"]) for row in rows])
                job_ids.append(job_id)
        return {"status": "queued", "group_id": group_id, "job_ids": job_ids, "total": total}

    def claim(self, parallelism=3):
        now = time.time()
        with self.db() as conn:
            if conn.execute("SELECT COUNT(*) FROM jobs WHERE lease_until>?", (now,)).fetchone()[0] >= parallelism:
                return None
            job = conn.execute("""SELECT * FROM jobs j WHERE status IN ('queued','running')
                AND retry_at<=? AND lease_until<=? AND NOT EXISTS
                (SELECT 1 FROM jobs other WHERE other.target_id=j.target_id AND other.lease_until>?)
                ORDER BY updated_at,id LIMIT 1""", (now, now, now)).fetchone()
            if not job:
                return None
            job = dict(job)
            # A dead worker may have sent these. They remain pinned to this job/target.
            conn.execute("UPDATE job_items SET state='unknown',error='执行中断，使用原幂等键确认' WHERE job_id=? AND state='inflight'", (job["id"],))
            rows = conn.execute("""SELECT i.*,a.cookie FROM job_items i JOIN accounts a ON a.id=i.account_id
                WHERE job_id=? AND (i.state='queued' OR (i.state='unknown' AND attempts<3))
                ORDER BY i.rowid LIMIT ?""", (job["id"], job["batch_size"])).fetchall()
            if not rows:
                self._rollup(conn, job["id"])
                return None
            lease = uuid.uuid4().hex
            conn.execute("UPDATE jobs SET status='running',lease=?,lease_until=?,updated_at=? WHERE id=?",
                         (lease, now + 300, now, job["id"]))
            conn.executemany("UPDATE job_items SET state='inflight',attempts=attempts+1 WHERE id=?", [(r["id"],) for r in rows])
            return dict(job, lease=lease, items=[dict(r) for r in rows])

    @staticmethod
    def _rollup(conn, job_id):
        states = dict(conn.execute("SELECT state,COUNT(*) FROM job_items WHERE job_id=? GROUP BY state", (job_id,)).fetchall())
        retryable = conn.execute("SELECT 1 FROM job_items WHERE job_id=? AND state='unknown' AND attempts<3", (job_id,)).fetchone()
        if states.get("queued") or states.get("inflight") or retryable:
            status = "queued"
        elif states.get("failed") or states.get("unknown"):
            status = "attention"
        elif states.get("cancelled"):
            status = "cancelled"
        else:
            status = "completed"
        conn.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (status, time.time(), job_id))

    def finish(self, job, receipts=None, error=""):
        receipts = receipts if isinstance(receipts, list) else []
        by_key = {}
        for receipt in receipts:
            if isinstance(receipt, dict) and isinstance(receipt.get("import_key"), str):
                key = receipt["import_key"]
                # Duplicate receipts are ambiguous, not proof of success.
                by_key[key] = None if key in by_key else receipt
        with self.db() as conn:
            current = conn.execute("SELECT lease FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if not current or current["lease"] != job["lease"]:
                return
            unknown = False
            for item in job["items"]:
                result = by_key.get(item["id"]) or {}
                if result.get("persisted") is True and isinstance(result.get("profile_id"), str) and result["profile_id"]:
                    account = conn.execute("SELECT * FROM accounts WHERE id=?", (item["account_id"],)).fetchone()
                    conn.execute("INSERT INTO delivered VALUES (?,?,?,?,?)",
                                 (account["fingerprint"], account["identity"], job["target_id"], result["profile_id"], time.time()))
                    conn.execute("DELETE FROM accounts WHERE id=?", (item["account_id"],))
                    conn.execute("UPDATE job_items SET state='success',error='',profile_id=?,already_exists=? WHERE id=?",
                                 (result["profile_id"], int(result.get("already_exists") is True), item["id"]))
                elif result.get("persisted") is False and result.get("retryable") is False:
                    conn.execute("UPDATE job_items SET state='failed',error='目标拒绝：账号格式无效或幂等键冲突' WHERE id=?", (item["id"],))
                else:
                    unknown = True
                    conn.execute("UPDATE job_items SET state='unknown',error=? WHERE id=?",
                                 (error or "回执未确认，保留账号并绑定原服务器", item["id"]))
            conn.execute("UPDATE jobs SET lease='',lease_until=0,retry_at=? WHERE id=?",
                         (time.time() + 15 if unknown else 0, job["id"]))
            self._rollup(conn, job["id"])

    def list_jobs(self, limit=50):
        with self.db() as conn:
            jobs = [dict(r) for r in conn.execute("""SELECT id,group_id,target_id,target_name,status,source,created_at,updated_at
                FROM jobs ORDER BY created_at DESC LIMIT ?""", (limit,))]
            for job in jobs:
                counts = dict(conn.execute("SELECT state,COUNT(*) FROM job_items WHERE job_id=? GROUP BY state", (job["id"],)).fetchall())
                job["counts"] = counts
                job["total"] = sum(counts.values())
                job["already_exists"] = conn.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND already_exists=1", (job["id"],)).fetchone()[0]
        return jobs

    def details(self, job_id, offset=0):
        with self.db() as conn:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise ValueError("任务不存在")
            return [dict(row) for row in conn.execute("""SELECT id,name,state,attempts,error,profile_id,already_exists
                FROM job_items WHERE job_id=? ORDER BY CASE WHEN state IN ('failed','unknown') THEN 0 ELSE 1 END,rowid
                LIMIT 100 OFFSET ?""", (job_id, offset))]

    def action(self, job_id, action):
        with self.db() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise ValueError("任务不存在")
            if job["lease_until"] > time.time():
                raise ValueError("该任务正在发送，请等待当前批次完成后操作")
            if action == "retry":
                # Same item ID, same target, same credentials on every retry.
                conn.execute("UPDATE job_items SET state='unknown',attempts=0,error='' WHERE job_id=? AND state IN ('failed','unknown','inflight')", (job_id,))
                conn.execute("UPDATE jobs SET retry_at=0 WHERE id=?", (job_id,))
            elif action == "cancel":
                conn.execute("UPDATE accounts SET state='available' WHERE id IN (SELECT account_id FROM job_items WHERE job_id=? AND state='queued')", (job_id,))
                conn.execute("UPDATE job_items SET state='cancelled' WHERE job_id=? AND state='queued'", (job_id,))
            else:
                raise ValueError("不支持的操作")
            self._rollup(conn, job_id)

    def has_open_job(self, target_id):
        with self.db() as conn:
            return bool(conn.execute("SELECT 1 FROM jobs WHERE target_id=? AND status IN ('queued','running','attention')", (target_id,)).fetchone())
