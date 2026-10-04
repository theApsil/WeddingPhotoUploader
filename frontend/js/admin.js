import {
  BASE_PATH,
  applySiteCopy,
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
  logoutBtn: document.getElementById("logout-btn"),
};

const state = {
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  items: [],
  offset: 0,
  total: 0,
  kind: "all",
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

function rowActions(item, li) {
  const actions = document.createElement("div");
  actions.className = "admin-actions";

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
      item.hidden = updated.hidden;
      renderList(false);
      showAlert(els.adminAlert, "", "error");
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
    (item.client_ip ? ` · IP ${item.client_ip}` : "") +
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

async function load(reset = false) {
  if (state.loading) return;
  if (reset) {
    state.offset = 0;
    state.items = [];
    setStatus("Загружаю…", false);
  }
  state.loading = true;
  renderMore();
  try {
    const hidden = els.showHidden.checked ? "true" : "false";
    const data = await adminFetch(
      `/api/admin/photos?limit=${PAGE}&offset=${state.offset}&kind=${state.kind}&hidden=${hidden}`,
    );
    state.total = data.total;
    state.items = reset ? data.items : state.items.concat(data.items);
    state.offset = state.items.length;
    setStatus("", true);
    renderList(false);
    if (!state.items.length) {
      setStatus("Ничего не найдено.", false);
    }
  } catch (err) {
    setStatus("", true);
    showAlert(els.adminAlert, `Не получилось загрузить список: ${err.message}`, "error");
    if (err.status === 401) logout();
  } finally {
    state.loading = false;
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
  document.querySelectorAll("#panel .filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#panel .filter-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      state.kind = btn.dataset.kind;
      load(true);
    });
  });
}

applySiteCopy();
bind();
showPanel();