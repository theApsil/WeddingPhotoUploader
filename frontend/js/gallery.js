import { api, applySiteCopy, formatBytes, formatWhen, showAlert } from "./common.js";

const els = {
  grid: document.getElementById("gallery"),
  status: document.getElementById("status"),
  alert: document.getElementById("alert"),
  total: document.getElementById("total"),
  loadMore: document.getElementById("load-more"),
  lightbox: document.getElementById("lightbox"),
  lbStage: document.getElementById("lb-stage"),
  lbMeta: document.getElementById("lb-meta"),
};

const PAGE = 48;
const state = {
  items: [],
  offset: 0,
  total: 0,
  kind: "all",
  loading: false,
  current: -1,
  hasMore: false,
};

function setStatus(message, cls = "loading") {
  els.status.className = cls;
  els.status.textContent = message || "";
}

function tileStyle(item) {
  if (item.kind === "video") return { aspectRatio: "4 / 3" };
  if (item.thumb_width && item.thumb_height) {
    return { aspectRatio: `${item.thumb_width} / ${item.thumb_height}` };
  }
  return { aspectRatio: "1 / 1" };
}

function makeTile(item, index) {
  const tile = document.createElement("div");
  tile.className = `tile tile-${item.kind}`;
  tile.style.aspectRatio = tileStyle(item).aspectRatio;
  tile.dataset.index = index;
  tile.title = `${formatWhen(item.uploaded_at)} · ${formatBytes(item.size_bytes)}`;

  if (item.kind === "video") {
    const play = document.createElement("span");
    play.className = "tile-play";
    play.textContent = "▶";
    tile.appendChild(play);
  } else {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "Фото со свадьбы";
    // Fall back to display/original if a preview is missing (e.g. old data).
    const fallbacks = [item.thumb_url, item.display_url, item.url].filter(Boolean);
    img.src = fallbacks[0] || "";
    let fi = 1;
    img.onerror = () => {
      if (fi < fallbacks.length) {
        img.src = fallbacks[fi++];
      } else {
        tile.classList.add("tile-broken");
        img.remove();
      }
    };
    tile.appendChild(img);
  }

  const badge = document.createElement("span");
  badge.className = "tile-badge";
  badge.textContent = item.kind === "video" ? "Видео" : formatWhen(item.uploaded_at);
  tile.appendChild(badge);

  tile.addEventListener("click", () => openLightbox(index));
  return tile;
}

function renderTotal() {
  const label = state.kind === "all" ? "Всего" : state.kind === "video" ? "Видео" : "Фото";
  els.total.textContent = `${label} · ${state.total}`;
}

function renderLoadMore() {
  const remaining = state.total - state.items.length;
  if (state.items.length > 0 && remaining > 0) {
    els.loadMore.hidden = false;
    els.loadMore.textContent = `Показать ещё · осталось ${remaining}`;
    els.loadMore.disabled = state.loading;
  } else {
    els.loadMore.hidden = true;
  }
}

function render() {
  els.grid.innerHTML = "";
  state.items.forEach((item, index) => {
    els.grid.appendChild(makeTile(item, index));
  });
  renderTotal();
  renderLoadMore();
}

async function load(reset = false) {
  if (state.loading) return;
  if (reset) {
    state.offset = 0;
    state.items = [];
    setStatus("Загружаю…");
    showAlert(els.alert, "", "error");
  }
  state.loading = true;
  renderLoadMore();

  try {
    const data = await api(
      `/api/photos?limit=${PAGE}&offset=${state.offset}&kind=${state.kind}`,
    );
    state.total = data.total;
    state.hasMore = data.has_more;
    state.items = reset ? data.items : state.items.concat(data.items);
    state.offset = state.items.length;

    if (!state.items.length) {
      setStatus("Пока пусто — загрузите фото или видео на странице «Загрузить».", "empty");
      renderTotal();
      renderLoadMore();
      return;
    }

    setStatus("");
    render();
  } catch (err) {
    setStatus("", "error-box");
    showAlert(
      els.alert,
      `Не получилось загрузить галерею: ${err.message}`,
      "error",
    );
    renderLoadMore();
  } finally {
    state.loading = false;
  }
}

/* ---- Lightbox ---- */

function pauseStage() {
  const video = els.lbStage.querySelector("video");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
}

function renderStage() {
  const item = state.items[state.current];
  if (!item) return;
  els.lbStage.innerHTML = "";
  if (item.kind === "video") {
    const video = document.createElement("video");
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.src = item.url;
    els.lbStage.appendChild(video);
  } else {
    // Show the EXIF-free display JPEG (works for HEIC everywhere, lighter);
    // fall back to the original when no display version exists.
    const img = document.createElement("img");
    img.src = item.display_url || item.url;
    img.alt = "Фото со свадьбы";
    els.lbStage.appendChild(img);
  }
  const download = document.createElement("a");
  download.className = "lb-download";
  download.href = item.url;
  download.download = "";
  download.textContent = "Скачать";
  download.target = "_blank";
  download.rel = "noopener noreferrer";
  els.lbStage.appendChild(download);
  els.lbMeta.textContent =
    `${state.current + 1} / ${state.items.length} · ` +
    `${formatWhen(item.uploaded_at)} · ${formatBytes(item.size_bytes)}`;
}

function openLightbox(index) {
  if (!state.items.length) return;
  state.current = index;
  els.lightbox.hidden = false;
  els.lightbox.setAttribute("aria-hidden", "false");
  document.body.classList.add("no-scroll");
  renderStage();
}

function closeLightbox() {
  pauseStage();
  els.lightbox.hidden = true;
  els.lightbox.setAttribute("aria-hidden", "true");
  document.body.classList.remove("no-scroll");
  state.current = -1;
}

async function step(delta) {
  if (state.current < 0 || !state.items.length) return;
  let next = state.current + delta;
  if (delta > 0 && next >= state.items.length) {
    // Load the next page so the lightbox can keep navigating forward.
    if (state.hasMore && !state.loading) {
      await load(false);
    }
    next = Math.min(next, state.items.length - 1);
  } else if (delta < 0 && next < 0) {
    next = state.items.length - 1;
  }
  state.current = next;
  renderStage();
}

function bindFilters() {
  document.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      // Ignore clicks while a request is in flight so the filter isn't lost.
      if (state.loading) return;
      document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      state.kind = btn.dataset.kind;
      load(true);
    });
  });
}

function bindLightbox() {
  els.loadMore.addEventListener("click", () => load(false));
  els.lightbox.querySelector(".lb-close").addEventListener("click", closeLightbox);
  els.lightbox.querySelector(".lb-prev").addEventListener("click", () => step(-1));
  els.lightbox.querySelector(".lb-next").addEventListener("click", () => step(1));
  els.lightbox.addEventListener("click", (e) => {
    if (e.target === els.lightbox) closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (els.lightbox.hidden) return;
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
  });
}

applySiteCopy();
bindFilters();
bindLightbox();
load(true);