from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("MONITOR_TARGETS_FILE", BASE_DIR / "config" / "targets.json"))
REQUEST_TIMEOUT = float(os.getenv("MONITOR_REQUEST_TIMEOUT", "12"))
REFRESH_SECONDS = int(os.getenv("MONITOR_REFRESH_SECONDS", "15"))
VERIFY_SSL = str(os.getenv("MONITOR_VERIFY_SSL", "true")).strip().lower() not in {"0", "false", "no", "off"}

ADMIN_USERNAME = os.getenv("MONITOR_ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("MONITOR_ADMIN_PASSWORD", "")

SYSTEM_CONFIG_PATH = BASE_DIR / "config" / "system.json"

def load_system_credentials() -> tuple[str, str]:
    if SYSTEM_CONFIG_PATH.exists():
        try:
            import json
            data = json.loads(SYSTEM_CONFIG_PATH.read_text(encoding="utf-8"))
            return (
                str(data.get("system_username") or "").strip(),
                str(data.get("system_password") or "").strip()
            )
        except Exception:
            pass
    return (
        os.getenv("MONITOR_SYSTEM_USERNAME", "").strip(),
        os.getenv("MONITOR_SYSTEM_PASSWORD", "").strip()
    )

def save_system_credentials(username: str, password: str) -> None:
    SYSTEM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    import json
    data = {
        "system_username": username.strip(),
        "system_password": password.strip()
    }
    SYSTEM_CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

SYSTEM_USERNAME, SYSTEM_PASSWORD = load_system_credentials()

ACTIVE_SESSIONS: set[str] = set()

def is_authenticated(request: Request) -> bool:
    if not SYSTEM_USERNAME and not SYSTEM_PASSWORD:
        return True
    session_token = request.cookies.get("session_token")
    return bool(session_token and session_token in ACTIVE_SESSIONS)

def verify_api_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


@dataclass(frozen=True)
class Target:
    id: str
    name: str
    base_url: str
    enabled: bool = True
    ip: str = ""
    group: str = ""
    note: str = ""
    username: str = ""
    password: str = ""


class TargetClient:
    def __init__(self, target: Target) -> None:
        self.target = target
        self.client = httpx.AsyncClient(
            base_url=target.base_url.rstrip("/"),
            timeout=REQUEST_TIMEOUT,
            verify=VERIFY_SSL,
            follow_redirects=False,
        )
        self.logged_in_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def ensure_login(self) -> None:
        if self.logged_in_at > 0:
            return
        async with self._lock:
            if self.logged_in_at > 0:
                return
            await self._login_unlocked()

    async def login(self) -> None:
        async with self._lock:
            await self._login_unlocked()

    async def _login_unlocked(self) -> None:
        username = self.target.username or ADMIN_USERNAME
        password = self.target.password or ADMIN_PASSWORD
        if not username or not password:
            raise RuntimeError("target username and password are required")
        response = await self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code != 200:
            raise RuntimeError(f"login failed: HTTP {response.status_code}")
        self.logged_in_at = time.time()

    async def get_json(self, path: str, *, auth: bool = True) -> dict[str, Any]:
        if auth:
            await self.ensure_login()
        response = await self.client.get(path)
        if auth and response.status_code == 401:
            await self.login()
            response = await self.client.get(path)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"GET {path} failed: HTTP {response.status_code}")
        data = response.json()
        return data if isinstance(data, dict) else {"value": data}

    async def collect(self) -> dict[str, Any]:
        started = time.time()
        payload: dict[str, Any] = {
            "id": self.target.id,
            "name": self.target.name,
            "base_url": self.target.base_url,
            "ip": self.target.ip,
            "group": self.target.group,
            "note": self.target.note,
            "username": self.target.username,
            "has_password": bool(self.target.password),
            "enabled": self.target.enabled,
            "online": False,
            "latency_ms": None,
            "updated_at": int(time.time()),
            "error": "",
            "health": {},
            "stats": {},
            "token_summary": {},
            "recent_logs": [],
        }
        if not self.target.enabled:
            payload["error"] = "disabled"
            return payload
        try:
            health_task = self.get_json("/api/v1/health", auth=False)
            stats_task = self.get_json("/api/v1/logs/stats?range=today")
            tokens_task = self.get_json("/api/v1/tokens?page=1&page_size=1")
            logs_task = self.get_json("/api/v1/logs?limit=8&page=1&task_filter=all")
            health, stats, tokens, logs = await asyncio.gather(
                health_task,
                stats_task,
                tokens_task,
                logs_task,
            )
            payload.update(
                {
                    "online": True,
                    "latency_ms": int((time.time() - started) * 1000),
                    "health": health,
                    "stats": normalize_stats(stats),
                    "token_summary": normalize_token_summary(tokens),
                    "recent_logs": normalize_recent_logs(logs),
                    "updated_at": int(time.time()),
                }
            )
        except Exception as exc:
            payload["latency_ms"] = int((time.time() - started) * 1000)
            payload["error"] = str(exc)
        return payload


