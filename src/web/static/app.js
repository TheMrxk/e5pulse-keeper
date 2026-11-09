/* E5Pulse panel front-end logic (vanilla JS, no framework, no build step) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const api = {
  async req(method, path, body) {
    const opt = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const resp = await fetch(path, opt);
    let data = null;
    try { data = await resp.json(); } catch { /* non-JSON response */ }
    if (resp.status === 401) { showLogin(); throw new Error("Unauthorized"); }
    if (!resp.ok) {
      let msg = `Request failed ${resp.status}`;
      if (data) {
        if (typeof data.detail === "string") msg = data.detail;
        else if (Array.isArray(data.detail) && data.detail.length) {
          // FastAPI 422 validation error: detail is [{loc, msg, type}, ...]
          msg = data.detail
            .map((d) => d.msg || JSON.stringify(d))
            .join("；");
        } else if (data.reason) msg = data.reason;
      }
      throw new Error(msg);
    }
    return data;
  },
  get: (p) => api.req("GET", p),
  post: (p, b) => api.req("POST", p, b),
  put: (p, b) => api.req("PUT", p, b),
};

/* ---------------- Toast ---------------- */
let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

/* ---------------- time formatting ---------------- */
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtDur(iso) {
  if (!iso) return "—";
  const diff = new Date(iso) - new Date();
  if (diff <= 0) return "due now";
  const m = Math.floor(diff / 60000);
  if (m < 60) return `in ${m} min`;
  return `in ${Math.floor(m / 60)} h ${m % 60} min`;
}

/* ---------------- login / session ---------------- */
async function boot() {
  try {
    await api.get("/api/auth/check");
    showApp();
  } catch { showLogin(); }
}

function showLogin() {
  $("#login-view").classList.remove("hidden");
  $("#app-view").classList.add("hidden");
  stopPolling();
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  refreshAll();
  startPolling();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = $("#password").value;
  const errEl = $("#login-err");
  errEl.textContent = "";
  try {
    const r = await api.post("/api/auth/login", { password });
    if (r.ok !== false) { showApp(); $("#password").value = ""; }
    else errEl.textContent = r.detail || "Sign-in failed";
  } catch (err) { errEl.textContent = err.message; }
});

$("#btn-logout").addEventListener("click", async () => {
  try { await api.post("/api/auth/logout", {}); } catch {}
  showLogin();
});

/* ---------------- tab switching ---------------- */
$$(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    ["status", "config", "logs"].forEach((t) =>
      $(`#tab-${t}`).classList.toggle("hidden", t !== btn.dataset.tab));
    if (btn.dataset.tab === "logs") loadLogs();
    if (btn.dataset.tab === "config") loadConfig();
  });
});

/* ---------------- status tab ---------------- */
function badgeFor(acc) {
  if (!acc.enabled) return ['<span class="badge off">Disabled</span>'];
  if (acc.manual_pause) return ['<span class="badge pause">Manually paused</span>'];
  if (acc.pause_until && new Date(acc.pause_until) > new Date())
    return ['<span class="badge fail">Cooling down</span>'];
  return ['<span class="badge on">Running</span>'];
}

