const state = {
  token: localStorage.getItem("tyca_token") || "",
  currentRun: null,
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

function setUploadStatus(text, kind = "muted") {
  const element = $("uploadStatus");
  if (!element) return;
  element.textContent = text;
  element.className = `hint status-line ${kind}`;
}

async function loadMe() {
  const payload = await api("/api/me");
  setSession(`已登录：${payload.user.email}`);
  if (!payload.cookie.hasCookie) {
    showResult({ ok: false, error: "当前账号没有可用 Cookie，请重新扫码登录。" });
  }
}

async function uploadMarkdown() {
  const file = $("mdFile").files[0];
  if (!file) throw new Error("请选择 .md 文件。");
  setUploadStatus("正在读取 Markdown 并生成 adapter...", "working");
  const markdown = await file.text();
  const payload = await api("/api/runs", {
    method: "POST",
    body: JSON.stringify({ fileName: file.name, markdown }),
  });
  state.currentRun = payload.run;
  renderReview(payload.run);
  renderAdapter(payload.run);
  const validation = payload.run.adapterValidation || {};
  if (validation.ok) {
    setUploadStatus(`已生成 Run #${payload.run.id}，可以先预演上传。`, "success");
  } else {
    const errors = validation.errors || [];
    setUploadStatus(`已生成 Run #${payload.run.id}，但有 ${errors.length} 个阻断问题，请按审查表修正。`, "error");
  }
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
    renderTycaPreview(null);
    return;
  }
  editor.value = JSON.stringify(run.adapter, null, 2);
  renderTycaPreview(run.adapter);
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

function renderTycaPreview(adapter) {
  const root = $("tycaPreview");
  if (!root) return;
  const items = adapter && Array.isArray(adapter.items) ? adapter.items : [];
  if (!items.length) {
    root.className = "tyca-preview-empty";
    root.textContent = "上传 Markdown 后在这里预览 TYCA 页面效果。";
    return;
  }
  root.className = "tyca-preview-shell";
  root.innerHTML = `
    <aside class="tyca-preview-nav">
      ${items.map((item, index) => `
        <button class="${index === 0 ? "active" : ""}" data-preview-index="${index}">
          <span>${index + 1}</span>
          <strong>${escapeHtml(typeLabel(item.localType))}</strong>
          <em>${escapeHtml(previewTitle(item))}</em>
        </button>
      `).join("")}
    </aside>
    <article class="tyca-preview-main"></article>
  `;
  const main = root.querySelector(".tyca-preview-main");
  const buttons = Array.from(root.querySelectorAll("button[data-preview-index]"));
  const show = (index) => {
    buttons.forEach((button, buttonIndex) => button.classList.toggle("active", buttonIndex === index));
    main.innerHTML = renderTycaItem(items[index], index);
  };
  buttons.forEach((button) => button.addEventListener("click", () => show(Number(button.dataset.previewIndex))));
  show(0);
}

function renderTycaItem(item, index) {
  const payload = item.payload || {};
  const localType = item.localType || "";
  if (item.targetGroup === "application") return renderApplicationPreview(payload, localType, index);
  if (item.targetGroup === "oj") return renderOjPreview(payload, index);
  return renderChoicePreview(payload, localType, index);
}

function renderChoicePreview(payload, localType, index) {
  return `
    <div class="tyca-question-head">
      <span>${index + 1}</span>
      <div>
        <h3>${escapeHtml(payload.name || "未命名题目")}</h3>
        ${renderMeta(payload, localType)}
      </div>
    </div>
    <section class="tyca-question-body">${renderContent(payload.description, payload.contentFormat)}</section>
    <section class="tyca-options">
      ${(payload.options || []).map((option, optionIndex) => renderOption(option, optionIndex)).join("")}
    </section>
    ${payload.analysis ? `<section class="tyca-analysis"><h4>解析</h4>${renderContent(payload.analysis, payload.contentFormat)}</section>` : ""}
  `;
}

function renderApplicationPreview(payload, localType, index) {
  return `
    <div class="tyca-question-head">
      <span>${index + 1}</span>
      <div>
        <h3>${escapeHtml(payload.name || "未命名应用题")}</h3>
        ${renderMeta(payload, localType)}
      </div>
    </div>
    <section class="tyca-question-body">
      <h4>大题材料</h4>
      ${renderContent(payload.description, payload.contentFormat)}
    </section>
    <section class="tyca-subquestions">
      ${(payload.innerQuestionDetails || []).map((question, questionIndex) => `
        <div class="tyca-subquestion">
          <h4>小题 ${questionIndex + 1}</h4>
          ${renderContent(question.description, 0)}
          <div class="tyca-options">
            ${(question.optionDetails || []).map((option, optionIndex) => renderOption(option, optionIndex)).join("")}
          </div>
          ${question.analysis ? `<div class="tyca-analysis">${renderContent(question.analysis, 0)}</div>` : ""}
        </div>
      `).join("") || `<p class="hint">未识别到小题。</p>`}
    </section>
  `;
}

function renderOjPreview(payload, index) {
  const oj = payload.ojInfo || {};
  return `
    <div class="tyca-question-head">
      <span>${index + 1}</span>
      <div>
        <h3>${escapeHtml(payload.name || "未命名 OJ 题")}</h3>
        ${renderMeta(payload, "programming")}
      </div>
    </div>
    <section class="tyca-question-body">
      <h4>题目描述</h4>${renderContent(payload.description, payload.contentFormat)}
      <h4>输入描述</h4>${renderContent(oj.inputType || "", 1)}
      <h4>输出描述</h4>${renderContent(oj.outputType || "", 1)}
      <h4>样例</h4>
      ${(oj.example || []).map((sample, sampleIndex) => `
        <div class="tyca-sample"><strong>样例输入 ${sampleIndex + 1}</strong><pre>${escapeHtml(sample.in || "")}</pre></div>
        <div class="tyca-sample"><strong>样例输出 ${sampleIndex + 1}</strong><pre>${escapeHtml(sample.out || "")}</pre></div>
        ${sample.description ? `<p>${escapeHtml(sample.description)}</p>` : ""}
      `).join("") || `<p class="hint">未识别到样例。</p>`}
      ${oj.dataRange ? `<h4>数据范围</h4>${renderContent(oj.dataRange, 1)}` : ""}
    </section>
  `;
}

function renderMeta(payload, localType) {
  const knowledge = Array.isArray(payload.knowledgeArr) ? payload.knowledgeArr : [];
  return `
    <div class="tyca-meta">
      <span>${escapeHtml(typeLabel(localType))}</span>
      <span>难度 ${escapeHtml(String(payload.difficulty || 3))}</span>
      <span>考试难度 ${escapeHtml(String(payload.examDifficulty || "-"))}</span>
      ${knowledge.map((item) => `<span>${escapeHtml(String(item))}</span>`).join("")}
    </div>
  `;
}

function renderOption(option, index) {
  const letter = String.fromCharCode(65 + index);
  return `
    <div class="tyca-option ${option && option.isCorrect ? "correct" : ""}">
      <strong>${letter}</strong>
      <span>${renderInlineContent(option ? option.text : "")}</span>
      ${option && option.isCorrect ? "<em>正确答案</em>" : ""}
    </div>
  `;
}

function renderContent(value, contentFormat) {
  const text = String(value || "").trim();
  if (!text) return `<p class="hint">待确认</p>`;
  if (Number(contentFormat) === 0 || /<\/?[a-z][\s\S]*>/i.test(text)) {
    return sanitizeRichHtml(text);
  }
  return markdownToPreviewHtml(text);
}

function renderInlineContent(value) {
  return markdownToPreviewHtml(String(value || "")).replace(/^<p>|<\/p>$/g, "");
}

function markdownToPreviewHtml(value) {
  const escaped = escapeHtml(String(value || ""));
  const withCode = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");
  return withCode
    .split(/\n{2,}/)
    .map((paragraph) => `<p>${paragraph.replace(/\n/g, "<br>")}</p>`)
    .join("");
}

function sanitizeRichHtml(value) {
  const template = document.createElement("template");
  template.innerHTML = String(value || "");
  template.content.querySelectorAll("script, iframe, object, embed, link, style").forEach((node) => node.remove());
  template.content.querySelectorAll("*").forEach((node) => {
    Array.from(node.attributes).forEach((attr) => {
      if (/^on/i.test(attr.name) || /javascript:/i.test(attr.value)) node.removeAttribute(attr.name);
    });
  });
  return template.innerHTML;
}

function previewTitle(item) {
  return (item && item.payload && item.payload.name) || item.localId || "未命名题目";
}

function typeLabel(type) {
  return {
    single_choice: "单选题",
    multiple_choice: "多选题",
    true_false: "判断题",
    reading_program: "阅读程序",
    complete_program: "完善程序",
    programming: "OJ 题",
  }[type] || type || "未知题型";
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
  const button = $(id);
  button.addEventListener("click", async () => {
    const previousText = button.textContent;
    try {
      button.disabled = true;
      if (id === "uploadBtn") button.textContent = "生成中...";
      await handler();
    } catch (error) {
      showResult({ ok: false, error: error.message });
      if (id === "uploadBtn") {
        setUploadStatus(`生成失败：${error.message}`, "error");
      }
    } finally {
      button.disabled = false;
      button.textContent = previousText;
    }
  });
}

bind("uploadBtn", uploadMarkdown);
bind("saveAdapterBtn", saveAdapter);
bind("dryRunBtn", dryRun);
bind("submitBtn", submitRun);
bind("refreshHistoryBtn", loadHistory);
bind("logoutBtn", () => {
  localStorage.removeItem("tyca_token");
  window.location.href = "./login.html";
});

applyRuntimeConfig();

if (!state.token) {
  window.location.href = "./login.html";
} else {
  loadMe().then(loadHistory).catch(() => {
    localStorage.removeItem("tyca_token");
    window.location.href = "./login.html";
  });
}