def normalize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    total_requests = int(stats.get("total_requests") or 0)
    failed_requests = int(stats.get("failed_requests") or 0)
    generated_total = max(0, total_requests - failed_requests)
    return {
        "in_progress_requests": int(stats.get("in_progress_requests") or 0),
        "total_requests": total_requests,
        "failed_requests": failed_requests,
        "generated_images": int(stats.get("generated_images") or 0),
        "generated_videos": int(stats.get("generated_videos") or 0),
        "generated_total": generated_total,
        "recent_completed_count": int(stats.get("recent_completed_count") or 0),
        "recent_completed_avg_duration_sec": int(stats.get("recent_completed_avg_duration_sec") or 0),
        "recent_completed_timeline": stats.get("recent_completed_timeline") if isinstance(stats.get("recent_completed_timeline"), list) else [],
        "range": stats.get("range") or "today",
        "end_ts": stats.get("end_ts"),
    }


def normalize_token_summary(tokens_payload: dict[str, Any]) -> dict[str, Any]:
    summary = tokens_payload.get("summary") if isinstance(tokens_payload.get("summary"), dict) else {}
    return {
        "total": int(tokens_payload.get("total") or summary.get("total") or 0),
        "active": int(summary.get("active") or summary.get("active_count") or 0),
        "pending": int(summary.get("pending") or 0),
        "credit_active": int(summary.get("credit_active") or summary.get("credit_active_count") or 0),
        "regular_active": int(summary.get("regular_active") or summary.get("regular_active_count") or 0),
        "credits_available_total": float(summary.get("credits_available_total") or 0),
    }


def normalize_recent_logs(logs_payload: dict[str, Any]) -> list[dict[str, Any]]:
    logs = logs_payload.get("logs")
    if not isinstance(logs, list):
        return []
    items: list[dict[str, Any]] = []
    for item in logs[:8]:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "ts": item.get("ts"),
                "operation": item.get("operation") or item.get("path") or "-",
                "status_code": item.get("status_code"),
                "task_status": item.get("task_status") or "",
                "task_progress": item.get("task_progress"),
                "duration_sec": item.get("duration_sec"),
                "error": item.get("error") or "",
                "model": item.get("model") or "",
            }
        )
    return items


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def normalize_target_id(value: Any = "") -> str:
    text = str(value or "").strip()
    if text and all(ch.isalnum() or ch in {"-", "_"} for ch in text):
        return text[:80]
    return uuid.uuid4().hex[:12]


def normalize_base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if text and "://" not in text:
        text = f"http://{text}"
    return text


def target_to_dict(target: Target) -> dict[str, Any]:
    return {
        "id": target.id,
        "name": target.name,
        "base_url": target.base_url,
        "ip": target.ip,
        "group": target.group,
        "enabled": target.enabled,
        "note": target.note,
        "username": target.username,
        "password": target.password,
    }


def target_to_public_dict(target: Target) -> dict[str, Any]:
    payload = target_to_dict(target)
    payload.pop("password", None)
    payload["has_password"] = bool(target.password)
    return payload


