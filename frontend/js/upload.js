import {
  ALLOWED_TYPES,
  MAX_FILES_DEFAULT,
  MAX_SIZE_MB_DEFAULT,
  MAX_VIDEO_SIZE_MB_DEFAULT,
  BASE_PATH,
  HOME_URL,
  api,
  applySiteCopy,
  clearGuestName,
  formatBytes,
  guestName,
  isVideoType,
  showAlert,
} from "./common.js";

const state = {
  files: [],
  maxSizeMb: MAX_SIZE_MB_DEFAULT,
  maxVideoSizeMb: MAX_VIDEO_SIZE_MB_DEFAULT,
  maxFiles: MAX_FILES_DEFAULT,
  backend: "local",
  busy: false,
};

// Batch several files per request so guests on one network don't blow the
// per-minute rate limit, and run a few batches concurrently for throughput.
const BATCH_SIZE = 6;
const PARALLEL_BATCHES = 2;

const els = {
  zone: document.getElementById("dropzone"),
  input: document.getElementById("file-input"),
  camera: document.getElementById("camera-input"),
  pickGallery: document.getElementById("pick-gallery"),
  pickCamera: document.getElementById("pick-camera"),
  queue: document.getElementById("queue"),
  uploadBtn: document.getElementById("upload-btn"),
  clearBtn: document.getElementById("clear-btn"),
  alert: document.getElementById("alert"),
  hints: document.getElementById("hints"),
  overall: document.getElementById("overall"),
  overallBar: document.getElementById("overall-bar"),
  overallText: document.getElementById("overall-text"),
  guestBanner: document.getElementById("guest-banner"),
  guestName: document.getElementById("guest-name"),
  changeName: document.getElementById("change-name"),
};

function syncHints() {
  const backendHint =
    state.backend === "yandex" ? "облако Яндекса" : "этот сервер";
  const countHint =
    state.maxFiles > 0 ? `до ${state.maxFiles} файлов · ` : "";
  els.hints.textContent =
    `Фото до ${state.maxSizeMb} МБ, видео до ${state.maxVideoSizeMb} МБ · ` +
    `JPEG, PNG, WebP, HEIC, MP4, WebM, MOV · ${countHint}${backendHint}`;
}

function updateOverall() {
  const active = state.files.filter((f) => !f.error || f.retrying);
  if (!active.length) {
    els.overall.hidden = true;
    return;
  }
  const doneOrProgress = state.files.filter((f) => !f.error || f.progress > 0);
  if (!doneOrProgress.length && !state.busy) {
    els.overall.hidden = true;
    return;
  }
  const total = state.files.length;
  const sum = state.files.reduce((acc, f) => {
    if (f.done) return acc + 100;
    if (f.error) return acc;
    return acc + (f.progress || 0);
  }, 0);
  const pct = Math.round(sum / total);
  els.overall.hidden = false;
  els.overallBar.style.width = `${pct}%`;
  els.overallText.textContent = `${pct}% · ${state.files.filter((f) => f.done).length}/${total}`;
}

function validateFile(file) {
  const type = (file.type || "").toLowerCase();
  // Some phones leave HEIC/HEIF type empty — allow by extension.
  const name = (file.name || "").toLowerCase();
  const extOk = /\.(jpe?g|png|webp|heic|heif|mp4|webm|mov|m4v)$/i.test(name);
  if (type && !ALLOWED_TYPES.has(type) && !extOk) {
    return `«${file.name}»: тип не поддерживается (${type || "неизвестно"}).`;
  }
  if (!type && !extOk) {
    return `«${file.name}»: не похоже на фото или видео. Нужны JPEG, PNG, WebP, HEIC, MP4, WebM или MOV.`;
  }
  const limitMb = isVideoType(guessContentType(file))
    ? state.maxVideoSizeMb
    : state.maxSizeMb;
  if (file.size > limitMb * 1024 * 1024) {
    return `«${file.name}»: больше ${limitMb} МБ. Выберите файл поменьше.`;
  }
  if (file.size < 1) {
    return `«${file.name}»: пустой файл.`;
  }
  return null;
}

function guessContentType(file) {
  const type = (file.type || "").toLowerCase();
  if (ALLOWED_TYPES.has(type)) return type;
  const name = (file.name || "").toLowerCase();
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".webp")) return "image/webp";
  if (name.endsWith(".heic")) return "image/heic";
  if (name.endsWith(".heif")) return "image/heif";
  if (name.endsWith(".mp4")) return "video/mp4";
  if (name.endsWith(".webm")) return "video/webm";
  if (name.endsWith(".mov")) return "video/quicktime";
  if (name.endsWith(".m4v")) return "video/x-m4v";
  // Unknown: never pretend it is a JPEG — validateFile rejects it.
  return "";
}

