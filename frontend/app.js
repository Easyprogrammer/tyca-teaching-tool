const state = {
  token: localStorage.getItem("tyca_token") || "",
  currentRun: null,
  qrToken: "",
  qrTimer: null,
};

const $ = (id) => document.getElementById(id);

function apiBase() {
  return $("apiBase").value.replace(/\/$/, "");
}

function applyRuntimeConfig() {
  const config = window.TYCA_TOOL_CONFIG || {};
  if (config.apiBase) {
    $("apiBase").value = String(config.apiBase).replace(/\/$/, "");
  }
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`${apiBase()}${path}`, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function showResult(payload) {
  $("resultBox").textContent = JSON.stringify(payload, null, 2);
}

function setSession(text) {
  $("sessionStatus").textContent = text;
}

async function startQrLogin() {
  stopQrPolling();
  $("qrStatus").textContent = "正在生成钉钉二维码...";
  $("startQrLoginBtn").disabled = true;
  const payload = await api("/api/qrcode-login/start", { method: "POST" });
  state.qrToken = payload.qrcodeToken;
  $("qrBox").innerHTML = `<img alt="钉钉登录二维码" src="data:image/png;base64,${payload.qrcode}" />`;
  $("qrStatus").textContent = "请用钉钉扫码确认登录。";
  $("cancelQrLoginBtn").disabled = false;
  state.qrTimer = window.setInterval(checkQrLogin, 1200);
}

async function checkQrLogin() {
  if (!state.qrToken) return;
  const payload = await api(`/api/qrcode-login/status?token=${encodeURIComponent(state.qrToken)}`);
  if (payload.status === "pending") {
    $("qrStatus").textContent = "等待扫码确认...";
    return;
  }
  if (payload.status === "done") {
    stopQrPolling();
    state.qrToken = "";
    $("startQrLoginBtn").disabled = false;
    $("cancelQrLoginBtn").disabled = true;
    $("qrStatus").textContent = `登录成功：${payload.teacher?.name || payload.user.email}`;
    $("qrBox").innerHTML = "<span>已登录</span>";
    finishLogin(payload);
    return;
  }
  stopQrPolling();
  $("startQrLoginBtn").disabled = false;
  $("cancelQrLoginBtn").disabled = true;
  $("qrStatus").textContent = payload.error || "二维码已失效，请重新扫码。";
}

async function cancelQrLogin() {
  if (state.qrToken) {
    await api("/api/qrcode-login/cancel", {
      method: "POST",
      body: JSON.stringify({ token: state.qrToken }),
    });
  }
  stopQrPolling();
  state.qrToken = "";
  $("qrBox").innerHTML = "<span>未生成二维码</span>";
  $("qrStatus").textContent = "已取消扫码登录。";
  $("startQrLoginBtn").disabled = false;
  $("cancelQrLoginBtn").disabled = true;
}

function stopQrPolling() {
  if (state.qrTimer) {
    window.clearInterval(state.qrTimer);
    state.qrTimer = null;
  }
}

async function finishLogin(payload) {
  state.token = payload.token;
  localStorage.setItem("tyca_token", state.token);
  setSession(`已登录：${payload.teacher?.name || payload.user.email}`);
  await loadMe();
  await loadHistory();
}

async function loadMe() {
  const payload = await api("/api/me");
  setSession(`已登录：${payload.user.email}`);
  $("cookieStatus").textContent = payload.cookie.hasCookie
    ? `已配置 Cookie，更新时间：${formatTime(payload.cookie.updatedAt)}`
    : "尚未配置 TYCA Cookie。";
}

async function saveCookie() {
  const cookie = $("cookieInput").value.trim();
  if (!cookie) throw new Error("请先粘贴 cookie。");
  const payload = await api("/api/me/tyca-cookie", {
    method: "POST",
    body: JSON.stringify({ cookie }),
  });
  $("cookieInput").value = "";
  $("cookieStatus").textContent = `已配置 Cookie，更新时间：${formatTime(payload.cookie.updatedAt)}`;
  showResult({ ok: true, message: "Cookie 已更新，前端不会回显原文。" });
}

async function uploadMarkdown() {
  const file = $("mdFile").files[0];
  if (!file) throw new Error("请选择 .md 文件。");
  const markdown = await file.text();
  const payload = await api("/api/runs", {
    method: "POST",
    body: JSON.stringify({ fileName: file.name, markdown }),
  });
  state.currentRun = payload.run;
  renderReview(payload.run);
  renderAdapter(payload.run);
  showResult({ runId: payload.run.id, status: payload.run.status, adapterGenerated: Boolean(payload.run.adapter) });
  await loadHistory();
}

function renderReview(run) {
  const rows = run.review.items
    .map(
      (item) => `
        <tr>
          <td>${escapeHtml(String(item.index))}</td>
          <td>${escapeHtml(item.type)}</td>
          <td>${escapeHtml(item.title)}</td>
          <td>${escapeHtml(item.answer || "待确认")}</td>
          <td>${escapeHtml(String(item.difficulty))}</td>
          <td>${escapeHtml(item.examDifficulty)}</td>
          <td><span class="badge">${escapeHtml(item.status)}</span></td>
          <td>${escapeHtml((item.issues || []).join("；") || "无")}</td>
        </tr>
      `,
    )
    .join("");
  $("review").innerHTML = `
    <p class="hint">Run #${run.id}：${escapeHtml(run.fileName)}</p>
    <table>
      <thead>
        <tr>
          <th>题号</th>
          <th>题型</th>
          <th>标题</th>
          <th>答案</th>
          <th>难度</th>
          <th>考试难度</th>
          <th>状态</th>
          <th>问题</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
  renderAdapter(run);
}

function renderAdapter(run) {
  const editor = $("adapterEditor");
  const status = $("adapterStatus");
  if (!editor || !status) return;
  if (!run || !run.adapter) {
    editor.value = "";
    status.textContent = "上传 Markdown 后，服务器会自动生成 tyca-adapter.json。";
    return;
  }
  editor.value = JSON.stringify(run.adapter, null, 2);
  const validation = run.adapterValidation;
  if (!validation) {
    status.textContent = "Adapter 已生成，尚未校验。";
    return;
  }
  const errors = validation.errors || [];
  const warnings = validation.warnings || [];
  status.textContent = validation.ok
    ? `Adapter 校验通过；${warnings.length ? `警告 ${warnings.length} 条。` : "无警告。"}`
    : `Adapter 存在 ${errors.length} 个错误，需修正后再预演。`;
}

async function saveAdapter() {
  if (!state.currentRun) throw new Error("请先上传 Markdown 生成 run。");
  let adapter;
  try {
    adapter = JSON.parse($("adapterEditor").value);
  } catch (error) {
    throw new Error(`Adapter JSON 不合法：${error.message}`);
  }
  const payload = await api(`/api/runs/${state.currentRun.id}/adapter`, {
    method: "POST",
    body: JSON.stringify({ adapter }),
  });
  state.currentRun = payload.run;
  renderReview(payload.run);
  renderAdapter(payload.run);
  showResult({ ok: true, status: payload.run.status, validation: payload.run.adapterValidation });
  await loadHistory();
}

async function dryRun() {
  if (!state.currentRun) throw new Error("请先生成审查表。");
  const payload = await api(`/api/runs/${state.currentRun.id}/dry-run`, { method: "POST" });
  state.currentRun = payload.run;
  renderReview(payload.run);
  showResult(payload.run.dryRun);
  await loadHistory();
}

async function submitRun() {
  if (!state.currentRun) throw new Error("请先生成审查表。");
  if (state.currentRun.status !== "dry_run_passed") throw new Error("必须先通过预演上传。");
  const typed = prompt("正式上传需要二次确认。请输入 CONFIRM_SUBMIT");
  if (typed !== "CONFIRM_SUBMIT") {
    showResult({ ok: false, message: "已取消正式上传。" });
    return;
  }
  const payload = await api(`/api/runs/${state.currentRun.id}/submit`, {
    method: "POST",
    body: JSON.stringify({ confirm: typed }),
  });
  state.currentRun = payload.run;
  showResult(payload.run.submit);
  await loadHistory();
}

async function loadHistory() {
  const payload = await api("/api/runs");
  $("history").innerHTML = payload.runs
    .map(
      (run) => `
        <div class="history-item">
          <strong>#${run.id} ${escapeHtml(run.fileName)}</strong>
          <span class="badge">${escapeHtml(run.status)}</span>
          <button data-run-id="${run.id}">查看</button>
        </div>
      `,
    )
    .join("");
  $("history").querySelectorAll("button[data-run-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      const payload = await api(`/api/runs/${button.dataset.runId}`);
      state.currentRun = payload.run;
      renderReview(payload.run);
      renderAdapter(payload.run);
      showResult(payload.run);
    });
  });
}

function formatTime(epoch) {
  if (!epoch) return "未知";
  return new Date(epoch * 1000).toLocaleString();
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char];
  });
}

function bind(id, handler) {
  $(id).addEventListener("click", async () => {
    try {
      await handler();
    } catch (error) {
      showResult({ ok: false, error: error.message });
    }
  });
}

bind("startQrLoginBtn", startQrLogin);
bind("cancelQrLoginBtn", cancelQrLogin);
bind("saveCookieBtn", saveCookie);
bind("uploadBtn", uploadMarkdown);
bind("saveAdapterBtn", saveAdapter);
bind("dryRunBtn", dryRun);
bind("submitBtn", submitRun);
bind("refreshHistoryBtn", loadHistory);

applyRuntimeConfig();

if (state.token) {
  loadMe().then(loadHistory).catch(() => {
    localStorage.removeItem("tyca_token");
    state.token = "";
    setSession("未登录");
  });
}
