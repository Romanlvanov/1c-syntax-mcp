"use strict";

function getToken() {
  return localStorage.getItem("syntaxMcpPanelToken") || "";
}
function setToken(t) {
  localStorage.setItem("syntaxMcpPanelToken", t);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

let toastTimer = null;
function showToast(msg, isError) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  el.classList.toggle("error", !!isError);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);
}

// Общий 401-обработчик: панель защищена SYNTAX_MCP_PANEL_TOKEN опционально —
// сервер не сообщает панели, включена ли защита, поэтому реагируем на факт 401.
async function apiFetch(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers);
  const token = getToken();
  if (token) opts.headers["X-Panel-Token"] = token;
  let res = await fetch(path, opts);
  if (res.status === 401) {
    const entered = window.prompt(
      "Панель защищена токеном (SYNTAX_MCP_PANEL_TOKEN). Введите значение:"
    );
    if (entered) {
      setToken(entered);
      opts.headers["X-Panel-Token"] = entered;
      res = await fetch(path, opts);
    }
  }
  return res;
}

function apiPostJson(path, body) {
  return apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function apiDelete(path) {
  return apiFetch(path, { method: "DELETE" });
}

// PUT-загрузка raw-телом через XHR (не fetch): нужен xhr.upload.onprogress —
// fetch не даёт событий прогресса ОТДАЧИ тела при обычном использовании.
function uploadFile(version, kind, file, onProgress) {
  return new Promise((resolve, reject) => {
    function attempt(token) {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `/api/uploads/${encodeURIComponent(version)}/${kind}`);
      if (token) xhr.setRequestHeader("X-Panel-Token", token);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        if (xhr.status === 401) {
          const entered = window.prompt(
            "Панель защищена токеном (SYNTAX_MCP_PANEL_TOKEN). Введите значение:"
          );
          if (entered) {
            setToken(entered);
            attempt(entered);
            return;
          }
          reject(new Error("требуется токен панели"));
          return;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          let msg = xhr.responseText;
          try {
            msg = JSON.parse(xhr.responseText).error || msg;
          } catch (e) { /* тело не JSON -- оставляем как есть */ }
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error("сетевая ошибка при загрузке"));
      xhr.send(file);
    }
    attempt(getToken());
  });
}

function renderMcpInfo(mcpUrl) {
  document.getElementById("mcp-url").textContent = mcpUrl;
  document.getElementById("snippet-claude").textContent =
    `claude mcp add --transport http --scope user 1c-syntax ${mcpUrl}`;
  document.getElementById("snippet-opencode").textContent = JSON.stringify(
    { mcp: { "1c-syntax": { type: "remote", url: mcpUrl, enabled: true } } },
    null,
    2
  );
}

function statusBadge(row) {
  if (row.indexed === true) {
    return `<span class="badge ok">${row.entry_count} элементов</span>`;
  }
  if (row.indexed === "error") {
    return `<span class="badge error">повреждён, требуется пересборка</span>`;
  }
  if (row.has_help) {
    return `<span class="badge warn">справка есть, не собран</span>`;
  }
  return `<span class="badge muted">нет справки</span>`;
}

function sourceLabel(source) {
  if (source === "upload") return "загружено";
  if (source === "install") return "установка";
  return "—";
}