function renderQueue() {
  els.queue.innerHTML = "";
  for (const item of state.files) {
    const li = document.createElement("li");
    li.className = "queue-item";
    li.dataset.id = item.id;

    let thumb;
    if (item.preview) {
      thumb = document.createElement("img");
      thumb.className = "thumb";
      thumb.alt = "";
      thumb.src = item.preview;
    } else {
      thumb = document.createElement("div");
      thumb.className = "thumb thumb-placeholder";
      thumb.textContent = item.kind === "video" ? "▶" : "✦";
      thumb.setAttribute("aria-hidden", "true");
    }

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML =
      `<div class="name"></div>` +
      `<div class="sub"></div>` +
      `<div class="progress"><i></i></div>` +
      `<div class="status-row"></div>`;
    meta.querySelector(".name").textContent = item.file.name;
    meta.querySelector(".sub").textContent =
      `${formatBytes(item.file.size)} · ${guessContentType(item.file) || "неизвестный тип"}`;
    meta.querySelector("i").style.width = `${item.progress || 0}%`;

    const row = meta.querySelector(".status-row");
    const status = document.createElement("div");
    status.className = "status";
    if (item.error) {
      status.classList.add("err");
      status.textContent = item.error;
      row.append(status);
      // Retrying the same file can't fix a wrong format or size.
      if (!item.invalid) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "retry-btn";
        retry.textContent = "Повторить";
        retry.disabled = state.busy;
        retry.addEventListener("click", () => retryOne(item.id));
        row.append(retry);
      }
    } else if (item.done) {
      status.classList.add("ok");
      status.textContent = "готово";
      row.append(status);
    } else if (item.progress > 0) {
      status.textContent = `${item.progress}%`;
      row.append(status);
    } else {
      status.textContent = "в очереди";
      row.append(status);
    }

    li.append(thumb, meta);
    els.queue.appendChild(li);
  }
  els.uploadBtn.disabled = state.busy || !state.files.some((f) => !f.done && !f.error);
  els.clearBtn.disabled = state.busy || state.files.length === 0;
  updateOverall();
}

function addFiles(fileList) {
  showAlert(els.alert, "", "error");
  const incoming = Array.from(fileList || []);
  if (!incoming.length) return;

  const unlimited = !state.maxFiles || state.maxFiles <= 0;
  const room = unlimited ? Infinity : state.maxFiles - state.files.length;
  if (!unlimited && room <= 0) {
    showAlert(
      els.alert,
      `Уже выбрано ${state.maxFiles} файлов — это лимит за один заход.`,
      "error",
    );
    return;
  }

  const slice = unlimited ? incoming : incoming.slice(0, room);
  if (!unlimited && incoming.length > room) {
    showAlert(
      els.alert,
      `Добавлены только первые ${room} файл(ов) — лимит ${state.maxFiles}.`,
      "error",
    );
  }

  for (const file of slice) {
    const err = validateFile(file);
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const type = guessContentType(file);
    const item = {
      id,
      file,
      kind: isVideoType(type) ? "video" : "image",
      preview: "",
      progress: 0,
      done: false,
      error: err,
      invalid: Boolean(err),
    };
    if (!err && item.kind === "image" && type !== "image/heic" && type !== "image/heif") {
      try {
        item.preview = URL.createObjectURL(file);
      } catch {
        item.preview = "";
      }
    }
    state.files.push(item);
  }
  renderQueue();
}

function clearQueue() {
  for (const item of state.files) {
    if (item.preview) URL.revokeObjectURL(item.preview);
  }
  state.files = [];
  renderQueue();
  showAlert(els.alert, "", "error");
}