def target_from_payload(
    payload: dict[str, Any], *, target_id: str = "", existing: Target | None = None,
    require_credentials: bool = True
) -> Target:
    name = str(payload.get("name") or "").strip()
    base_url = normalize_base_url(payload.get("base_url") or payload.get("url"))
    if not name:
        raise ValueError("name is required")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if existing is not None:
        if not username:
            username = existing.username
        if not password:
            password = existing.password
    if require_credentials and not username:
        raise ValueError("username is required")
    if require_credentials and not password:
        raise ValueError("password is required")
    return Target(
        id=normalize_target_id(target_id or payload.get("id")),
        name=name,
        base_url=base_url,
        enabled=parse_bool(payload.get("enabled"), True),
        ip=str(payload.get("ip") or "").strip(),
        group=str(payload.get("group") or "").strip(),
        note=str(payload.get("note") or "").strip(),
        username=username,
        password=password,
    )


def load_targets() -> list[Target]:
    if not CONFIG_PATH.exists():
        return []
    config_text = CONFIG_PATH.read_text(encoding="utf-8").strip()
    if not config_text:
        return []
    raw = json.loads(config_text)
    items = raw.get("targets") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise RuntimeError("targets config must be a list or {\"targets\": [...]}")
    targets: list[Target] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        try:
            target = target_from_payload(
                item,
                target_id=str(item.get("id") or f"target-{index}").strip(),
                require_credentials=False,
            )
        except ValueError:
            continue
        if target.id in seen_ids:
            target = Target(
                id=normalize_target_id(),
                name=target.name,
                base_url=target.base_url,
                enabled=target.enabled,
                ip=target.ip,
                group=target.group,
                note=target.note,
                username=target.username,
                password=target.password,
            )
        seen_ids.add(target.id)
        targets.append(target)
    return targets


def save_targets(targets: list[Target]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"targets": [target_to_dict(target) for target in targets]}
    temp_path = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(CONFIG_PATH)


class MonitorState:
    def __init__(self) -> None:
        self.clients: dict[str, TargetClient] = {}
        self.snapshot: dict[str, Any] = {"targets": [], "updated_at": 0, "refresh_seconds": REFRESH_SECONDS}
        self._task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()

    async def start(self) -> None:
        self.reload_targets()
        await self.refresh()
        self._task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for client in self.clients.values():
            await client.close()
        self.clients.clear()

    def reload_targets(self) -> None:
        targets = load_targets()
        next_keys = {target.id for target in targets}
        for key in list(self.clients.keys()):
            if key not in next_keys:
                old_client = self.clients.pop(key)
                asyncio.create_task(old_client.close())
        for target in targets:
            existing = self.clients.get(target.id)
            if existing and existing.target == target:
                continue
            if existing:
                asyncio.create_task(existing.close())
            self.clients[target.id] = TargetClient(target)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(max(5, REFRESH_SECONDS))
            try:
                await self.refresh()
            except Exception:
                pass

    async def refresh(self) -> dict[str, Any]:
        async with self._refresh_lock:
            self.reload_targets()
            clients = list(self.clients.values())
            results = await asyncio.gather(
                *(client.collect() for client in clients),
                return_exceptions=True,
            )
            targets: list[dict[str, Any]] = []
            for client, result in zip(clients, results):
                if isinstance(result, Exception):
                    targets.append(
                        {
                            "id": client.target.id,
                            "name": client.target.name,
                            "base_url": client.target.base_url,
                            "ip": client.target.ip,
                            "group": client.target.group,
                            "username": client.target.username,
                            "has_password": bool(client.target.password),
                            "enabled": client.target.enabled,
                            "online": False,
                            "error": str(result),
                            "updated_at": int(time.time()),
                        }
                    )
                else:
                    targets.append(result)
            self.snapshot = {
                "targets": targets,
                "summary": build_summary(targets),
                "updated_at": int(time.time()),
                "refresh_seconds": REFRESH_SECONDS,
            }
            return self.snapshot


