(() => {
  const $ = (id) => document.getElementById(id);
  let available = 0;
  let loading = false;
  let uploading = false;
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

  async function refresh() {
    if (loading) return;
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
      timer = setTimeout(refresh, delay);
    }
  }

  function setUploadBusy(busy) {
    uploading = busy;
    ["accountFiles", "accountFolder", "importCookiesBtn"].forEach((id) => { $(id).disabled = busy; });
    $("importCookiesBtn").textContent = busy ? "正在导入..." : "导入到号池";
    $("accountImportDialog").setAttribute("aria-busy", String(busy));
    if (busy) {
      $("accountUploadMessage").textContent = "正在读取账号并准备导入...";
      $("uploadErrorDetails").hidden = true;
    }
  }

  async function uploadDocuments(documents) {
    return api("/api/cookies/import", { documents });
  }

  function showUploadResult(totals, processed, total, errors, finished = false) {
    const message = `${finished ? "上传完成" : "上传中"} ${processed}/${total} 文件 · 新增 ${totals.imported_count} · 重复 ${totals.duplicate_count} · 无效 ${totals.invalid_count}`;
    $("accountUploadMessage").textContent = message;
    $("cookieFormMessage").textContent = message;
    $("uploadErrorDetails").hidden = !errors.length;
    $("uploadErrors").textContent = errors.slice(0, 100).map((e) => `${e.source || "粘贴内容"} 第 ${e.index || 0} 条：${e.error}`).join("\n");
  }

  async function importFiles(event) {
    if (uploading) return;
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
      await refresh();
    }
    try {
      for (const file of files) {
        if (file.size > 4 * 1024 * 1024) {
          totals.invalid_count++; processed++;
          errors.push({ source: file.name, error: "单文件超过 4MB，请拆分" });
          continue;
        }
        const document = { name: file.webkitRelativePath || file.name, text: await file.text() };
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
    if (uploading) return;
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
