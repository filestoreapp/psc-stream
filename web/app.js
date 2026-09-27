/* PSC Stream frontend — plain JS, no build step. All backend calls are /api/*. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  status: null,
  videos: [],       // all loaded video metas (pages appended)
  page: 0,
  hasMore: false,
  query: "",
  scanTimer: null,
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!data.ok) {
    const err = new Error(data.message || ("Request failed (" + res.status + ")"));
    err.code = data.error;
    err.status = res.status;
    throw err;
  }
  return data;
}
const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

/* ---------------- view switching ---------------- */
const VIEWS = ["login", "library", "watch", "settings"];
function showView(name) {
  for (const v of VIEWS) $("view-" + v).classList.toggle("hidden", v !== name);
  for (const b of document.querySelectorAll(".nav-btn"))
    b.classList.toggle("active", b.dataset.view === name);
  if (name === "watch") stopScanPoll();
}
$("nav-library").onclick = () => { showView("library"); };
$("nav-settings").onclick = () => { showView("settings"); loadSettingsForm(); };
$("brand-home").onclick = () => { showView("library"); };

/* ---------------- helpers ---------------- */
function showError(elId, msg) {
  const el = $(elId);
  el.textContent = msg;
  el.classList.remove("hidden");
}
function hideError(elId) { $(elId).classList.add("hidden"); }
function fmtDuration(s) {
  if (!s) return "";
  const m = Math.floor(s / 60), sec = s % 60;
  return m + ":" + String(sec).padStart(2, "0");
}
function fmtSize(b) {
  if (!b) return "";
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  return (b / 1e6).toFixed(0) + " MB";
}
function setBusy(btn, busy, label) {
  btn.disabled = busy;
  if (label !== undefined) btn.dataset.label = btn.textContent;
  btn.textContent = busy ? "Please wait…" : (btn.dataset.label || btn.textContent);
}

/* ---------------- boot ---------------- */
async function boot() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    showError("login-error", "Cannot reach the local server: " + e.message);
    showView("login");
    return;
  }
  $("mock-badge").classList.toggle("hidden", !state.status.mock);
  $("net-status").className = "status-dot " + (state.status.authorized ? "ok" : "bad");
  $("net-status").title = state.status.authorized ? "Signed in" : "Not signed in";
  if (state.status.authorized) {
    await enterLibrary();
  } else {
    showView("login");
    showStep(1);
  }
}

/* ---------------- login wizard ---------------- */
function showStep(n) {
  for (const s of [1, 2, 3]) $("step-" + s).classList.toggle("hidden", s !== n);
  hideError("login-error");
}

$("btn-send-code").onclick = async (e) => {
  const btn = e.target;
  hideError("login-error");
  const apiId = $("in-api-id").value.trim();
  const apiHash = $("in-api-hash").value.trim();
  const phone = $("in-phone").value.trim();
  if (!apiId || !apiHash || !phone) {
    showError("login-error", "Fill in api_id, api_hash and phone number.");
    return;
  }
  setBusy(btn, true, "Send login code");
  try {
    const r = await post("/api/auth/start", { api_id: apiId, api_hash: apiHash, phone });
    if (r.next === "done") { await loginDone(); }
    else showStep(2);
  } catch (err) { showError("login-error", err.message); }
  finally { setBusy(btn, false); }
};

$("btn-verify-code").onclick = async (e) => {
  const btn = e.target;
  hideError("login-error");
  setBusy(btn, true, "Verify");
  try {
    const r = await post("/api/auth/code", { code: $("in-code").value.trim() });
    if (r.next === "password") showStep(3);
    else await loginDone();
  } catch (err) { showError("login-error", err.message); }
  finally { setBusy(btn, false); }
};

$("btn-verify-password").onclick = async (e) => {
  const btn = e.target;
  hideError("login-error");
  setBusy(btn, true, "Sign in");
  try {
    await post("/api/auth/password", { password: $("in-password").value });
    await loginDone();
  } catch (err) { showError("login-error", err.message); }
  finally { setBusy(btn, false); }
};

$("btn-back-1").onclick = () => showStep(1);
$("btn-back-2").onclick = () => showStep(2);

async function loginDone() {
  $("in-password").value = "";
  $("net-status").className = "status-dot ok";
  await enterLibrary();
}

/* ---------------- library ---------------- */
async function enterLibrary() {
  showView("library");
  state.videos = [];
  state.page = 0;
  await loadPage(0);
  const st = await api("/api/status").catch(() => null);
  if (st) {
    if (!st.channel) {
      $("scan-note").textContent = "No channel set yet — open Settings and add your channel.";
      $("scan-note").classList.remove("hidden");
    } else if (st.scan.status === "running") {
      startScanPoll();
    } else {
      $("scan-note").classList.add("hidden");
    }
  }
}

