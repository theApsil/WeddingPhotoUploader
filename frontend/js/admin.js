import {
  BASE_PATH,
  applySiteCopy,
  hideHomeForKnownGuest,
  formatBytes,
  formatWhen,
  showAlert,
} from "./common.js";

const TOKEN_KEY = "admin_token";
const PAGE = 48;

const els = {
  loginCard: document.getElementById("login-card"),
  panel: document.getElementById("panel"),
  password: document.getElementById("admin-password"),
  loginBtn: document.getElementById("login-btn"),
  loginAlert: document.getElementById("login-alert"),
  adminAlert: document.getElementById("admin-alert"),
  adminStatus: document.getElementById("admin-status"),
  adminList: document.getElementById("admin-list"),
  adminMore: document.getElementById("admin-more"),
  adminTotal: document.getElementById("admin-total"),
  showHidden: document.getElementById("show-hidden"),
  pendingOnly: document.getElementById("pending-only"),
  guestFilter: document.getElementById("admin-guest-filter"),
  archiveBtn: document.getElementById("archive-btn"),
  logoutBtn: document.getElementById("logout-btn"),
};

const state = {
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  items: [],
  offset: 0,
  total: 0,
  kind: "all",
  guest: "",
  loading: false,
};

function token() {
  return state.token;
}

async function adminFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token()) headers.Authorization = `Bearer ${token()}`;
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${BASE_PATH}${path}`, { ...options, headers });
  let data = null;
  const text = await res.text();
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text || res.statusText };
  }
  if (!res.ok) {
    const err = new Error(data?.detail || `Ошибка ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function setStatus(message, hidden = false) {
  els.adminStatus.hidden = hidden;
  els.adminStatus.textContent = message || "";
}

function renderTotal() {
  const label = state.kind === "all" ? "Всего" : state.kind === "video" ? "Видео" : "Фото";
  els.adminTotal.textContent = `${label} · ${state.total}`;
}

function renderMore() {
  const remaining = state.total - state.items.length;
  if (state.items.length > 0 && remaining > 0) {
    els.adminMore.hidden = false;
    els.adminMore.textContent = `Показать ещё · осталось ${remaining}`;
    els.adminMore.disabled = state.loading;
  } else {
    els.adminMore.hidden = true;
  }
}

/** Does an item still belong to the list under the current filters? */
function matchesFilters(item) {
  if (Boolean(item.hidden) !== els.showHidden.checked) return false;
  if (els.pendingOnly.checked && !item.pending) return false;
  return true;
}

/** Apply a PATCH result; drop the row if it left the current filter. */
function applyUpdate(item, updated) {
  item.hidden = updated.hidden;
  item.pending = updated.pending;
  if (!matchesFilters(item)) {
    state.items = state.items.filter((it) => it.id !== item.id);
    state.total = Math.max(0, state.total - 1);
    // Server-side the row left this filter, so the next page starts one earlier.
    state.offset = state.items.length;
  }
  renderList(false);
  showAlert(els.adminAlert, "", "error");
}

function rowActions(item, li) {
  const actions = document.createElement("div");
  actions.className = "admin-actions";

  // Moderation: pending uploads get approve / reject controls.
  if (item.pending) {
    const approveBtn = document.createElement("button");
    approveBtn.type = "button";
    approveBtn.className = "btn btn-small";
    approveBtn.textContent = "Одобрить";
    approveBtn.addEventListener("click", async () => {
      try {
        const updated = await adminFetch(`/api/admin/photos/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ pending: false }),
        });
        applyUpdate(item, updated);
      } catch (err) {
        showAlert(els.adminAlert, `Не удалось одобрить: ${err.message}`, "error");
      }
    });
    const rejectBtn = document.createElement("button");
    rejectBtn.type = "button";
    rejectBtn.className = "btn btn-danger btn-small";
    rejectBtn.textContent = "Отклонить";
    rejectBtn.addEventListener("click", async () => {
      if (!confirm("Скрыть файл от гостей?")) return;
      try {
        const updated = await adminFetch(`/api/admin/photos/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ pending: true, hidden: true }),
        });
        applyUpdate(item, updated);
      } catch (err) {
        showAlert(els.adminAlert, `Не удалось отклонить: ${err.message}`, "error");
      }
    });
    actions.append(approveBtn, rejectBtn);
  }

  const hideBtn = document.createElement("button");
  hideBtn.type = "button";
  hideBtn.className = "btn btn-ghost btn-small";
  hideBtn.textContent = item.hidden ? "Показать" : "Скрыть";
  hideBtn.addEventListener("click", async () => {
    try {
      const updated = await adminFetch(`/api/admin/photos/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ hidden: !item.hidden }),
      });
      applyUpdate(item, updated);
    } catch (err) {
      showAlert(els.adminAlert, `Не удалось обновить: ${err.message}`, "error");
    }
  });

  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "btn btn-danger btn-small";
  delBtn.textContent = "Удалить";
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Удалить файл безвозвратно?`)) return;
    try {
      await adminFetch(`/api/admin/photos/${item.id}`, { method: "DELETE" });
      state.items = state.items.filter((it) => it.id !== item.id);
      state.total = Math.max(0, state.total - 1);
      // Recompute offset from what's actually shown so "Показать ещё"
      // doesn't skip the row that shifted into the deleted item's place.
      state.offset = state.items.length;
      renderList(false);
      showAlert(els.adminAlert, "", "error");
    } catch (err) {
      showAlert(els.adminAlert, `Не удалось удалить: ${err.message}`, "error");
    }
  });

  actions.append(hideBtn, delBtn);
  li.appendChild(actions);
}

