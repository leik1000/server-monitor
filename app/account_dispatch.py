import asyncio
import logging

import httpx


logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, pool, load_targets, client_factory, parallelism=3):
        self.pool = pool
        self.load_targets = load_targets
        self.client_factory = client_factory
        self.parallelism = max(1, min(10, parallelism))
        self.tasks = []

    async def start(self):
        self.tasks = [asyncio.create_task(self.loop()) for _ in range(self.parallelism)]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def deliver(self, job):
        client = None
        try:
            target = next((t for t in self.load_targets() if t.id == job["target_id"]), None)
            if not target or not target.enabled or target.base_url.rstrip("/") != job["base_url"]:
                await asyncio.to_thread(self.pool.finish, job, None, "目标已停用、删除或地址变化，请恢复原服务器配置后重试")
                return
            # A dedicated client cannot be closed by monitoring/config reloads mid-delivery.
            client = self.client_factory(target)
            capability = await client.get_json("/api/v1/refresh-profiles/dispatch-capabilities")
            if capability.get("version") != 1 or capability.get("durable_receipts") is not True:
                await asyncio.to_thread(self.pool.finish, job, None, "请先升级目标 Luma 服务以支持持久化导入回执")
                return
            receipts = []
            retry_items = [item for item in job["items"] if item["attempts"] > 0]
            if retry_items:
                result = await client.post_json("/api/v1/refresh-profiles/dispatch-receipts",
                                                {"keys": [i["id"] for i in retry_items]})
                receipts = result.get("items", [])
                if not isinstance(receipts, list):
                    receipts = []
            confirmed = {r.get("import_key") for r in receipts if isinstance(r, dict) and r.get("persisted") is True and r.get("profile_id")}
            remaining = [i for i in job["items"] if i["id"] not in confirmed]
            if remaining:
                result = await client.post_json("/api/v1/refresh-profiles/import-dispatch", {
                    "items": [{"import_key": i["id"], "cookie": i["cookie"], "name": i["name"]} for i in remaining]
                })
                returned = result.get("items", [])
                if isinstance(returned, list):
                    receipts.extend(returned)
            await asyncio.to_thread(self.pool.finish, job, receipts)
        except asyncio.CancelledError:
            # Durable inflight rows will be reconciled after the lease expires.
            raise
        except httpx.TimeoutException:
            await asyncio.to_thread(self.pool.finish, job, None, "请求超时，结果待确认；只向原服务器查询或重试")
        except Exception:
            await asyncio.to_thread(self.pool.finish, job, None, "连接或接口异常，请检查服务器状态、登录信息及 Luma 版本后重试")
        finally:
            if client:
                await client.close()

    async def loop(self):
        while True:
            try:
                job = await asyncio.to_thread(self.pool.claim, self.parallelism)
                if job:
                    await self.deliver(job)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Don't log exception bodies which could contain account credentials.
                logger.error("Account dispatcher iteration failed; retrying after delay")
                await asyncio.sleep(5)
