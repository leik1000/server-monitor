const els = {
  updatedAt: document.getElementById("updatedAt"),
  refreshBtn: document.getElementById("refreshBtn"),
  refreshBtnIcon: document.getElementById("refreshBtnIcon"),
  targets: document.getElementById("targets"),
  tpl: document.getElementById("targetTpl"),
  targetCount: document.getElementById("targetCount"),
  onlineCount: document.getElementById("onlineCount"),
  runningCount: document.getElementById("runningCount"),
  totalRequests: document.getElementById("totalRequests"),
  failedRequests: document.getElementById("failedRequests"),
  targetForm: document.getElementById("targetForm"),
  targetId: document.getElementById("targetId"),
  targetName: document.getElementById("targetName"),
  targetBaseUrl: document.getElementById("targetBaseUrl"),
  targetUsername: document.getElementById("targetUsername"),
  targetPassword: document.getElementById("targetPassword"),
  targetGroup: document.getElementById("targetGroup"),
  targetNote: document.getElementById("targetNote"),
  saveTargetBtn: document.getElementById("saveTargetBtn"),
  cancelEditBtn: document.getElementById("cancelEditBtn"),
  formMessage: document.getElementById("formMessage"),
  targetManagerList: document.getElementById("targetManagerList"),
  
  // Collapsible Manager
  toggleManagerBtn: document.getElementById("toggleManagerBtn"),
  closeManagerBtn: document.getElementById("closeManagerBtn"),
  managerCard: document.getElementById("managerCard"),
  
  // System Config
  toggleSystemBtn: document.getElementById("toggleSystemBtn"),
  closeSystemBtn: document.getElementById("closeSystemBtn"),
  systemCard: document.getElementById("systemCard"),
  systemForm: document.getElementById("systemForm"),
  systemUsername: document.getElementById("systemUsername"),
  systemPassword: document.getElementById("systemPassword"),
  systemFormMessage: document.getElementById("systemFormMessage"),
  saveSystemBtn: document.getElementById("saveSystemBtn"),
  
  // Group Tabs Container
  groupTabsBar: document.getElementById("groupTabsBar"),
};

let refreshTimer = null;
let latestTargets = [];
let currentGroup = "全部";
const expandedLogsTargets = new Set();
const expandedTargets = new Set();

function fmtNumber(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return "0";
  return n.toLocaleString();
}

