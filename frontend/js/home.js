import {
  UPLOAD_URL,
  applySiteCopy,
  clearGuestName,
  guestName,
  setGuestName,
  showAlert,
} from "./common.js";

const els = {
  input: document.getElementById("guest-name"),
  continueBtn: document.getElementById("welcome-continue"),
  alert: document.getElementById("welcome-alert"),
};

function go() {
  window.location.assign(UPLOAD_URL);
}

async function boot() {
  applySiteCopy();

  // Returning guest: name already remembered — skip straight to upload.
  if (guestName()) {
    go();
    return;
  }

  els.continueBtn.addEventListener("click", submit);
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });

  function submit() {
    const name = els.input.value.trim();
    if (!name) {
      showAlert(els.alert, "Пожалуйста, напишите ваше имя.", "error");
      els.input.focus();
      return;
    }
    setGuestName(name);
    go();
  }
}

boot();