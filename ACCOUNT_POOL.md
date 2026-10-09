# 中央账号池与批量分配

## 更新顺序

1. 先更新各目标服务器的 `luma2api_com` 并重启 API 服务。新版本会自动创建 PostgreSQL 表 `account_import_receipts`，提供持久化回执接口。
2. 再更新 `server-monitor`，安装 `requirements.txt` 中的依赖并重启。使用 Docker Compose 时，在各项目目录执行 `docker compose up -d --build`。
3. 刷新监控页面，出现独立的「中央账号池」区域。

Luma 端相关修改：

- `api/routes/account_dispatch.py`：能力检查、批量分配、回执查询。
- `api/routes/admin.py`：挂载新接口，沿用管理员身份认证。
- `core/browser_initialization_store.py`：账号与回执在同一个事务中保存。
- `core/refresh_mgr.py`：接收分配账号并唤醒后台账号检查。
- `core/db.py`：自动创建回执表。

旧版目标服务器不能执行新分配协议。未升级时任务会保留账号，显示待确认；更新目标后点击「重试 / 确认原服务器」。

## 页面使用

1. 展开「批量上传账号 / 粘贴导入」。
2. 点击「选择账号文件夹」，选择本机 `available_account` 文件夹；也可以多选 JSON/TXT 文件或直接粘贴。
3. 上传会自动按最多 50 个文件、约 8MB 请求大小分批。单文件最大 4MB，单文件最多 10000 个账号；超出时请拆分。
4. 查看新增、重复、无效数量。格式错误会显示文件名及条目序号。
5. 点击「分配到服务器」，填写各服务器数量和每批账号数，点击「开始导入」。服务器卡片的「立即补号」也会打开分配窗口。
6. 后台持续执行，关闭网页不影响任务。执行期间约每 2 秒刷新库存与进度。

上传支持以下格式：

```json
{
  "email": "account@example.com",
  "cookie": "session=example; other=value"
}
```

也支持对象数组、`{"items": [...]}`、`_usage.cookie_header`、`session_cookies` 字典和每行一个 Cookie。

账号 JSON 中同时包含多个 Cookie 来源时，按 `cookie` → `cookies` → `_usage.cookie_header` → `session_cookies` 的顺序取第一个非空来源，保持显式 Cookie 与现有 Luma 导入入口一致，不混合不同时刻的会话。

只保存名称、规范化邮箱指纹、Cookie 指纹及 Cookie，不保存账号文件中的邮箱密码、手机号等其他字段。邮箱相同（忽略大小写）或规范化 Cookie 相同会跳过；已分配账号的指纹也参与去重。文件只读取上传，原始本地文件不会被移动或删除。

## 库存与任务状态

- **可分配账号**：尚未被任务预占的库存。
- **排队 / 导入中**：已预占、等待发送或正在发送。
- **待确认**：超时、连接中断、接口不兼容或回执缺失，仍保留凭据并绑定原服务器。
- **失败待处理**：远端明确拒绝的账号，保留供任务重试。
- **累计已分配**：确认入库的历史数量，包含目标已存在的账号。

「导入成功」表示目标服务器已经持久化保存账号，不代表已经激活或可以生成。后续激活和可用性由 Luma 服务管理。

收到成功回执后，监控端在事务中删除号池账号和 Cookie，只保留分配历史、去重指纹和远端 profile ID。目标已存在的账号也会移出中央号池。

失败或待确认任务可以点击「重试 / 确认原服务器」。重试沿用原任务账号的幂等键和目标；即使远端已经保存、响应丢失或后来删除了账号，也不会通过相同幂等键再次创建。

「取消未发送部分」只释放尚未发送的账号；已发送、待确认和失败项继续保留在原任务。如果当前批次仍在执行，页面会提示等待批次完成再操作。

服务非正常退出时，进行中的账号在最长约 5 分钟执行租约到期后自动恢复确认；连续 3 次未确认后等待手动处理。存在未完成任务的服务器不能删除或修改地址，可以更新登录凭据后重试。

## 存储和并发

- 默认数据库：`config/account_pool.sqlite3`，Docker 沿用 `./config:/app/config` 挂载。
- `MONITOR_ACCOUNT_POOL_DB`：自定义数据库路径。
- `MONITOR_IMPORT_PARALLELISM`：并行目标服务器数量，默认 3，范围 1–10，修改后重启生效。
- 每台服务器同时发送 1 批，每批 1–100 个账号，页面默认 50。
- Luma 端一批内最多 5 线程入库。导入请求超时为 60 秒，监控请求仍使用原超时配置。
- 手动分配与自动补号共用事务号池和后台任务。自动补号计算缺口时会计入待激活账号，并跳过已有未完成任务的目标。

首次启动自动迁移旧 `config/cookies.json`：可用账号进入库存，已分配账号进入历史，原 `assigning` / 失败账号进入「旧号池待核对」，不会自动重新分配。旧文件保留为迁移备份；迁移完成后不再作为活动号池读取，旧待核对数据需核对原目标后再处理。

## 新接口

监控端（沿用面板登录认证）：

- `GET /api/cookies`：库存统计。
- `POST /api/cookies/import`：账号数组或 `documents: [{name,text}]` 入池。
- `POST /api/import-jobs`：`allocations: [{target_id,count}], batch_size` 创建任务，返回 HTTP 202。
- `GET /api/import-jobs`：最近 50 个服务器任务和并发配置。
- `GET /api/import-jobs/{id}?offset=0`：账号明细，每页 100 条，不返回 Cookie。
- `POST /api/import-jobs/{id}/retry`：确认 / 重试。
- `POST /api/import-jobs/{id}/cancel`：取消尚未发送部分。

Luma 端（沿用目标管理员登录认证）：

- `GET /api/v1/refresh-profiles/dispatch-capabilities`
- `POST /api/v1/refresh-profiles/import-dispatch`
- `POST /api/v1/refresh-profiles/dispatch-receipts`