function fmtCredits(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return "0";
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtTime(ts) {
  const n = Number(ts || 0);
  if (!n) return "-";
  return new Date(n * 1000).toLocaleString();
}

function fmtDuration(value) {
  const n = Number(value || 0);
  if (!n) return "-";
  return `${n}s`;
}

function buildMinuteBuckets(items = []) {
  const rows = Array.isArray(items) ? items : [];
  const nowMinute = Math.floor(Date.now() / 60000);
  const byMinute = new Map();
  for (const item of rows) {
    const bucket = Number(item?.minute_bucket || 0);
    if (bucket > 0) byMinute.set(bucket, item);
  }
  return Array.from({ length: 60 }, (_, index) => {
    const minuteBucket = nowMinute - 59 + index;
    return byMinute.get(minuteBucket) || {
      minute_bucket: minuteBucket,
      start_ts: minuteBucket * 60,
      end_ts: (minuteBucket + 1) * 60,
      completed_count: 0,
      avg_duration_sec: null,
    };
  });
}

function getBucketClass(avgDurationSec, completedCount, target = {}) {
  if (!target.online || target.error) return "bad";
  if (completedCount <= 0) return "idle";
  if (avgDurationSec < 100) return "good";
  if (avgDurationSec <= 300) return "warn";
  return "bad";
}

function buildBucketTitle(item, target = {}) {
  const endTs = Number(item?.end_ts || 0);
  const completedCount = Number(item?.completed_count || 0);
  const avgDurationSec = Number(item?.avg_duration_sec || 0);
  const minuteText = endTs > 0
    ? new Date((endTs - 1) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "-";
  if (!target.online || target.error) {
    return `${minuteText} 服务器异常：${target.error || "离线"}`;
  }
  if (completedCount <= 0) return `${minuteText} 空闲，无已完成任务`;
  return `${minuteText} 完成 ${completedCount} 个，平均耗时 ${avgDurationSec}s`;
}

function renderSummary(summary = {}) {
  els.targetCount.textContent = fmtNumber(summary.target_count);
  els.onlineCount.textContent = `${fmtNumber(summary.online_count)} / ${fmtNumber(summary.target_count)}`;
  els.runningCount.textContent = fmtNumber(summary.in_progress_requests);
  els.totalRequests.textContent = fmtNumber(summary.total_requests);
  els.failedRequests.textContent = fmtNumber(summary.failed_requests);
}

function renderTimeline(container, items = [], target = {}) {
  container.innerHTML = "";
  const list = buildMinuteBuckets(items);
  const maxCount = Math.max(1, ...list.map((item) => Number(item.completed_count || 0)));
  for (const item of list) {
    const count = Number(item.completed_count || 0);
    const avg = Number(item.avg_duration_sec || 0);
    const bucket = document.createElement("span");
    bucket.className = `monitor-bucket ${getBucketClass(avg, count, target)}`;
    bucket.style.height = `${Math.max(8, Math.round((count / maxCount) * 42))}px`;
    bucket.title = buildBucketTitle(item, target);
    container.appendChild(bucket);
  }
}

function setHealthPill(card, key, text, state) {
  const pill = card.querySelector(`[data-health="${key}"]`);
  if (!pill) return;
  pill.textContent = text;
  pill.className = `health-pill ${state}`;
}

function renderHealthStrip(card, target, stats, token) {
  const running = Number(stats.in_progress_requests || 0);
  const failed = Number(stats.failed_requests || 0);
  const total = Number(stats.total_requests || 0);
  const active = Number(token.active || 0);
  const accountTotal = Number(token.total || 0);
  const failRate = total > 0 ? failed / total : 0;
  setHealthPill(
    card,
    "availability",
    target.online ? `在线 ${target.latency_ms || 0}ms` : "离线",
    target.online ? "good" : "bad",
  );
  setHealthPill(
    card,
    "load",
    `当前任务 ${fmtNumber(running)}`,
    running <= 0 ? "idle" : running <= 5 ? "good" : running <= 15 ? "warn" : "bad",
  );
  setHealthPill(
    card,
    "errors",
    `失败 ${fmtNumber(failed)}`,
    failRate <= 0 ? "good" : failRate <= 0.1 ? "warn" : "bad",
  );
  setHealthPill(
    card,
    "accounts",
    `账号 ${fmtNumber(active)}/${fmtNumber(accountTotal)}`,
    accountTotal <= 0 ? "bad" : active <= 0 ? "bad" : active < accountTotal * 0.2 ? "warn" : "good",
  );
}

function renderLogs(tbody, logs = []) {
  tbody.innerHTML = "";
  if (!logs.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td colspan="5" class="empty" style="padding:1.5rem; text-align:center;">暂无请求记录</td>';
    tbody.appendChild(tr);
    return;
  }
  for (const log of logs) {
    const status = String(log.task_status || log.status_code || "-");
    let statusClass = "";
    if (status.toLowerCase() === "success" || status === "200" || status.toLowerCase() === "completed") {
      statusClass = "success";
    } else if (status.toLowerCase() === "failed" || status.toLowerCase() === "error" || Number(status) >= 400) {
      statusClass = "error";
    } else if (status.toLowerCase() === "processing" || status.toLowerCase() === "running") {
      statusClass = "progress";
    }
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(log.ts)}</td>
      <td>${escapeHtml(log.operation || log.model || "-")}</td>
      <td><span class="log-status-pill ${statusClass}">${escapeHtml(status)}</span></td>
      <td>${fmtDuration(log.duration_sec)}</td>
      <td style="color:var(--bad); font-size:0.72rem; word-break:break-all;">${escapeHtml(log.error || "")}</td>
    `;
    tbody.appendChild(tr);
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  }[ch]));
}

function renderTargetUrl(url) {
  if (!url) return "";
  const safeUrl = escapeHtml(url);
  return `<a class="target-url" href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeUrl}</a>`;
}

function renderTargetMeta(parts = []) {
  return parts.filter(Boolean).map(escapeHtml).join(" · ");
}

function renderGroupTabs(targets = []) {
  const groups = new Set();
  for (const target of targets) {
    if (target.group && target.group.trim()) {
      groups.add(target.group.trim());
    }
  }
  
  els.groupTabsBar.innerHTML = "";
  if (groups.size === 0) {
    els.groupTabsBar.style.display = "none";
    return;
  }
  els.groupTabsBar.style.display = "flex";
  
  // "全部" Tab
  const allTab = document.createElement("button");
  allTab.className = `group-tab ${currentGroup === "全部" ? "active" : ""}`;
  allTab.textContent = "全部";
  allTab.addEventListener("click", () => {
    currentGroup = "全部";
    renderTargets(latestTargets);
    renderGroupTabs(latestTargets);
  });
  els.groupTabsBar.appendChild(allTab);
  
  // 分组 Tab
  for (const group of Array.from(groups).sort()) {
    const tab = document.createElement("button");
    tab.className = `group-tab ${currentGroup === group ? "active" : ""}`;
    tab.textContent = group;
    tab.addEventListener("click", () => {
      currentGroup = group;
      renderTargets(latestTargets);
      renderGroupTabs(latestTargets);
    });
    els.groupTabsBar.appendChild(tab);
  }
}

function renderTargets(targets = []) {
  latestTargets = Array.isArray(targets) ? targets : [];
  renderTargetManager(latestTargets);
  els.targets.innerHTML = "";
  
  // 过滤分组
  const filteredTargets = currentGroup === "全部" 
    ? latestTargets 
    : latestTargets.filter(t => t.group && t.group.trim() === currentGroup);

  if (!filteredTargets.length) {
    els.targets.innerHTML = '<div class="empty">暂无该分组下的服务器监控目标。</div>';
    return;
  }

  for (const target of filteredTargets) {
    const node = els.tpl.content.cloneNode(true);
    const card = node.querySelector(".target-card");
    const name = node.querySelector(".target-name");
    const meta = node.querySelector(".target-meta");
    const statusWrapper = node.querySelector(".target-status-wrapper");
    const status = node.querySelector(".target-status");
    const error = node.querySelector(".error-text");
    const stats = target.stats || {};
    const token = target.token_summary || {};
    const failed = Number(stats.failed_requests || 0);
    const total = Number(stats.total_requests || 0);
    const failRate = total > 0 ? failed / total : 0;
    
    name.textContent = target.name || "-";
    meta.innerHTML = [
      renderTargetMeta([target.group, target.ip]),
      renderTargetUrl(target.base_url),
      renderTargetMeta([target.note]),
    ].filter(Boolean).join(" · ");
    status.textContent = target.online ? "在线" : "离线";
    statusWrapper.classList.add(target.online ? "online" : "offline");
    
    if (target.error && target.error !== "disabled") {
      error.textContent = target.error;
    } else {
      error.textContent = "";
    }
    
    for (const item of card.querySelectorAll("[data-key]")) {
      item.textContent = fmtNumber(stats[item.dataset.key]);
    }
    for (const item of card.querySelectorAll("[data-token]")) {
      const key = item.dataset.token;
      item.textContent = key === "credits_available_total" ? fmtCredits(token[key]) : fmtNumber(token[key]);
    }
    
    renderHealthStrip(card, target, stats, token);
    renderTimeline(card.querySelector(".timeline"), stats.recent_completed_timeline || [], target);
    renderLogs(card.querySelector("tbody"), target.recent_logs || []);
    
    // 折叠日志处理
    const logsBody = card.querySelector(".recent-logs-body");
    if (expandedLogsTargets.has(target.id)) {
      logsBody.classList.add("expanded");
    }
    
    // 折叠卡片状态还原
    if (expandedTargets.has(target.id)) {
      card.classList.add("expanded");
    }
    
    // 编辑和删除按钮的 ID 绑定
    const editBtn = card.querySelector(".btn-edit");
    const deleteBtn = card.querySelector(".btn-delete");
    if (editBtn) editBtn.dataset.id = target.id || "";
    if (deleteBtn) deleteBtn.dataset.id = target.id || "";
    
    els.targets.appendChild(node);
  }
}

function renderTargetManager(targets = []) {
  els.targetManagerList.innerHTML = "";
  if (!targets.length) {
    els.targetManagerList.innerHTML = '<div class="manager-empty">暂无服务器配置</div>';
    return;
  }
  for (const target of targets) {
    const row = document.createElement("div");
    row.className = "manager-row";
    row.innerHTML = `
      <div>
        <strong>${escapeHtml(target.name || "-")}</strong>
        <span>${[
          renderTargetMeta([target.group, target.ip]),
          renderTargetUrl(target.base_url),
          renderTargetMeta([target.username ? `用户 ${target.username}` : "未配置用户名"]),
        ].filter(Boolean).join(" · ")}</span>
      </div>
      <div class="manager-row-actions">
        <button class="secondary small btn-row-edit" type="button" data-action="edit" data-id="${escapeHtml(target.id || "")}">编辑</button>
        <button class="danger small btn-row-delete" type="button" data-action="delete" data-id="${escapeHtml(target.id || "")}">删除</button>
      </div>
    `;
    els.targetManagerList.appendChild(row);
  }
}

function getFormPayload() {
  return {
    name: els.targetName.value.trim(),
    base_url: els.targetBaseUrl.value.trim(),
    ip: "",
    username: els.targetUsername.value.trim(),
    password: els.targetPassword.value.trim(),
    group: els.targetGroup.value.trim(),
    note: els.targetNote.value.trim(),
    enabled: true,
  };
}

function resetForm() {
  els.targetForm.reset();
  els.targetId.value = "";
  els.targetPassword.required = true;
  els.targetPassword.placeholder = "新增必填；编辑留空表示不修改";
  els.saveTargetBtn.textContent = "添加服务器";
  els.cancelEditBtn.hidden = true;
  els.formMessage.textContent = "";
}

function startEdit(targetId) {
  const target = latestTargets.find((item) => String(item.id || "") === String(targetId || ""));
  if (!target) return;
  els.targetId.value = target.id || "";
  els.targetName.value = target.name || "";
  els.targetBaseUrl.value = target.base_url || "";
  els.targetUsername.value = target.username || "";
  els.targetPassword.value = "";
  els.targetPassword.required = !target.has_password;
  els.targetPassword.placeholder = target.has_password
    ? "已保存密码，留空表示不修改"
    : "未保存密码，请输入";
  els.targetGroup.value = target.group || "";
  els.targetNote.value = target.note || "";
  els.saveTargetBtn.textContent = "保存修改";
  els.cancelEditBtn.hidden = false;
  els.formMessage.textContent = `正在编辑：${target.name || "-"}`;
  
  // 自动展开管理面板
  els.managerCard.classList.remove("collapsed");
  els.targetName.focus();
}

async function saveTarget(event) {
  event.preventDefault();
  const id = els.targetId.value.trim();
  const payload = getFormPayload();
  els.saveTargetBtn.disabled = true;
  els.formMessage.textContent = id ? "正在保存..." : "正在添加...";
  try {
    const res = await fetch(id ? `/api/targets/${encodeURIComponent(id)}` : "/api/targets", {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    resetForm();
    await loadStatus(true);
  } catch (err) {
    els.formMessage.textContent = `保存失败：${err.message || err}`;
  } finally {
    els.saveTargetBtn.disabled = false;
  }
}

async function deleteTarget(targetId) {
  const target = latestTargets.find((item) => String(item.id || "") === String(targetId || ""));
  if (!target) return;
  if (!confirm(`确定删除 ${target.name || "该服务器"}？`)) return;
  try {
    const res = await fetch(`/api/targets/${encodeURIComponent(targetId)}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (els.targetId.value === targetId) resetForm();
    await loadStatus(true);
  } catch (err) {
    els.formMessage.textContent = `删除失败：${err.message || err}`;
  }
}

function handleTargetAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const id = button.dataset.id;
  if (action === "edit") startEdit(id);
  if (action === "delete") deleteTarget(id);
}

async function loadStatus(force = false) {
  els.refreshBtn.disabled = true;
  if (els.refreshBtnIcon) {
    els.refreshBtnIcon.classList.add("spinning");
  }
  try {
    const res = await fetch(force ? "/api/refresh" : "/api/status", { method: force ? "POST" : "GET" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    latestTargets = Array.isArray(data.targets) ? data.targets : [];
    
    renderSummary(data.summary || {});
    renderTargets(latestTargets);
    renderGroupTabs(latestTargets);
    
    els.updatedAt.textContent = `更新于 ${fmtTime(data.updated_at)}`;
    if (refreshTimer) clearInterval(refreshTimer);
    const seconds = Math.max(5, Number(data.refresh_seconds || 15));
    refreshTimer = setInterval(() => loadStatus(false), seconds * 1000);
  } catch (err) {
    els.updatedAt.textContent = `加载失败：${err.message || err}`;
  } finally {
    els.refreshBtn.disabled = false;
    if (els.refreshBtnIcon) {
      els.refreshBtnIcon.classList.remove("spinning");
    }
  }
}

// Collapsible Form Bindings
if (els.toggleManagerBtn) {
  els.toggleManagerBtn.addEventListener("click", () => {
    els.managerCard.classList.toggle("collapsed");
    if (els.systemCard) els.systemCard.classList.add("collapsed");
  });
}
if (els.closeManagerBtn) {
  els.closeManagerBtn.addEventListener("click", () => {
    els.managerCard.classList.add("collapsed");
  });
}

// System Config Bindings
if (els.toggleSystemBtn) {
  els.toggleSystemBtn.addEventListener("click", async () => {
    els.systemCard.classList.toggle("collapsed");
    if (els.managerCard) els.managerCard.classList.add("collapsed");
    
    if (!els.systemCard.classList.contains("collapsed")) {
      els.systemFormMessage.textContent = "正在读取配置...";
      els.saveSystemBtn.disabled = true;
      try {
        const res = await fetch("/api/system/config");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        els.systemUsername.value = data.username || "";
        els.systemPassword.value = "";
        els.systemFormMessage.textContent = "";
      } catch (err) {
        els.systemFormMessage.textContent = `读取失败：${err.message || err}`;
      } finally {
        els.saveSystemBtn.disabled = false;
      }
    }
  });
}
if (els.closeSystemBtn) {
  els.closeSystemBtn.addEventListener("click", () => {
    els.systemCard.classList.add("collapsed");
  });
}

async function saveSystemConfig(event) {
  event.preventDefault();
  const username = els.systemUsername.value.trim();
  const password = els.systemPassword.value.trim();
  
  els.saveSystemBtn.disabled = true;
  els.systemFormMessage.textContent = "正在保存系统设置...";
  try {
    const res = await fetch("/api/system/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    els.systemPassword.value = "";
    els.systemFormMessage.textContent = "保存成功！";
  } catch (err) {
    els.systemFormMessage.textContent = `保存失败：${err.message || err}`;
  } finally {
    els.saveSystemBtn.disabled = false;
  }
}

// Collapsible Logs Delegation Binding
els.targets.addEventListener("click", (event) => {
  const header = event.target.closest('[data-action="toggle-logs"]');
  if (!header) return;
  const body = header.nextElementSibling;
  const card = header.closest(".target-card");
  
  // 查找 targetId (通过 edit/delete 按钮)
  const editBtn = card.querySelector('.btn-edit');
  const targetId = editBtn ? editBtn.dataset.id : null;
  
  if (body.classList.contains("expanded")) {
    body.classList.remove("expanded");
    if (targetId) expandedLogsTargets.delete(targetId);
  } else {
    body.classList.add("expanded");
    if (targetId) expandedLogsTargets.add(targetId);
  }
});

// Collapsible Card Delegation Binding
els.targets.addEventListener("click", (event) => {
  if (event.target.closest("button") || event.target.closest("a") || event.target.closest("input") || event.target.closest(".recent-logs-wrapper") || event.target.closest(".target-actions")) {
    return;
  }
  const card = event.target.closest(".target-card");
  if (!card) return;
  
  const editBtn = card.querySelector(".btn-edit");
  const targetId = editBtn ? editBtn.dataset.id : null;
  
  if (card.classList.contains("expanded")) {
    card.classList.remove("expanded");
    if (targetId) expandedTargets.delete(targetId);
  } else {
    card.classList.add("expanded");
    if (targetId) expandedTargets.add(targetId);
  }
});

els.refreshBtn.addEventListener("click", () => loadStatus(true));
els.targetForm.addEventListener("submit", saveTarget);
els.cancelEditBtn.addEventListener("click", resetForm);
els.systemForm.addEventListener("submit", saveSystemConfig);
els.targets.addEventListener("click", handleTargetAction);
els.targetManagerList.addEventListener("click", handleTargetAction);
loadStatus(false);