function uploadViaXhrLocal(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("files", file, file.name);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_PATH}/api/uploads`);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        onProgress(Math.round((ev.loaded / ev.total) * 100));
      }
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        data = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data);
        return;
      }
      const detail = data?.detail;
      const message = Array.isArray(detail)
        ? detail.map((x) => x.msg || JSON.stringify(x)).join("; ")
        : detail || `Сервер ответил ${xhr.status}`;
      const err = new Error(message);
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () =>
      reject(new Error("Сеть оборвалась. Проверьте связь и нажмите «Повторить»."));
    xhr.send(form);
  });
}

function uploadBatchLocal(batch) {
  /* One multipart POST for the whole batch (far fewer requests = fewer 429s). */
  return new Promise((resolve, reject) => {
    const form = new FormData();
    batch.forEach((item) => form.append("files", item.file, item.file.name));

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_PATH}/api/uploads`);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        const pct = Math.round((ev.loaded / ev.total) * 100);
        batch.forEach((item) => {
          item.progress = pct;
        });
        renderQueue();
      }
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        data = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data);
        return;
      }
      const detail = data?.detail;
      const message = Array.isArray(detail)
        ? detail.map((x) => x.msg || JSON.stringify(x)).join("; ")
        : detail || `Сервер ответил ${xhr.status}`;
      const err = new Error(message);
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () =>
      reject(new Error("Сеть оборвалась. Проверьте связь и попробуйте снова."));
    xhr.send(form);
  });
}

function postToObjectStorage(uploadUrl, fields, file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    Object.entries(fields).forEach(([k, v]) => form.append(k, v));
    form.append("file", file, file.name);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", uploadUrl);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        onProgress(Math.round((ev.loaded / ev.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
        return;
      }
      reject(
        new Error(
          `Облако ответило ${xhr.status}. Попробуйте ещё раз или выберите меньший файл.`,
        ),
      );
    };
    xhr.onerror = () =>
      reject(
        new Error(
          "Не удалось отправить в облако (сеть или CORS бакета). Повторите позже.",
        ),
      );
    xhr.send(form);
  });
}

async function uploadOneLocal(item) {
  await uploadViaXhrLocal(item.file, (pct) => {
    item.progress = pct;
    renderQueue();
  });
}

async function uploadOneYandex(item) {
  const contentType = guessContentType(item.file);
  const presign = await api("/api/uploads/presign", {
    method: "POST",
    body: JSON.stringify({
      files: [{ content_type: contentType, size: item.file.size }],
    }),
  });
  const slot = presign.items[0];
  item.progress = 5;
  renderQueue();
  await postToObjectStorage(slot.upload_url, slot.fields, item.file, (pct) => {
    item.progress = Math.max(5, Math.min(95, pct));
    renderQueue();
  });
  await api("/api/uploads/confirm", {
    method: "POST",
    body: JSON.stringify({
      files: [
        {
          key: slot.key,
          content_type: contentType,
          size_bytes: item.file.size,
        },
      ],
    }),
  });
}

async function uploadBatchYandex(batch) {
  /* One presign + one confirm per batch; object uploads go straight to the bucket. */
  const contentTypes = batch.map((item) => guessContentType(item.file));
  const presign = await api("/api/uploads/presign", {
    method: "POST",
    body: JSON.stringify({
      files: batch.map((item, i) => ({
        content_type: contentTypes[i],
        size: item.file.size,
      })),
    }),
  });
  const slots = presign.items;

  await Promise.all(
    slots.map((slot, i) =>
      postToObjectStorage(slot.upload_url, slot.fields, batch[i].file, (pct) => {
        batch[i].progress = Math.max(5, Math.min(95, pct));
        renderQueue();
      }),
    ),
  );

  await api("/api/uploads/confirm", {
    method: "POST",
    body: JSON.stringify({
      files: slots.map((slot, i) => ({
        key: slot.key,
        content_type: contentTypes[i],
        size_bytes: batch[i].file.size,
      })),
    }),
  });
}

async function uploadOne(item) {
  item.error = null;
  item.progress = 0;
  // Every attempt (first upload and «Повторить») goes through validation;
  // limits may also have changed since the file was added (health loaded).
  const invalid = validateFile(item.file);
  if (invalid) {
    item.invalid = true;
    throw new Error(invalid);
  }
  try {
    if (state.backend === "yandex") {
      await uploadOneYandex(item);
    } else {
      await uploadOneLocal(item);
    }
  } catch (err) {
    // 400 = the server rejected the file itself; resending it won't help.
    // Network errors, 429 and 5xx stay retryable.
    if (err.status === 400) item.invalid = true;
    throw err;
  }
  item.progress = 100;
  item.done = true;
}

