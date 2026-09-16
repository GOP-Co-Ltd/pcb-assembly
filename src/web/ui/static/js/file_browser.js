"use strict";

// PCB の一覧表示・選択・アップロード。通信と machine prefix は共通 API に任せる。
(() => {
  const pcbChip = document.getElementById("pcb-chip");
  const browser = document.getElementById("file-browser");
  if (!pcbChip || !browser) return;

  const { api } = window.webui;
  const fbPath = document.getElementById("fb-path");
  const fbEntries = document.getElementById("fb-entries");
  const fbError = document.getElementById("fb-error");
  const fbEmpty = document.getElementById("fb-empty");

  const showError = (message) => {
    fbError.textContent = message;
    fbError.hidden = false;
  };

  const appendEntry = (name, type, onClick) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = type;
    const label = document.createElement("span");
    label.className = "fb-entry-name";
    label.textContent = name;
    button.appendChild(label);
    button.addEventListener("click", onClick);
    li.appendChild(button);
    fbEntries.appendChild(li);
  };

  const showDirectory = async (path) => {
    const data = await api("GET", `/api/files?path=${encodeURIComponent(path)}`);
    fbPath.textContent = `/${data.path}`;
    fbError.hidden = true;
    fbEmpty.hidden = data.entries.length !== 0;
    fbEntries.replaceChildren();

    if (data.path !== "") {
      appendEntry("上のフォルダー", "parent", () => {
        const parent = data.path.split("/").slice(0, -1).join("/");
        showDirectory(parent).catch((err) => showError(err.message));
      });
    }

    for (const entry of data.entries) {
      const childPath = data.path === "" ? entry.name : `${data.path}/${entry.name}`;
      if (entry.type === "dir") {
        appendEntry(entry.name, entry.type, () => {
          showDirectory(childPath).catch((err) => showError(err.message));
        });
      } else {
        appendEntry(entry.name, entry.type, async () => {
          fbError.hidden = true;
          try {
            await api("PUT", "/api/pcb-file", { path: childPath });
            browser.close();
            window.location.reload();
          } catch (err) {
            showError(`PCB選択失敗: ${err.message}`);
          }
        });
      }
    }
  };

  pcbChip.addEventListener("click", () => {
    fbError.hidden = true;
    fbEmpty.hidden = true;
    fbEntries.replaceChildren();
    browser.showModal();
    showDirectory(pcbChip.dataset.fbStart || "").catch((err) =>
      showError(err.message)
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
    fbError.hidden = true;
    const form = new FormData();
    form.append("file", file);
    try {
      await api("POST", "/api/pcb-file/upload", form);
      browser.close();
      window.location.reload();
    } catch (err) {
      showError(`アップロード失敗: ${err.message}`);
    } finally {
      uploadInput.value = "";
    }
  });
})();