async function loadStatus() {
  let status, overview;
  try {
    [status, overview] = await Promise.all([
      api.get("/api/status"), api.get("/api/config/overview"),
    ]);
  } catch { return; }
  const cfgMap = Object.fromEntries(overview.accounts.map((a) => [a.name, a]));
  const box = $("#account-cards");
  if (!status.accounts.length) {
    box.innerHTML = '<div class="card muted">No accounts</div>';
    return;
  }
  box.innerHTML = status.accounts.map((acc) => {
    const cfg = cfgMap[acc.name] || {};
    const interval = cfg.interval || overview.default_interval;
    return `
    <div class="card" data-name="${escapeHtml(acc.name)}">
      <h3>${escapeHtml(acc.name)} ${badgeFor(acc).join("")}</h3>
      <div class="metrics">
        <div class="metric"><div class="num ok">${acc.total_success}</div><div class="label">Success</div></div>
        <div class="metric"><div class="num fail">${acc.total_failure}</div><div class="label">Failed</div></div>
        <div class="metric"><div class="num">${acc.consecutive_failures}</div><div class="label">Streak</div></div>
      </div>
      <div class="kv"><span class="k">Auth method</span><span class="v">${acc.auth_type === "client_credentials" ? "App permission (CC)" : "Password (ROPC)"}</span></div>
      <div class="kv"><span class="k">Interval</span><span class="v">${interval[0]}–${interval[1]} s</span></div>
      <div class="kv"><span class="k">Last run</span><span class="v">${fmtTime(acc.last_run_at)}</span></div>
      <div class="kv"><span class="k">Next run</span><span class="v">${fmtTime(acc.next_run_at)} <span class="muted">(${fmtDur(acc.next_run_at)})</span></span></div>
      ${acc.pause_until ? `<div class="kv"><span class="k">Paused until</span><span class="v">${fmtTime(acc.pause_until)}</span></div>` : ""}
      ${acc.last_error ? `<div class="kv"><span class="k">Last error</span><span class="v" style="color:#fca5a5">${escapeHtml(acc.last_error)}</span></div>` : ""}
      <div class="btn-row">
        <button class="btn primary sm act-trigger">&#9654; Run now</button>
        <button class="btn amber sm act-pause">&#10074;&#10074; Pause</button>
        <button class="btn green sm act-resume">&#9654; Resume</button>
      </div>
    </div>`;
  }).join("");
  $("#last-refresh").textContent = fmtTime(new Date().toISOString());

  // bind actions
  box.querySelectorAll(".card").forEach((card) => {
    const name = card.dataset.name;
    card.querySelector(".act-trigger").addEventListener("click", () => doTrigger(name));
    card.querySelector(".act-pause").addEventListener("click", () => doPause(name));
    card.querySelector(".act-resume").addEventListener("click", () => doResume(name));
  });
}

async function doTrigger(name) {
  try {
    const r = await api.post(`/api/accounts/${encodeURIComponent(name)}/trigger`, {});
    toast(r.ok ? `Triggered ${name}, succeeded` : `Trigger failed: ${r.reason || "unknown"}`, r.ok ? "ok" : "err");
  } catch (e) { toast(e.message, "err"); }
  loadStatus();
}
async function doPause(name) {
  try { await api.post(`/api/accounts/${encodeURIComponent(name)}/pause`, {}); toast(`${name} paused`, "ok"); }
  catch (e) { toast(e.message, "err"); }
  loadStatus();
}
async function doResume(name) {
  try { await api.post(`/api/accounts/${encodeURIComponent(name)}/resume`, {}); toast(`${name} resumed`, "ok"); }
  catch (e) { toast(e.message, "err"); }
  loadStatus();
}

/* ---------------- config tab ---------------- */
let configCache = null;
const WD_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function loadConfig() {
  try { configCache = await api.get("/api/config/overview"); }
  catch (e) { return toast(e.message, "err"); }
  const s = configCache.schedule;
  $("#sched-enabled").checked = s.enabled;
  // working days
  $("#workdays").innerHTML = WD_LABELS.map((label, i) => {
    const day = i + 1;
    const active = s.workdays.includes(day) ? "active" : "";
    return `<div class="wd ${active}" data-day="${day}">${label}</div>`;
  }).join("");
  $$("#workdays .wd").forEach((el) =>
    el.addEventListener("click", () => el.classList.toggle("active")));
  // time windows
  renderWindows(s.time_windows);
  // API toggles
  $("#api-list").innerHTML = configCache.default_apis.map((a) => `
    <div class="row-between">
      <div style="min-width:0">
        <div><span class="method">${a.method}</span> <strong>${escapeHtml(a.name)}</strong></div>
        <div class="api-url">${escapeHtml(a.url)}</div>
      </div>
      <label class="switch">
        <input type="checkbox" class="api-switch" data-api="${escapeHtml(a.name)}" ${a.enabled ? "checked" : ""}>
        <span class="slider"></span>
      </label>
    </div>`).join("");
  $$(".api-switch").forEach((sw) =>
    sw.addEventListener("change", () => saveApi(sw.dataset.api, sw.checked)));

  // global scheduling
  $("#g-min-interval").value = configCache.default_interval[0];
  $("#g-max-interval").value = configCache.default_interval[1];
  $("#g-concurrency").value = configCache.max_concurrent_accounts;
  $("#g-timeout").value = configCache.request_timeout;

  // error handling
  const eh = configCache.error_handling;
  $("#eh-max-fail").value = eh.max_consecutive_failures;
  $("#eh-pause-min").value = eh.pause_minutes;
  $("#eh-rate").value = eh.max_error_rate;
  $("#eh-window").value = eh.sliding_window_size;
  $("#eh-retry").value = eh.retry_times;
  $("#eh-backoff").value = eh.retry_backoff.join(", ");

  // call policy
  const cp = configCache.call_policy;
  $("#cp-min").value = cp.apis_per_round_min;
  $("#cp-max").value = cp.apis_per_round_max;
  $("#cp-delay-min").value = cp.inter_call_delay_min;
  $("#cp-delay-max").value = cp.inter_call_delay_max;
  $("#cp-shuffle").checked = cp.shuffle_order;

  // account settings (enable/disable + custom interval)
  $("#account-settings").innerHTML = configCache.accounts.map((a) => `
    <div class="acct-block" data-name="${escapeHtml(a.name)}">
      <div class="acct-head">
        <span>${escapeHtml(a.name)}</span>
        <label class="switch">
          <input type="checkbox" class="acct-enabled" ${a.enabled ? "checked" : ""}>
          <span class="slider"></span>
        </label>
      </div>
      <div class="acct-interval">
        <label>Custom min interval (s, blank=global)
          <input type="number" class="acct-min" min="30" placeholder="default ${configCache.default_interval[0]}">
        </label>
        <label>Custom max interval (s, blank=global)
          <input type="number" class="acct-max" min="30" placeholder="default ${configCache.default_interval[1]}">
        </label>
      </div>
      <div class="btn-row">
        <button class="btn primary sm acct-save">Save account</button>
      </div>
    </div>`).join("");
  $$(".acct-block").forEach((block) => {
    const name = block.dataset.name;
    block.querySelector(".acct-enabled").addEventListener("change",
      (e) => saveAccountEnabled(name, e.target.checked));
    block.querySelector(".acct-save").addEventListener("click",
      () => saveAccountInterval(
        name,
        block.querySelector(".acct-min").value,
        block.querySelector(".acct-max").value,
      ));
  });
}