function runBatches(batches) {
  /* Process batches with up to PARALLEL_BATCHES in flight. Returns okCount. */
  let okCount = 0;
  const worker = async (idx) => {
    while (idx < batches.length) {
      const batch = batches[idx];
      batch.forEach((item) => {
        item.error = null;
        item.progress = 0;
      });
      renderQueue();
      try {
        if (state.backend === "yandex") {
          await uploadBatchYandex(batch);
        } else {
          await uploadBatchLocal(batch);
        }
        batch.forEach((item) => {
          item.progress = 100;
          item.done = true;
        });
        okCount += batch.length;
      } catch (err) {
        const message = err.message || "Ошибка загрузки";
        batch.forEach((item) => {
          item.error = message;
          item.progress = 0;
          if (err.status === 400) item.invalid = true;
        });
      }
      renderQueue();
      idx += PARALLEL_BATCHES;
    }
  };
  return Promise.all(
    Array.from({ length: Math.min(PARALLEL_BATCHES, batches.length) }, (_, i) => worker(i)),
  ).then(() => okCount);
}

async function startUpload() {
  if (state.busy) return;
  const pending = state.files.filter((f) => !f.done && !f.error);
  if (!pending.length) {
    showAlert(els.alert, "Нет файлов для загрузки.", "error");
    return;
  }

  state.busy = true;
  renderQueue();
  showAlert(els.alert, "", "error");

  const batches = [];
  for (let i = 0; i < pending.length; i += BATCH_SIZE) {
    batches.push(pending.slice(i, i + BATCH_SIZE));
  }

  try {
    const okCount = await runBatches(batches);
    if (okCount) {
      showAlert(
        els.alert,
        `Спасибо! Загружено: ${okCount}. Можно открыть галерею.`,
        "ok",
      );
    } else {
      showAlert(
        els.alert,
        "Ни один файл не загрузился. Нажмите «Повторить» у ошибки.",
        "error",
      );
    }
  } catch (err) {
    showAlert(els.alert, err.message || "Не удалось начать загрузку", "error");
  } finally {
    state.busy = false;
    renderQueue();
  }
}

async function retryOne(id) {
  const item = state.files.find((f) => f.id === id);
  if (!item || state.busy) return;
  state.busy = true;
  item.error = null;
  item.done = false;
  item.progress = 0;
  renderQueue();
  try {
    await uploadOne(item);
    showAlert(els.alert, "Файл загружен.", "ok");
  } catch (err) {
    item.error = err.message || "Ошибка загрузки";
    item.progress = 0;
    showAlert(els.alert, item.error, "error");
  } finally {
    state.busy = false;
    renderQueue();
  }
}

function bind() {
  const openPicker = (input) => input.click();

  els.zone.addEventListener("click", () => openPicker(els.input));
  els.zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openPicker(els.input);
    }
  });
  els.pickGallery.addEventListener("click", () => openPicker(els.input));
  els.pickCamera.addEventListener("click", () => openPicker(els.camera));

  els.input.addEventListener("change", () => {
    addFiles(els.input.files);
    els.input.value = "";
  });
  els.camera.addEventListener("change", () => {
    addFiles(els.camera.files);
    els.camera.value = "";
  });

  ["dragenter", "dragover"].forEach((evt) => {
    els.zone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.zone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    els.zone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.zone.classList.remove("is-dragover");
    });
  });
  els.zone.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));

  els.uploadBtn.addEventListener("click", startUpload);
  els.clearBtn.addEventListener("click", clearQueue);
}

async function boot() {
  applySiteCopy();
  // Guest must have entered their name on the home page; otherwise send them back.
  const name = guestName();
  if (!name) {
    window.location.replace(HOME_URL);
    return;
  }
  if (els.guestBanner) {
    els.guestName.textContent = name;
    els.guestBanner.hidden = false;
    els.changeName.addEventListener("click", (e) => {
      e.preventDefault();
      clearGuestName();
      window.location.assign(HOME_URL);
    });
  }
  bind();
  syncHints();
  renderQueue();
  try {
    const health = await api("/api/health");
    if (health.max_file_size_mb) state.maxSizeMb = health.max_file_size_mb;
    if (health.max_video_size_mb) state.maxVideoSizeMb = health.max_video_size_mb;
    if (health.max_files_per_request != null) state.maxFiles = health.max_files_per_request;
    if (health.storage_backend) state.backend = health.storage_backend;
    syncHints();
    if (!health.storage_configured) {
      showAlert(
        els.alert,
        state.backend === "yandex"
          ? "Облако не настроено. Нужны ключи Yandex или STORAGE_BACKEND=local."
          : "Локальное хранилище недоступно для записи. Проверьте STORAGE_DIR.",
        "error",
      );
    }
  } catch (err) {
    showAlert(els.alert, `API недоступен: ${err.message}`, "error");
  }
}

boot();
