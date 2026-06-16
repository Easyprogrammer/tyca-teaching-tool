const state = {
  token: localStorage.getItem("tyca_token") || "",
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
    localStorage.setItem("tyca_token", payload.token);
    $("qrStatus").textContent = `登录成功：${payload.teacher?.name || payload.user.email}`;
    $("qrBox").innerHTML = "<span>已登录</span>";
    window.location.href = "./";
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

function bind(id, handler) {
  $(id).addEventListener("click", async () => {
    try {
      await handler();
    } catch (error) {
      $("qrStatus").textContent = error.message;
      $("startQrLoginBtn").disabled = false;
    }
  });
}

applyRuntimeConfig();
bind("startQrLoginBtn", startQrLogin);
bind("cancelQrLoginBtn", cancelQrLogin);

if (state.token) {
  api("/api/me")
    .then(() => {
      window.location.href = "./";
    })
    .catch(() => {
      localStorage.removeItem("tyca_token");
      state.token = "";
    });
}