async function saveAccountEnabled(name, enabled) {
  try {
    await api.put(`/api/config/accounts/${encodeURIComponent(name)}/enabled`, { enabled });
    toast(`${name} ${enabled ? "enabled" : "disabled"}`, "ok");
  } catch (e) { toast(e.message, "err"); loadConfig(); }
}

async function saveAccountInterval(name, minRaw, maxRaw) {
  // Both blank => fall back to global (backend cannot clear, use current global value)
  const min = minRaw === "" ? configCache.default_interval[0] : Number(minRaw);
  const max = maxRaw === "" ? configCache.default_interval[1] : Number(maxRaw);
  if (!Number.isFinite(min) || !Number.isFinite(max))
    return toast("Interval must be a number", "err");
  if (min > max) return toast("Min interval cannot exceed max interval", "err");
  try {
    await api.put(`/api/config/accounts/${encodeURIComponent(name)}/interval`,
      { min_interval: min, max_interval: max });
    toast(`${name} interval saved (${min}–${max} s)`, "ok");
  } catch (e) { toast(e.message, "err"); }
}

$("#btn-save-global").addEventListener("click", async () => {
  const minInterval = Number($("#g-min-interval").value);
  const maxInterval = Number($("#g-max-interval").value);
  const concurrency = Number($("#g-concurrency").value);
  const timeout = Number($("#g-timeout").value);
  if (minInterval > maxInterval) return toast("Default min interval cannot exceed max", "err");
  // Submit each independently so one invalid value does not block the others
  const tasks = [
    ["Interval", () => api.put("/api/config/global/interval",
      { min_interval: minInterval, max_interval: maxInterval })],
    ["Concurrency", () => api.put("/api/config/global/concurrency",
      { max_concurrent: concurrency })],
    ["Timeout", () => api.put("/api/config/global/timeout", { timeout })],
  ];
  const failed = [];
  for (const [label, fn] of tasks) {
    try { await fn(); } catch (e) { failed.push(`${label}(${e.message})`); }
  }
  if (failed.length) toast("Some settings failed: " + failed.join("; "), "err");
  else toast("Global settings saved and applied", "ok");
  loadConfig();
});

$("#btn-save-eh").addEventListener("click", async () => {
  const backoff = $("#eh-backoff").value
    .split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => Number.isFinite(n));
  if (!backoff.length) return toast("Provide at least one backoff value", "err");
  const body = {
    max_consecutive_failures: Number($("#eh-max-fail").value),
    pause_minutes: Number($("#eh-pause-min").value),
    max_error_rate: Number($("#eh-rate").value),
    sliding_window_size: Number($("#eh-window").value),
    retry_times: Number($("#eh-retry").value),
    retry_backoff: backoff,
  };
  try {
    await api.put("/api/config/error-handling", body);
    toast("Error handling saved and applied", "ok");
    loadConfig();
  } catch (e) { toast(e.message, "err"); }
});