async function loadPage(page) {
  const r = await api("/api/videos?page=" + page);
  state.page = r.page;
  state.hasMore = r.has_more;
  if (page === 0) state.videos = r.videos;
  else state.videos = state.videos.concat(r.videos);
  renderGrid();
  $("lib-count").textContent = r.total ? "(" + r.total + ")" : "";
  $("btn-more").classList.toggle("hidden", !r.has_more);
  $("empty-lib").classList.toggle("hidden", state.videos.length > 0);
}

$("btn-more").onclick = () => loadPage(state.page + 1).catch((e) => alert(e.message));

$("search").oninput = (e) => {
  state.query = e.target.value.trim().toLowerCase();
  renderGrid();
};

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  const q = state.query;
  const list = q
    ? state.videos.filter((v) => v.title.toLowerCase().includes(q))
    : state.videos;
  for (const v of list) {
    const card = document.createElement("div");
    card.className = "card";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = "/api/thumb/" + v.id;
    const body = document.createElement("div");
    body.className = "card-body";
    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = v.title;
    const meta = document.createElement("div");
    meta.className = "card-meta";
    meta.textContent = [fmtDuration(v.duration), fmtSize(v.size), v.date]
      .filter(Boolean).join(" · ");
    body.appendChild(title);
    body.appendChild(meta);
    card.appendChild(img);
    card.appendChild(body);
    card.onclick = () => openWatch(v);
    grid.appendChild(card);
  }
  $("empty-lib").classList.toggle("hidden", list.length > 0 || state.videos.length > 0);
}

/* ---------------- watch ---------------- */
function openWatch(v) {
  const player = $("player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  $("watch-title").textContent = v.title;
  $("watch-meta").textContent = [fmtDuration(v.duration), fmtSize(v.size), v.date]
    .filter(Boolean).join(" · ");
  // The browser requests byte ranges itself; our /api/stream answers 206.
  player.src = "/api/stream/" + v.id;
  showView("watch");
  player.play().catch(() => {}); // autoplay may be blocked; controls remain
}

$("btn-back-watch").onclick = () => {
  const player = $("player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  showView("library");
};

/* ---------------- settings ---------------- */
function loadSettingsForm() {
  hideError("settings-error");
  $("settings-ok").classList.add("hidden");
  if (state.status && state.status.channel) $("in-channel").value = state.status.channel;
  refreshScanStatus();
}

$("btn-save-channel").onclick = async (e) => {
  const btn = e.target;
  hideError("settings-error");
  $("settings-ok").classList.add("hidden");
  setBusy(btn, true, "Save channel");
  try {
    const r = await post("/api/settings/channel", { channel: $("in-channel").value.trim() });
    state.status = await api("/api/status");
    const ok = $("settings-ok");
    ok.textContent = "Channel saved: " + (r.title || "ok") + ". Press “Rescan now”.";
    ok.classList.remove("hidden");
    $("scan-note").classList.add("hidden");
  } catch (err) { showError("settings-error", err.message); }
  finally { setBusy(btn, false); }
};

async function startRescan() {
  hideError("settings-error");
  try {
    await post("/api/rescan");
    startScanPoll();
  } catch (err) { showError("settings-error", err.message); }
}
$("btn-rescan").onclick = startRescan;
$("btn-rescan-lib").onclick = async () => {
  showView("settings");
  await startRescan();
};

function startScanPoll() {
  stopScanPoll();
  refreshScanStatus();
  state.scanTimer = setInterval(refreshScanStatus, 2000);
}
function stopScanPoll() {
  if (state.scanTimer) { clearInterval(state.scanTimer); state.scanTimer = null; }
}

async function refreshScanStatus() {
  const el = $("scan-status");
  if (!el) return;
  const st = await api("/api/status").catch(() => null);
  if (!st) return;
  state.status = st;
  const s = st.scan;
  if (s.status === "running") {
    el.textContent = "Scanning… " + s.count + " videos found so far.";
  } else if (s.status === "done") {
    el.textContent = "Scan complete: " + s.count + " videos.";
    stopScanPoll();
    if (!$("view-library").classList.contains("hidden")) { await loadPage(0); }
  } else if (s.status === "error") {
    el.textContent = "Scan failed: " + (s.error || "unknown error");
    stopScanPoll();
  } else {
    el.textContent = st.total_videos ? ("Library: " + st.total_videos + " videos cached.") : "";
  }
}

$("btn-logout").onclick = async (e) => {
  if (!confirm("Log out and remove the saved Telegram session from this laptop?")) return;
  const btn = e.target;
  setBusy(btn, true, "Log out");
  try {
    await post("/api/auth/logout");
    state.videos = [];
    renderGrid();
    $("net-status").className = "status-dot bad";
    showView("login");
    showStep(1);
  } catch (err) { showError("settings-error", err.message); }
  finally { setBusy(btn, false); }
};

document.addEventListener("DOMContentLoaded", boot);