def build_summary(targets: list[dict[str, Any]]) -> dict[str, Any]:
    enabled = [item for item in targets if item.get("enabled")]
    online = [item for item in enabled if item.get("online")]
    stats = [item.get("stats") or {} for item in online]
    token_summaries = [item.get("token_summary") or {} for item in online]
    return {
        "target_count": len(enabled),
        "online_count": len(online),
        "offline_count": max(0, len(enabled) - len(online)),
        "in_progress_requests": sum(int(item.get("in_progress_requests") or 0) for item in stats),
        "total_requests": sum(int(item.get("total_requests") or 0) for item in stats),
        "failed_requests": sum(int(item.get("failed_requests") or 0) for item in stats),
        "account_total": sum(int(item.get("total") or 0) for item in token_summaries),
        "account_active": sum(int(item.get("active") or 0) for item in token_summaries),
        "credits_available_total": sum(float(item.get("credits_available_total") or 0) for item in token_summaries),
    }


monitor_state = MonitorState()
app = FastAPI(title="Server Monitor", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


@app.on_event("startup")
async def startup() -> None:
    await monitor_state.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await monitor_state.stop()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=307)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> Response:
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=307)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
) -> Response:
    if username == SYSTEM_USERNAME and password == SYSTEM_PASSWORD:
        token = uuid.uuid4().hex
        ACTIVE_SESSIONS.add(token)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            max_age=86400,
            samesite="lax",
        )
        return response
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "用户名或密码不正确"}
    )


@app.get("/logout")
async def logout() -> Response:
    response = RedirectResponse(url="/login", status_code=307)
    response.delete_cookie("session_token")
    return response


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "targets": len(monitor_state.clients)}


@app.get("/api/status", dependencies=[Depends(verify_api_auth)])
async def status() -> dict[str, Any]:
    return monitor_state.snapshot


@app.get("/api/targets", dependencies=[Depends(verify_api_auth)])
async def list_monitor_targets() -> dict[str, Any]:
    return {"targets": [target_to_public_dict(target) for target in load_targets()]}


@app.post("/api/targets", dependencies=[Depends(verify_api_auth)])
async def add_monitor_target(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        targets = load_targets()
        target = target_from_payload(payload, target_id=normalize_target_id())
        targets.append(target)
        save_targets(targets)
        await monitor_state.refresh()
        return {"status": "ok", "target": target_to_public_dict(target)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/targets/{target_id}", dependencies=[Depends(verify_api_auth)])
async def update_monitor_target(target_id: str, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        targets = load_targets()
        for index, target in enumerate(targets):
            if target.id == target_id:
                updated = target_from_payload(
                    payload, target_id=target.id, existing=target
                )
                targets[index] = updated
                save_targets(targets)
                await monitor_state.refresh()
                return {"status": "ok", "target": target_to_public_dict(updated)}
        raise HTTPException(status_code=404, detail="target not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/targets/{target_id}", dependencies=[Depends(verify_api_auth)])
async def delete_monitor_target(target_id: str) -> dict[str, Any]:
    targets = load_targets()
    next_targets = [target for target in targets if target.id != target_id]
    if len(next_targets) == len(targets):
        raise HTTPException(status_code=404, detail="target not found")
    save_targets(next_targets)
    await monitor_state.refresh()
    return {"status": "ok"}


@app.post("/api/refresh", dependencies=[Depends(verify_api_auth)])
async def refresh() -> dict[str, Any]:
    try:
        return await monitor_state.refresh()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/system/config", dependencies=[Depends(verify_api_auth)])
async def get_system_config() -> dict[str, str]:
    global SYSTEM_USERNAME
    return {"username": SYSTEM_USERNAME}


@app.post("/api/system/config", dependencies=[Depends(verify_api_auth)])
async def update_system_config(request: Request) -> dict[str, str]:
    global SYSTEM_USERNAME, SYSTEM_PASSWORD
    try:
        payload = await request.json()
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "").strip()
        if not username:
            raise ValueError("用户名不能为空")
        
        # If password is empty, keep current password
        if not password:
            password = SYSTEM_PASSWORD
            
        save_system_credentials(username, password)
        SYSTEM_USERNAME = username
        SYSTEM_PASSWORD = password
        return {"status": "ok", "username": username}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