function renderRow(item) {
  const li = document.createElement("li");
  li.className = `admin-item${item.hidden ? " is-hidden" : ""}`;

  const thumb = document.createElement("div");
  thumb.className = "admin-thumb";
  if (item.kind === "image") {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = item.thumb_url || item.url;
    img.alt = "";
    thumb.appendChild(img);
  } else {
    thumb.classList.add("admin-thumb-video");
    thumb.textContent = "▶";
  }

  const meta = document.createElement("div");
  meta.className = "admin-meta";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = item.kind === "video" ? "Видео" : "Фото";
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent =
    `${formatWhen(item.uploaded_at)} · ${formatBytes(item.size_bytes)}` +
    (item.guest_name ? ` · ${item.guest_name}` : "") +
    (item.client_ip ? ` · IP ${item.client_ip}` : "") +
    (item.pending ? " · на модерации" : "") +
    (item.hidden ? " · скрыто" : "");
  meta.append(name, sub);

  li.append(thumb, meta);
  rowActions(item, li);
  return li;
}

function renderList(keepList = true) {
  if (!keepList) {
    els.adminList.innerHTML = "";
    state.items.forEach((item) => els.adminList.appendChild(renderRow(item)));
  }
  renderTotal();
  renderMore();
}

// In-flight list request; a new filter aborts it so the latest click wins.
let inflight = null;

async function load(reset = false) {
  if (reset) {
    inflight?.abort();
  } else if (state.loading) {
    return; // "Показать ещё" while a page is already coming
  }
  const controller = new AbortController();
  inflight = controller;
  if (reset) {
    state.offset = 0;
    state.items = [];
    setStatus("Загружаю…", false);
  }
  state.loading = true;
  renderMore();
  try {
    const hidden = els.showHidden.checked ? "true" : "false";
    const pending = els.pendingOnly.checked ? "true" : "";
    const guest = state.guest ? `&guest=${encodeURIComponent(state.guest)}` : "";
    const data = await adminFetch(
      `/api/admin/photos?limit=${PAGE}&offset=${state.offset}&kind=${state.kind}` +
        `&hidden=${hidden}${pending ? `&pending=${pending}` : ""}${guest}`,
      { signal: controller.signal },
    );
    if (controller !== inflight) return; // superseded by a newer filter
    state.total = data.total;
    state.items = reset ? data.items : state.items.concat(data.items);
    state.offset = state.items.length;
    setStatus("", true);
    renderList(false);
    if (!state.items.length) {
      setStatus("Ничего не найдено.", false);
    }
  } catch (err) {
    if (controller !== inflight || err.name === "AbortError") return;
    setStatus("", true);
    showAlert(els.adminAlert, `Не получилось загрузить список: ${err.message}`, "error");
    if (err.status === 401) logout();
  } finally {
    if (controller === inflight) {
      state.loading = false;
      inflight = null;
      renderMore();
    }
  }
}

async function login() {
  state.token = els.password.value.trim();
  showAlert(els.loginAlert, "", "error");
  els.loginBtn.disabled = true;
  try {
    await adminFetch("/api/admin/photos?limit=1");
    sessionStorage.setItem(TOKEN_KEY, state.token);
    showPanel();
  } catch (err) {
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    showAlert(
      els.loginAlert,
      err.status === 401
        ? "Неверный пароль."
        : `Не удалось войти: ${err.message}`,
      "error",
    );
  } finally {
    els.loginBtn.disabled = false;
  }
}

function logout() {
  state.token = "";
  sessionStorage.removeItem(TOKEN_KEY);
  showPanel();
}

function showPanel() {
  const authed = Boolean(state.token);
  els.loginCard.hidden = authed;
  els.panel.hidden = !authed;
  if (authed) {
    loadAdminGuests();
    load(true);
  }
}

function bind() {
  els.loginBtn.addEventListener("click", login);
  els.password.addEventListener("keydown", (e) => {
    if (e.key === "Enter") login();
  });
  els.logoutBtn.addEventListener("click", logout);
  els.adminMore.addEventListener("click", () => load(false));
  els.showHidden.addEventListener("change", () => load(true));
  els.pendingOnly.addEventListener("change", () => load(true));
  if (els.guestFilter) {
    els.guestFilter.addEventListener("change", () => {
      state.guest = els.guestFilter.value;
      load(true);
    });
  }
  if (els.archiveBtn) {
    els.archiveBtn.addEventListener("click", downloadArchive);
  }
  document.querySelectorAll("#panel .filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      // A newer filter aborts the in-flight request inside load(true).
      document.querySelectorAll("#panel .filter-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      state.kind = btn.dataset.kind;
      load(true);
    });
  });
}

async function loadAdminGuests() {
  if (!els.guestFilter) return;
  try {
    const data = await adminFetch("/api/admin/photos/guests");
    els.guestFilter.innerHTML = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "Все гости";
    els.guestFilter.appendChild(all);
    (data.guests || []).forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      els.guestFilter.appendChild(opt);
    });
  } catch {
    // non-fatal
  }
}

async function downloadArchive() {
  try {
    const res = await fetch(`${BASE_PATH}/api/admin/photos/archive.zip`, {
      headers: { Authorization: `Bearer ${token()}` },
    });
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `wedding-photos-${new Date().toISOString().slice(0, 10)}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    showAlert(els.adminAlert, `Не удалось скачать архив: ${err.message}`, "error");
  }
}

applySiteCopy();
hideHomeForKnownGuest();
bind();
showPanel();