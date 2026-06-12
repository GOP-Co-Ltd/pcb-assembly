"use strict";

// マシン操作パネル: REST (/api/machine-control) への送信と
// 展開中のみのステータスポーリング。
// 対話ジョブ（accepts_commands）実行中は WS command 送信に切り替える
// （送信先が REST か WS かの違いのみで、ボタン構成は共通）。

(() => {
  const { toast, api } = window.webui;
  const panel = document.getElementById("machine-control");
  if (!panel) return;

  const positionEl = document.getElementById("mc-position");
  const homedEl = document.getElementById("mc-homed");
  const TERMINAL = new Set(["succeeded", "failed", "aborted"]);

  function activeJob() {
    const job = window.webui.jobs ? window.webui.jobs.currentJob() : null;
    return job && !TERMINAL.has(job.status) ? job : null;
  }

  function toCommand(payload) {
    if (payload.action === "jog") {
      return { type: "jog", axis: payload.axis, dist: payload.distance };
    }
    const { action, ...rest } = payload;
    return { type: action, ...rest };
  }

  async function sendControl(payload) {
    const job = activeJob();
    if (job) {
      // ジョブモード: 対話ジョブの command キューへ送る
      if (!job.accepts_commands) {
        toast("ジョブ実行中はマシン操作できません", false);
        return;
      }
      if (window.webui.jobs.sendCommand(toCommand(payload))) {
        toast("コマンドを送信しました");
      } else {
        toast("WebSocket 未接続のため送信できません", false);
      }
      return;
    }
    try {
      const status = await api("POST", "/api/machine-control", payload);
      renderStatus(status);
      toast("操作完了");
    } catch (err) {
      toast(err.message, false);
    }
  }

  // 対話を受けないジョブの実行中はパネルを disabled 表示にする
  // （テンプレート由来の disabled（フォーカスZ未設定）は維持する）
  if (window.webui.jobs) {
    const originallyDisabled = new Set(
      [...panel.querySelectorAll("button, input")].filter((c) => c.disabled)
    );
    window.webui.jobs.onUpdate((job) => {
      const blocked = job !== null && !TERMINAL.has(job.status) && !job.accepts_commands;
      panel.classList.toggle("mc-blocked", blocked);
      for (const control of panel.querySelectorAll("button, input")) {
        control.disabled = blocked || originallyDisabled.has(control);
      }
    });
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
