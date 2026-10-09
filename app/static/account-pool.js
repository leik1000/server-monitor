(() => {
  const $ = (id) => document.getElementById(id);
  let available = 0;
  let loading = false;
  let uploading = false;
  let clearing = false;
  let refreshPending = false;
  let timer;
  let refillTarget = null;

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
        ["待确认", summary.unknown, ""], ["失败待处理", summary.failed, ""], ["累计已分配", summary.assigned, ""],
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
    $("dispatchTotal").textContent = `本次补号 ${fmtNumber(total)} 个${total > available ? "，库存不足" : ""}`;
  }

  async function openRefill(targetId) {
    if ($("dispatchDialog").open) return;
    refillTarget = null;
    $("dispatchMessage").textContent = "";
    $("dispatchTargetName").textContent = "正在读取服务器信息...";
    $("dispatchCount").value = "";
    $("dispatchAvailable").textContent = "正在读取库存...";
    $("dispatchTotal").textContent = "";
    $("submitDispatchBtn").disabled = true;
    $("dispatchDialog").showModal();
    try {
      const [result, summary] = await Promise.all([api("/api/targets"), api("/api/cookies")]);
      const target = result.targets.find((t) => t.id === targetId);
      if (!target || !target.enabled) throw new Error("该服务器不存在或已停用");
      refillTarget = target;
      available = summary.available;
      $("dispatchTargetName").textContent = `目标服务器：${target.name}（${target.base_url}）`;
      updateTotal();
      $("submitDispatchBtn").disabled = false;
      $("dispatchCount").focus();
    } catch (error) { $("dispatchMessage").textContent = error.message; }
  }

  $("dispatchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!refillTarget || $("submitDispatchBtn").disabled) return;
    const target = refillTarget;
    const count = Number($("dispatchCount").value);
    if (!Number.isInteger(count) || count < 1 || count > 100000) {
      $("dispatchMessage").textContent = "请输入 1–100000 之间的整数";
      return;
    }
    $("submitDispatchBtn").disabled = true;
    try {
      const result = await api("/api/import-jobs", {
        allocations: [{ target_id: target.id, count }],
        batch_size: target.refill_batch_size || 50,
      });
      $("dispatchDialog").close();
      $("cookieFormMessage").textContent = `已为 ${target.name} 创建补号任务，共 ${result.total} 个账号，后台处理中。`;
      await refresh();
    } catch (error) { $("dispatchMessage").textContent = error.message; }
    finally { $("submitDispatchBtn").disabled = false; }
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
