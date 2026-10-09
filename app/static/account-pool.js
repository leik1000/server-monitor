(() => {
  const $ = (id) => document.getElementById(id);
  let available = 0;
  let loading = false;
  let uploading = false;
  let clearing = false;
  let refreshPending = false;
  let timer;
  let refillTarget = null;
  let refillBusy = false;

  async function api(path, payload) {
    const response = await fetch(path, payload === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `请求失败 HTTP ${response.status}`);
    return data;
  }

  async function refresh(force = false) {
    if (loading) { if (force) refreshPending = true; return; }
    loading = true;
    let delay = 15000;
    try {
      const summary = await api("/api/cookies");
      available = summary.available;
      $("centralAccountCount").textContent = fmtNumber(summary.total);
      $("cookiePoolSummary").innerHTML = [
        ["可分配账号", summary.available, "pool-available"], ["排队 / 导入中", summary.assigning, ""],
        ["待确认", summary.unknown, ""], ["失败待处理", summary.failed, ""], ["今日已分配", summary.assigned, ""],
        ...(summary.legacy_review ? [["旧号池待核对", summary.legacy_review, ""]] : []),
      ].map(([label, value, cls]) => `<div class="pool-stat ${cls}"><span>${label}</span><strong>${fmtNumber(value)}</strong></div>`).join("");
      if (summary.assigning > 0 || summary.unknown > 0) delay = 2000;
      if ($("dispatchDialog").open) updateTotal();
    } catch (error) {
      $("cookiePoolSummary").textContent = `库存读取失败：${error.message}`;
      $("centralAccountCount").textContent = "-";
    } finally {
      loading = false;
      clearTimeout(timer);
      timer = setTimeout(refresh, refreshPending ? 0 : delay);
      refreshPending = false;
    }
  }

  function setUploadBusy(busy) {
    uploading = busy;
    $("clearAccountPoolBtn").disabled = busy || clearing;
    ["accountFiles", "accountFolder", "importCookiesBtn"].forEach((id) => { $(id).disabled = busy; });
    $("importCookiesBtn").textContent = busy ? "正在导入..." : "导入到号池";
    $("accountImportDialog").setAttribute("aria-busy", String(busy));
    if (busy) {
      $("accountUploadMessage").textContent = "正在读取账号并准备导入...";
      $("uploadErrorDetails").hidden = true;
    }
  }

  async function uploadDocuments(documents) {
    const prepared = documents.map((document) => ({ ...document, text: compactDocument(document.text) }));
    const raw = new Blob([JSON.stringify({ documents: prepared })], { type: "application/json" });
    let body = raw;
    let compressed = false;
    if (typeof CompressionStream !== "undefined" && raw.size > 4096) {
      $("accountUploadMessage").textContent = `正在压缩 ${documents.length} 个文件的账号资料...`;
      try {
        const gzip = await new Response(raw.stream().pipeThrough(new CompressionStream("gzip"))).blob();
        if (gzip.size < raw.size) { body = gzip; compressed = true; }
      } catch (_) {
        // Older browsers still support the uncompressed import protocol.
      }
    }
    const kb = Math.max(1, Math.round(body.size / 1024));
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/cookies/import");
      xhr.timeout = 120000;
      xhr.setRequestHeader("Content-Type", "application/json");
      if (compressed) xhr.setRequestHeader("Content-Encoding", "gzip");
      const progressText = (text) => {
        $("accountUploadMessage").textContent = text;
        $("cookieFormMessage").textContent = text;
      };
      progressText(`正在上传 ${documents.length} 个文件 · ${kb} KB${compressed ? "（已压缩）" : ""} · 0%`);
      xhr.upload.onprogress = (event) => {
        const percent = event.lengthComputable ? Math.round(event.loaded / event.total * 100) : 0;
        progressText(percent >= 100
          ? "上传进度 100%，正在等待服务器确认结果..."
          : `正在上传 ${documents.length} 个文件 · ${kb} KB${compressed ? "（已压缩）" : ""} · ${percent}%`);
      };
      xhr.onload = () => {
        let result;
        try { result = JSON.parse(xhr.responseText); }
        catch (_) { reject(new Error(`服务器返回异常响应（HTTP ${xhr.status}）`)); return; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(result);
        else reject(new Error(typeof result.detail === "string" ? result.detail : `导入失败 HTTP ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error("上传连接中断，请检查网络；重新导入会自动跳过已保存账号"));
      xhr.ontimeout = () => reject(new Error("上传超过 120 秒，已停止等待；可重新导入，已保存账号会自动去重"));
      xhr.onabort = () => reject(new Error("上传已取消"));
      xhr.send(body);
    });
  }

  function compactDocument(text) {
    const present = (value) => value && (Array.isArray(value) ? value.length > 0
      : typeof value === "object" ? Object.keys(value).length > 0 : true);
    const account = (item) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return item;
      const cookie = [item.cookie, item.cookies, item._usage?.cookie_header, item.session_cookies].find(present);
      if (!cookie) return item;
      return { email: item.email || item._bind_email || "", name: item.name || "", cookie };
    };
    try {
      const parsed = JSON.parse(text.replace(/^\uFEFF/, ""));
      if (Array.isArray(parsed)) return JSON.stringify(parsed.map(account));
      if (parsed && Array.isArray(parsed.items)) return JSON.stringify({ items: parsed.items.map(account) });
      return JSON.stringify(account(parsed));
    } catch (_) {
      return text; // Backend retains authoritative validation and error reporting.
    }
  }

  function showUploadResult(totals, processed, total, errors, finished = false) {
    const message = `${finished ? "上传完成" : "上传中"} ${processed}/${total} 文件 · 新增 ${totals.imported_count} · 重复 ${totals.duplicate_count} · 无效 ${totals.invalid_count}`;
    $("accountUploadMessage").textContent = message;
    $("cookieFormMessage").textContent = message;
    $("uploadErrorDetails").hidden = !errors.length;
    $("uploadErrors").textContent = errors.slice(0, 100).map((e) => `${e.source || "粘贴内容"} 第 ${e.index || 0} 条：${e.error}`).join("\n");
  }

  async function importFiles(event) {
    if (uploading || clearing) return;
    const files = Array.from(event.target.files || []).filter((f) => /\.(json|txt)$/i.test(f.name));
    if (!files.length) { $("accountUploadMessage").textContent = "请选择 JSON 或 TXT 文件"; return; }
    setUploadBusy(true);
    const totals = { imported_count: 0, duplicate_count: 0, invalid_count: 0 };
    const errors = [];
    let processed = 0;
    let batch = [];
    let size = 0;
    async function flush() {
      if (!batch.length) return;
      const result = await uploadDocuments(batch);
      for (const key of Object.keys(totals)) totals[key] += result[key] || 0;
      errors.push(...(result.errors || []));
      processed += batch.length;
      batch = []; size = 0;
      showUploadResult(totals, processed, files.length, errors);
      void refresh();
    }
    try {
      for (const file of files) {
        $("accountUploadMessage").textContent = `正在读取文件 ${processed + batch.length + 1}/${files.length}...`;
        if (file.size > 4 * 1024 * 1024) {
          totals.invalid_count++; processed++;
          errors.push({ source: file.name, error: "单文件超过 4MB，请拆分" });
          continue;
        }
        const document = { name: file.webkitRelativePath || file.name, text: compactDocument(await file.text()) };
        const bytes = new TextEncoder().encode(JSON.stringify(document)).length;
        if (batch.length >= 50 || size + bytes > 8 * 1024 * 1024) await flush();
        batch.push(document); size += bytes;
      }
      await flush();
      showUploadResult(totals, processed, files.length, errors, true);
    } catch (error) {
      $("cookieFormMessage").textContent = `上传中断，已处理 ${processed}/${files.length} 文件，新增 ${totals.imported_count}：${error.message}。可重新选择全部文件，已入池账号会自动跳过。`;
      $("accountUploadMessage").textContent = $("cookieFormMessage").textContent;
    } finally {
      event.target.value = "";
      setUploadBusy(false);
      await refresh();
    }
  }

  async function importText() {
    const text = $("cookieImportText").value.trim();
    if (uploading || clearing) return;
    if (!text) { $("accountUploadMessage").textContent = "请粘贴账号内容，或选择账号文件上传"; return; }
    setUploadBusy(true);
    try {
      const result = await uploadDocuments([{ name: "粘贴内容", text }]);
      showUploadResult(result, 1, 1, result.errors || [], true);
      if (!result.invalid_count) $("cookieImportText").value = "";
      await refresh();
    } catch (error) {
      $("accountUploadMessage").textContent = `入池失败：${error.message}`;
      $("cookieFormMessage").textContent = $("accountUploadMessage").textContent;
    } finally { setUploadBusy(false); }
  }

  function updateTotal() {
    const total = Number($("dispatchCount").value || 0);
    $("dispatchAvailable").textContent = `号池可分配：${fmtNumber(available)}`;
    const automatic = $("dispatchMode").value === "auto";
    $("dispatchManualFields").hidden = automatic;
    $("dispatchAutoFields").hidden = !automatic;
    $("dispatchCount").disabled = automatic || refillBusy;
    $("dispatchCount").required = !automatic;
    $("dispatchThreshold").disabled = !automatic || refillBusy;
    $("dispatchThreshold").required = automatic;
    $("submitDispatchBtn").textContent = automatic ? (refillTarget?.refill_enabled ? "保存自动补号设置" : "保存并开启") : "确认补号";
    $("disableAutoRefillBtn").hidden = !refillTarget?.refill_enabled;
    $("dispatchModeNotice").textContent = refillTarget?.refill_enabled
      ? (automatic ? "关闭自动补号后不再创建新任务，已创建的任务继续执行。" : "确认手动补号时将关闭自动补号；已创建的任务继续执行。") : "";
    $("dispatchTotal").textContent = automatic
      ? `可用账号低于 ${fmtNumber($("dispatchThreshold").value)} 时触发；补充量会扣除待激活账号。`
      : `本次补号 ${fmtNumber(total)} 个${total > available ? "，库存不足" : ""}`;
  }

  function setRefillBusy(busy) {
    refillBusy = busy;
    $("submitDispatchBtn").disabled = busy || !refillTarget || !refillTarget.enabled;
    $("disableAutoRefillBtn").disabled = busy || !refillTarget;
    $("dispatchMode").disabled = busy || !refillTarget;
    $("closeDispatchBtn").disabled = busy;
    updateTotal();
  }

  async function saveRefillSettings(enabled) {
    const result = await api(`/api/targets/${encodeURIComponent(refillTarget.id)}/refill-settings`, {
      enabled, ...(enabled ? { threshold: Number($("dispatchThreshold").value) } : {}),
    });
    refillTarget = result.target;
    await loadStatus(false);
    return result;
  }

  async function openRefill(targetId) {
    if ($("dispatchDialog").open) return;
    refillTarget = null;
    $("dispatchMessage").textContent = "";
    $("dispatchTargetName").textContent = "正在读取服务器信息...";
    $("dispatchCount").value = "";
    $("dispatchMode").value = "manual";
    $("dispatchThreshold").value = "";
    $("dispatchAvailable").textContent = "正在读取库存...";
    $("dispatchTotal").textContent = "";
    setRefillBusy(true);
    $("dispatchDialog").showModal();
    try {
      const [result, summary] = await Promise.all([api("/api/targets"), api("/api/cookies")]);
      const target = result.targets.find((t) => t.id === targetId);
      if (!target) throw new Error("该服务器不存在");
      refillTarget = target;
      $("dispatchMode").value = target.refill_enabled ? "auto" : "manual";
      $("dispatchThreshold").value = target.refill_threshold || "";
      available = summary.available;
      $("dispatchTargetName").textContent = `目标服务器：${target.name}（${target.base_url}）`;
      updateTotal();
      if (target.refill_enabled && (target.refill_mode !== "target" || target.refill_target !== target.refill_threshold || !target.refill_threshold)) {
        $("dispatchMessage").textContent = "检测到旧补号规则。保存后改为按可用账号阈值触发，并补齐至该阈值。";
      }
      if (!target.enabled) $("dispatchMessage").textContent = "服务器已停用，仍可关闭自动补号。";
    } catch (error) { $("dispatchMessage").textContent = error.message; }
    finally {
      setRefillBusy(false);
      ($("dispatchMode").value === "auto" ? $("dispatchThreshold") : $("dispatchCount")).focus();
    }
  }

  $("dispatchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!refillTarget || $("submitDispatchBtn").disabled) return;
    const target = refillTarget;
    const automatic = $("dispatchMode").value === "auto";
    const count = Number($("dispatchCount").value);
    const threshold = Number($("dispatchThreshold").value);
    if (automatic && (!Number.isInteger(threshold) || threshold < 1 || threshold > 100000)) {
      $("dispatchMessage").textContent = "可用账号阈值必须为 1–100000 之间的整数";
      return;
    }
    if (!automatic && (!Number.isInteger(count) || count < 1 || count > 100000)) {
      $("dispatchMessage").textContent = "请输入 1–100000 之间的整数";
      return;
    }
    if (!automatic && count > available) {
      $("dispatchMessage").textContent = "号池库存不足，请先导入账号";
      return;
    }
    setRefillBusy(true);
    $("dispatchMessage").textContent = "正在保存...";
    try {
      if (automatic) {
        await saveRefillSettings(true);
        $("dispatchDialog").close();
        $("cookieFormMessage").textContent = `已为 ${target.name} 开启自动补号，可用账号低于 ${threshold} 时自动补齐。`;
        return;
      }
      if (target.refill_enabled) await saveRefillSettings(false);
      const result = await api("/api/import-jobs", {
        allocations: [{ target_id: target.id, count }],
        batch_size: target.refill_batch_size || 50,
      });
      $("dispatchDialog").close();
      $("cookieFormMessage").textContent = `已为 ${target.name} 创建补号任务，共 ${result.total} 个账号，后台处理中。`;
      await refresh();
    } catch (error) {
      $("dispatchMessage").textContent = `${!automatic && target.refill_enabled && !refillTarget.refill_enabled ? "自动补号已关闭；" : ""}${error.message}`;
    }
    finally { setRefillBusy(false); }
  });

  $("disableAutoRefillBtn").addEventListener("click", async () => {
    if (refillBusy || !refillTarget) return;
    setRefillBusy(true);
    try {
      await saveRefillSettings(false);
      $("dispatchMode").value = "manual";
      $("dispatchMessage").textContent = "自动补号已关闭，已创建的任务继续执行。";
    } catch (error) { $("dispatchMessage").textContent = error.message; }
    finally { setRefillBusy(false); }
  });

  $("clearAccountPoolBtn").addEventListener("click", async () => {
    if (uploading || clearing) return;
    if (!confirm("确定清空中央号池中的全部账号吗？\n\n可用、排队、失败和待确认账号都会删除，未完成任务将取消，此操作无法撤销。\n不会删除各服务器已有账号；已经发出的补号请求可能仍会完成。")) return;
    clearing = true;
    const button = $("clearAccountPoolBtn");
    button.disabled = true;
    button.textContent = "正在清空...";
    ["openAccountImportBtn", "accountFiles", "accountFolder", "importCookiesBtn"].forEach((id) => { $(id).disabled = true; });
    try {
      const result = await api("/api/cookies/clear", {});
      $("cookieFormMessage").textContent = `中央号池已清空，删除 ${fmtNumber(result.deleted_count)} 个账号${result.cancelled_jobs ? `，取消 ${fmtNumber(result.cancelled_jobs)} 个任务` : ""}。`;
      await refresh(true);
    } catch (error) {
      $("cookieFormMessage").textContent = `清空失败：${error.message}`;
    } finally {
      clearing = false;
      button.disabled = false;
      button.textContent = "清空号池";
      ["openAccountImportBtn", "accountFiles", "accountFolder", "importCookiesBtn"].forEach((id) => { $(id).disabled = false; });
    }
  });

  $("closeDispatchBtn").addEventListener("click", () => $("dispatchDialog").close());
  $("dispatchCount").addEventListener("input", updateTotal);
  $("dispatchThreshold").addEventListener("input", updateTotal);
  $("dispatchMode").addEventListener("change", updateTotal);
  $("dispatchDialog").addEventListener("cancel", (event) => { if (refillBusy) event.preventDefault(); });
  $("accountFiles").addEventListener("change", importFiles);
  $("openAccountImportBtn").addEventListener("click", () => {
    $("accountImportDialog").showModal();
    if (!uploading) $("cookieImportText").focus();
  });
  $("closeAccountImportBtn").addEventListener("click", () => $("accountImportDialog").close());
  $("accountFolder").addEventListener("change", importFiles);
  window.accountPoolUI = { refresh, importText, openRefill };
  refresh();
})();
