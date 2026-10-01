"use strict";

// Klipper / Stage ステータスカード。/api/klipper/status の 2 秒ポーリング、
// /api/stage/limits のページ表示時 1 回の取得、任意 G-code 送信を行う。

(() => {
  const { toast, api, formatPosition } = window.webui;

  const connectedEl = document.getElementById("ks-connected");
  const positionEl = document.getElementById("ks-position");
  const homedEl = document.getElementById("ks-homed");
  if (!connectedEl) return;

  async function pollStatus() {
    try {
      const status = await api("GET", "/api/klipper/status");
      if (!status.connected) {
        connectedEl.textContent = `接続: NG${status.error ? ` (${status.error})` : ""}`;
        positionEl.textContent = "位置: ---";
        homedEl.textContent = "homed: ---";
        return;
      }
      const p = status.position;
      connectedEl.textContent = "接続: OK";
      positionEl.textContent = `位置: ${formatPosition(p)}`;
      homedEl.textContent = `homed: ${status.homed_axes || "なし"}`;
    } catch (err) {
      connectedEl.textContent = `接続: 取得失敗 (${err.message})`;
    }
  }
  // 放置タブによる Moonraker の負荷を減らす（背景タブでは止め、復帰時に即 1 回取得して再開）
  let timer = null;

  function startPolling() {
    if (timer !== null) return;
    pollStatus();
    timer = setInterval(pollStatus, 2000);
  }

  function stopPolling() {
    clearInterval(timer);
    timer = null;
  }

  startPolling();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else startPolling();
  });

  // limits は静的なのでページ表示時に 1 回だけ取得する
  (async () => {
    try {
      const limits = await api("GET", "/api/stage/limits");
      for (const axis of ["x", "y", "z"]) {
        document.getElementById(`ks-limit-${axis}-min`).textContent = limits[axis].min;
        document.getElementById(`ks-limit-${axis}-max`).textContent = limits[axis].max;
      }
    } catch (err) {
      toast(`limits 取得失敗: ${err.message}`, false);
    }
  })();

  document.getElementById("ks-gcode-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.getElementById("ks-gcode");
    const text = input.value.trim();
    if (!text) return;
    try {
      await api("POST", "/api/machine-control", { action: "gcode", gcode: text });
      toast("G-code 送信完了");
    } catch (err) {
      toast(err.message, false);
    }
  });
})();
