"use strict";

// ---- shared helpers ----

function toast(message, ok = true) {
  const container = document.getElementById("toasts");
  if (!container) return;
  const el = document.createElement("div");
  el.className = ok ? "toast" : "toast error";
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

async function api(method, url, body) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

// expose for other scripts
window.webui = { toast, api };

// ---- machine select ----

const machineSelect = document.getElementById("machine-select");
if (machineSelect) {
  machineSelect.addEventListener("change", async () => {
    try {
      await api("PUT", "/api/machine", { name: machineSelect.value });
      window.location.reload();
    } catch (err) {
      toast(`マシン切替失敗: ${err.message}`, false);
    }
  });
}

// ---- topbar safety controls ----

async function postTopbarCommand(button, url, successMessage, failurePrefix) {
  button.disabled = true;
  try {
    await api("POST", url);
    toast(successMessage);
  } catch (err) {
    toast(`${failurePrefix}: ${err.message}`, false);
  } finally {
    button.disabled = false;
  }
}

function bindTopbarCommand(buttonId, url, successMessage, failurePrefix) {
  const button = document.getElementById(buttonId);
  if (!button) return;
  button.addEventListener("click", () => {
    postTopbarCommand(button, url, successMessage, failurePrefix);
  });
}

bindTopbarCommand(
  "estop",
  "/api/emergency-stop",
  "緊急停止を送信しました",
  "緊急停止失敗"
);
bindTopbarCommand(
  "firmware-restart",
  "/api/firmware-restart",
  "ファームウェア再起動を送信しました",
  "ファームウェア再起動失敗"
);

// ---- PCB file browser ----

const pcbChip = document.getElementById("pcb-chip");
const browser = document.getElementById("file-browser");
const fbPath = document.getElementById("fb-path");
const fbEntries = document.getElementById("fb-entries");

async function showDirectory(path) {
  const data = await api("GET", `/api/files?path=${encodeURIComponent(path)}`);
  fbPath.textContent = `/${data.path}`;
  fbEntries.replaceChildren();

  if (data.path !== "") {
    const up = document.createElement("li");
    up.className = "dir";
    up.textContent = "..";
    up.addEventListener("click", () => {
      const parent = data.path.split("/").slice(0, -1).join("/");
      showDirectory(parent).catch((err) => toast(err.message, false));
    });
    fbEntries.appendChild(up);
  }

  for (const entry of data.entries) {
    const li = document.createElement("li");
    li.className = entry.type;
    li.textContent = entry.name;
    const childPath = data.path === "" ? entry.name : `${data.path}/${entry.name}`;
    if (entry.type === "dir") {
      li.addEventListener("click", () => {
        showDirectory(childPath).catch((err) => toast(err.message, false));
      });
    } else {
      li.addEventListener("click", async () => {
        try {
          await api("PUT", "/api/pcb-file", { path: childPath });
          browser.close();
          window.location.reload();
        } catch (err) {
          toast(`PCB選択失敗: ${err.message}`, false);
        }
      });
    }
    fbEntries.appendChild(li);
  }
}

if (pcbChip && browser) {
  pcbChip.addEventListener("click", () => {
    browser.showModal();
    showDirectory(pcbChip.dataset.fbStart || "").catch((err) =>
      toast(err.message, false)
    );
  });
  document.getElementById("fb-close").addEventListener("click", () => browser.close());

  const uploadInput = document.getElementById("fb-upload-input");
  document
    .getElementById("fb-upload")
    .addEventListener("click", () => uploadInput.click());
  uploadInput.addEventListener("change", async () => {
    const file = uploadInput.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch("/api/pcb-file/upload", { method: "POST", body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
      browser.close();
      window.location.reload();
    } catch (err) {
      toast(`アップロード失敗: ${err.message}`, false);
    } finally {
      uploadInput.value = "";
    }
  });
}
