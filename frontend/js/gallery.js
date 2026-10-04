import { api, applySiteCopy, formatBytes, formatWhen, showAlert } from "./common.js";

const els = {
  grid: document.getElementById("gallery"),
  status: document.getElementById("status"),
  alert: document.getElementById("alert"),
  total: document.getElementById("total"),
};

async function load() {
  applySiteCopy();
  els.status.className = "loading";
  els.status.textContent = "Загружаю…";
  els.grid.innerHTML = "";
  showAlert(els.alert, "", "error");

  try {
    const data = await api("/api/photos?limit=48");
    els.total.textContent = `Всего · ${data.total}`;

    if (!data.items.length) {
      els.status.className = "empty";
      els.status.textContent =
        "Пока пусто — загрузите фото на странице «Загрузить».";
      return;
    }

    els.status.textContent = "";
    for (const item of data.items) {
      const a = document.createElement("a");
      a.href = item.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.title = `${formatWhen(item.uploaded_at)} · ${formatBytes(item.size_bytes)}`;

      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "Фото со свадьбы";
      img.src = item.url;

      a.appendChild(img);
      els.grid.appendChild(a);
    }
  } catch (err) {
    els.status.className = "error-box";
    els.status.textContent = "";
    showAlert(
      els.alert,
      `Не получилось загрузить галерею: ${err.message}`,
      "error",
    );
  }
}

load();
