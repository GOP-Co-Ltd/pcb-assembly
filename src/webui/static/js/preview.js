"use strict";

// カメラプレビュー: MJPEG <img> の装着/切断・overlay/検出パラメータの動的反映・自動再接続。
// 輪郭調整ページでは #contour-modes（サーバ提供のモード定義 JSON）を読み、
// モード切替・スライダー反映・保存を data 駆動で行う（キー名は JS に持たない）。

(() => {
  const pane = document.getElementById("preview-pane");
  if (!pane) return;

  const { toast, api, debounce, createBackoff } = window.webui;
  const img = document.getElementById("preview-img");
  const status = document.getElementById("preview-status");
  const sliders = Array.from(document.querySelectorAll("input[data-param]"));
  const modesEl = document.getElementById("contour-modes");
  const modes = modesEl ? JSON.parse(modesEl.textContent) : null;
  const modeSelect = document.getElementById("contour-mode");

  const DEBOUNCE_MS = 300;

  const retryBackoff = createBackoff(1000, 5000);
  let retryTimer = null;
  let stopped = false;

  function currentMode() {
    return modes && modeSelect ? modes[modeSelect.value] : null;
  }

  function currentOverlay() {
    const mode = currentMode();
    if (mode) return mode.overlay;
    const radio = document.querySelector("input[name='overlay']:checked");
    return radio ? radio.value : pane.dataset.overlay || "none";
  }

  function streamUrl() {
    const params = new URLSearchParams({ overlay: currentOverlay() });
    for (const slider of sliders) params.set(slider.dataset.param, slider.value);
    params.set("t", Date.now()); // 再接続時のキャッシュ回避
    return `${pane.dataset.streamUrl}?${params}`;
  }

  function connect() {
    if (stopped) return;
    status.textContent = "接続中…";
    img.src = streamUrl();
  }

  img.addEventListener("load", () => {
    retryBackoff.reset();
    status.textContent = "";
  });

  img.addEventListener("error", () => {
    // マシン切替・カメラ失敗からの復帰: 指数バックオフでリトライ
    if (stopped) return;
    status.textContent = "切断されました。再接続します…";
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, retryBackoff.next());
  });

  // overlay/パラメータ変更の連打をまとめて再接続する（connect は stopped でガード済み）
  const reconnect = debounce(connect, DEBOUNCE_MS);

  for (const radio of document.querySelectorAll("input[name='overlay']")) {
    radio.addEventListener("change", reconnect);
  }

  function showSliderValue(slider) {
    const value = document.getElementById(`${slider.id}-value`);
    if (value) value.textContent = slider.value;
  }

  for (const slider of sliders) {
    slider.addEventListener("input", () => {
      showSliderValue(slider);
      reconnect();
    });
  }

  if (modeSelect) {
    // モード切替: フォーム値をそのモードの現在値に入替え、ストリームを再接続
    modeSelect.addEventListener("change", () => {
      const mode = currentMode();
      if (!mode) return;
      for (const slider of sliders) {
        const param = mode.params[slider.dataset.param];
        if (!param) continue;
        slider.value = param.value;
        showSliderValue(slider);
      }
      reconnect();
    });
  }

  const saveButton = document.getElementById("contour-save");
  if (saveButton && modes) {
    saveButton.addEventListener("click", async () => {
      const mode = currentMode();
      if (!mode) return;
      const values = {};
      for (const slider of sliders) {
        const param = mode.params[slider.dataset.param];
        if (param) values[param.key] = Number(slider.value);
      }
      try {
        await api("PUT", "/api/settings/machine", { values });
        toast(`${mode.label}のパラメータを設定に保存しました`);
      } catch (err) {
        toast(`保存失敗: ${err.message}`, false);
      }
    });
  }

  // ページ離脱でストリームを切断（参照カウントが下がり hub が停止する）
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(retryTimer);
    img.removeAttribute("src");
  });

  connect();
})();