function renderVersions(rows, jobs) {
  document.getElementById("empty-state").classList.toggle("hidden", rows.length > 0);

  const runningByVersion = {};
  for (const j of jobs) {
    if (j.status === "running") runningByVersion[j.version] = j;
  }

  const tbody = document.querySelector("#versions-table tbody");
  tbody.innerHTML = "";
  for (const row of rows) {
    const job = runningByVersion[row.version];
    const canBuild = row.has_help && row.indexed !== true;
    const canRebuild = row.has_help && row.indexed === true;
    const canDelete = row.indexed === true || row.source === "upload";

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.version)}</td>
      <td>${sourceLabel(row.source)}</td>
      <td>${escapeHtml(row.hbk_kinds.join(", ") || "—")}</td>
      <td>${job
        ? `<span class="badge warn">сборка… ${job.done || 0}/${job.total || "?"}</span>`
        : statusBadge(row)}</td>
      <td>${row.is_default
        ? "✓"
        : row.indexed === true
          ? `<button data-act="default" data-version="${escapeHtml(row.version)}">Сделать</button>`
          : ""}</td>
      <td class="row-actions">
        ${canBuild ? `<button data-act="build" data-version="${escapeHtml(row.version)}">Собрать</button>` : ""}
        ${canRebuild ? `<button class="secondary" data-act="rebuild" data-version="${escapeHtml(row.version)}">Пересобрать</button>` : ""}
        ${canDelete ? `<button class="danger" data-act="delete" data-version="${escapeHtml(row.version)}">Удалить</button>` : ""}
      </td>`;
    tbody.appendChild(tr);
  }
}

let pollTimer = null;

async function refreshState() {
  clearTimeout(pollTimer);
  let data;
  try {
    const res = await fetch("/api/state");
    data = await res.json();
  } catch (e) {
    pollTimer = setTimeout(refreshState, 10000);
    return;
  }
  renderMcpInfo(data.mcp_url);
  renderVersions(data.versions, data.jobs);
  const hasActiveJob = data.jobs.some((j) => j.status === "running");
  pollTimer = setTimeout(refreshState, hasActiveJob ? 1500 : 10000);
}

async function trackBuildJob(jobId) {
  const box = document.getElementById("build-progress");
  const bar = box.querySelector(".bar");
  const statusEl = document.getElementById("build-status");
  box.classList.remove("hidden");
  for (;;) {
    let job;
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      job = await res.json();
    } catch (e) {
      await sleep(1500);
      continue;
    }
    const pct = job.total ? Math.round((100 * (job.done || 0)) / job.total) : 0;
    bar.style.width = pct + "%";
    statusEl.textContent = `${job.phase || "подготовка"} (${job.done || 0}/${job.total || "?"})`;
    if (job.status === "done") {
      const count = job.result && job.result.count;
      statusEl.textContent = `Готово: ${count != null ? count : "?"} элементов`;
      showToast("Индекс собран");
      break;
    }
    if (job.status === "error") {
      statusEl.textContent = `Ошибка: ${job.error}`;
      showToast(job.error, true);
      break;
    }
    await sleep(1000);
  }
  refreshState();
}

function initVersionActions() {
  document.querySelector("#versions-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const version = btn.dataset.version;
    const act = btn.dataset.act;

    if (act === "delete" && !confirm(`Удалить индекс версии ${version}? Загруженные файлы .hbk тоже будут удалены.`)) {
      return;
    }

    btn.disabled = true;
    try {
      if (act === "default") {
        const res = await apiPostJson("/api/default", { version });
        const body = await res.json();
        if (!res.ok) showToast(body.error || "ошибка", true);
      } else if (act === "build" || act === "rebuild") {
        const res = await apiPostJson(`/api/versions/${encodeURIComponent(version)}/build`, { force: act === "rebuild" });
        const body = await res.json();
        if (res.ok) {
          trackBuildJob(body.job_id);
        } else {
          showToast(body.error || "ошибка запуска сборки", true);
        }
      } else if (act === "delete") {
        const res = await apiDelete(`/api/versions/${encodeURIComponent(version)}?with_files=1`);
        const body = await res.json();
        if (!res.ok) showToast(body.error || "ошибка удаления", true);
      }
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      refreshState();
    }
  });
}

function initUploadForm() {
  document.getElementById("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const version = document.getElementById("version-input").value.trim();
    if (!/^\d+(\.\d+){1,3}$/.test(version)) {
      showToast("Некорректный формат версии, ожидается вида 8.5.1.1150", true);
      return;
    }

    const slots = Array.from(document.querySelectorAll(".slot"));
    for (const slot of slots) {
      const input = slot.querySelector('input[type="file"]');
      const kind = input.dataset.kind;
      const file = input.files[0];
      const statusEl = slot.querySelector(".slot-status");
      const bar = slot.querySelector(".bar");

      if (!file) {
        if (kind === "shcntx_ru") {
          showToast("shcntx_ru.hbk обязателен", true);
          return;
        }
        continue;
      }

      statusEl.textContent = "загрузка…";
      bar.style.width = "0%";
      try {
        const result = await uploadFile(version, kind, file, (frac) => {
          bar.style.width = Math.round(frac * 100) + "%";
        });
        statusEl.textContent = `загружено, ${result.member_count} элементов`;
      } catch (err) {
        statusEl.textContent = "ошибка: " + err.message;
        showToast(`${kind}: ${err.message}`, true);
        return;
      }
    }

    showToast("Файлы загружены, запускаю сборку…");
    const res = await apiPostJson(`/api/versions/${encodeURIComponent(version)}/build`, {});
    const body = await res.json();
    if (res.ok) {
      trackBuildJob(body.job_id);
    } else {
      showToast(body.error || "ошибка запуска сборки", true);
    }
    refreshState();
  });
}

function initSearchForm() {
  document.getElementById("search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const query = document.getElementById("search-query").value.trim();
    if (!query) return;

    const res = await apiPostJson("/api/search", { query, limit: 10 });
    const body = await res.json();
    const table = document.getElementById("search-results");
    const tbody = table.querySelector("tbody");
    tbody.innerHTML = "";

    if (!res.ok) {
      showToast(body.error || "ошибка поиска", true);
      table.classList.add("hidden");
      return;
    }

    for (const item of body.results) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(item.name_ru)}${item.name_en ? " / " + escapeHtml(item.name_en) : ""}</td>
        <td>${escapeHtml(item.owner_ru || "—")}</td>
        <td>${escapeHtml(item.kind)}</td>
        <td>${escapeHtml(item.tier)}</td>
        <td><code>${escapeHtml(item.signature || "")}</code></td>`;
      tbody.appendChild(tr);
    }
    table.classList.toggle("hidden", body.results.length === 0);
    if (body.results.length === 0) showToast("Ничего не найдено");
  });
}

function initTopbar() {
  document.getElementById("copy-mcp-url").addEventListener("click", async () => {
    const text = document.getElementById("mcp-url").textContent;
    try {
      await navigator.clipboard.writeText(text);
      showToast("Скопировано");
    } catch (e) {
      showToast("Не удалось скопировать — выделите адрес вручную", true);
    }
  });

  document.getElementById("toggle-snippets").addEventListener("click", () => {
    document.getElementById("snippets").classList.toggle("hidden");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initTopbar();
  initUploadForm();
  initVersionActions();
  initSearchForm();
  refreshState();
});