$("#btn-save-cp").addEventListener("click", async () => {
  const body = {
    apis_per_round_min: Number($("#cp-min").value),
    apis_per_round_max: Number($("#cp-max").value),
    shuffle_order: $("#cp-shuffle").checked,
    inter_call_delay_min: Number($("#cp-delay-min").value),
    inter_call_delay_max: Number($("#cp-delay-max").value),
  };
  if (body.apis_per_round_min > body.apis_per_round_max)
    return toast("Min APIs per round cannot exceed max", "err");
  if (body.inter_call_delay_min > body.inter_call_delay_max)
    return toast("Min delay cannot exceed max delay", "err");
  try {
    await api.put("/api/config/call-policy", body);
    toast("Call policy saved and applied", "ok");
    loadConfig();
  } catch (e) { toast(e.message, "err"); }
});

function renderWindows(windows) {
  const box = $("#time-windows");
  box.innerHTML = windows.map((w, i) => `
    <div class="row-between window-row">
      <span class="muted">Window ${i + 1}</span>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="time" class="win-start" value="${w.start}">
        <span class="muted">to</span>
        <input type="time" class="win-end" value="${w.end}">
        <button class="btn ghost sm win-del">×</button>
      </div>
    </div>`).join("");
  box.querySelectorAll(".win-del").forEach((b) =>
    b.addEventListener("click", () => b.closest(".window-row").remove()));
}

$("#btn-add-window").addEventListener("click", () => {
  const box = $("#time-windows");
  const div = document.createElement("div");
  div.className = "row-between window-row";
  div.innerHTML = `<span class="muted">New window</span>
    <div style="display:flex;gap:8px;align-items:center">
      <input type="time" class="win-start" value="09:00">
      <span class="muted">to</span>
      <input type="time" class="win-end" value="18:00">
      <button class="btn ghost sm win-del">×</button>
    </div>`;
  box.appendChild(div);
  div.querySelector(".win-del").addEventListener("click", () => div.remove());
});

$("#btn-save-schedule").addEventListener("click", async () => {
  const workdays = $$("#workdays .wd.active").map((el) => Number(el.dataset.day));
  if (!workdays.length) return toast("Select at least one working day", "err");
  const time_windows = $$(".window-row").map((r) => ({
    start: r.querySelector(".win-start").value,
    end: r.querySelector(".win-end").value,
  }));
  for (const w of time_windows) {
    if (!w.start || !w.end || w.start >= w.end)
      return toast("Invalid window: start must be earlier than end", "err");
  }
  try {
    await api.put("/api/config/schedule", {
      enabled: $("#sched-enabled").checked, workdays, time_windows,
    });
    toast("Schedule saved and applied", "ok");
    loadConfig();
  } catch (e) { toast(e.message, "err"); }
});

async function saveApi(apiName, enabled) {
  try {
    await api.put("/api/config/apis/enabled", { api_name: apiName, enabled });
    toast(`${apiName} ${enabled ? "enabled" : "disabled"}`, "ok");
  } catch (e) {
    toast(e.message, "err");
    loadConfig(); // roll back UI on failure
  }
}

/* ---------------- logs tab ---------------- */
async function loadLogs() {
  try {
    const r = await api.get("/api/logs?lines=200");
    $("#log-box").textContent = r.lines.length ? r.lines.join("\n") : "(no logs yet)";
    if ($("#auto-logs").checked) $("#log-box").scrollTop = $("#log-box").scrollHeight;
  } catch (e) { $("#log-box").textContent = `Load failed: ${e.message}`; }
}
$("#btn-refresh-logs").addEventListener("click", loadLogs);

/* ---------------- polling ---------------- */
let pollTimer = null;
function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    if (!$("#tab-status").classList.contains("hidden")) loadStatus();
    if (!$("#tab-logs").classList.contains("hidden") && $("#auto-logs").checked) loadLogs();
  }, 15000);
}
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

async function refreshAll() {
  await loadStatus();
  // health check
  try { const h = await fetch("/health").then((r) => r.json());
    $("#health-dot").style.background = h.status === "ok" ? "#4ade80" : "#f87171";
  } catch { $("#health-dot").style.background = "#f87171"; }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- PWA registration ---------------- */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("#app-view").classList.contains("hidden")) refreshAll();
});

boot();
