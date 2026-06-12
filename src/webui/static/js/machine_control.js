"use strict";

// マシン操作パネル: REST (/api/machine-control) への送信と
// 展開中のみのステータスポーリング。
// Phase 3 でジョブモード（WS command 送信）に切り替えられるよう、
// 送信関数 sendControl を 1 箇所に集約しておく。

(() => {
  const { toast, api } = window.webui;
  const panel = document.getElementById("machine-control");
  if (!panel) return;

  const positionEl = document.getElementById("mc-position");
  const homedEl = document.getElementById("mc-homed");

  async function sendControl(payload) {
    try {
      const status = await api("POST", "/api/machine-control", payload);
      renderStatus(status);
      toast("操作完了");
    } catch (err) {
      toast(err.message, false);
    }
  }

  function renderStatus(status) {
    if (!status.connected) {
      positionEl.textContent = "位置: 接続エラー";
      homedEl.textContent = status.error ? `(${status.error})` : "";
      return;
    }
    const p = status.position;
    positionEl.textContent = `位置: X${p.x.toFixed(3)} Y${p.y.toFixed(3)} Z${p.z.toFixed(3)}`;
    homedEl.textContent = `homed: ${status.homed_axes || "なし"}`;
  }

  async function pollStatus() {
    try {
      renderStatus(await api("GET", "/api/klipper/status"));
    } catch (err) {
      positionEl.textContent = "位置: 取得失敗";
    }
  }

  // homing
  for (const button of panel.querySelectorAll("[data-mc-home]")) {
    button.addEventListener("click", () => {
      const axis = button.dataset.mcHome;
      sendControl({ action: "home", axes: axis ? [axis] : [] });
    });
  }

  // jog
  for (const button of panel.querySelectorAll("[data-mc-jog]")) {
    button.addEventListener("click", () => {
      sendControl({
        action: "jog",
        axis: button.dataset.mcJog,
        distance: Number(button.dataset.mcDist),
      });
    });
  }

  // absolute move
  document.getElementById("mc-move").addEventListener("click", () => {
    const value = (id) => {
      const text = document.getElementById(id).value.trim();
      return text === "" ? null : Number(text);
    };
    sendControl({ action: "move", x: value("mc-x"), y: value("mc-y"), z: value("mc-z") });
  });

  document.getElementById("mc-relax").addEventListener("click", () => {
    sendControl({ action: "relax" });
  });

  document.getElementById("mc-focus-z").addEventListener("click", () => {
    sendControl({ action: "focus_z" });
  });

  // 展開中のみステータスをポーリングする
  let timer = null;
  panel.addEventListener("toggle", () => {
    if (panel.open) {
      pollStatus();
      timer = setInterval(pollStatus, 2000);
    } else if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  });
})();
