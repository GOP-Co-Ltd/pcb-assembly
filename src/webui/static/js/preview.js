"use strict";

// カメラプレビュー: MJPEG <img> の装着/切断・overlay/エッジ検出設定の動的反映・自動再接続。

(() => {
  const pane = document.getElementById("preview-pane");
  if (!pane) return;

  const { toast, api, debounce, createBackoff } = window.webui;
  const img = document.getElementById("preview-img");
  const status = document.getElementById("preview-status");
  const cannyLow = document.getElementById("canny-low");
  const cannyHigh = document.getElementById("canny-high");

  const DEBOUNCE_MS = 300;

  const retryBackoff = createBackoff(1000, 5000);
  let retryTimer = null;
  let stopped = false;

  function currentOverlay() {
    const radio = document.querySelector("input[name='overlay']:checked");
    return radio ? radio.value : pane.dataset.overlay || "none";
  }

  function streamUrl() {
    const params = new URLSearchParams({ overlay: currentOverlay() });
    if (cannyLow) params.set("canny_low", cannyLow.value);
    if (cannyHigh) params.set("canny_high", cannyHigh.value);
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

  // overlay/エッジ検出設定の変更連打をまとめて再接続する（connect は stopped でガード済み）
  const reconnect = debounce(connect, DEBOUNCE_MS);

  for (const radio of document.querySelectorAll("input[name='overlay']")) {
    radio.addEventListener("change", reconnect);
  }

  function bindSlider(slider, valueId) {
    if (!slider) return;
    const value = document.getElementById(valueId);
    slider.addEventListener("input", () => {
      if (value) value.textContent = slider.value;
      reconnect();
    });
  }
  bindSlider(cannyLow, "canny-low-value");
  bindSlider(cannyHigh, "canny-high-value");

  const saveButton = document.getElementById("canny-save");
  if (saveButton && cannyLow && cannyHigh) {
    saveButton.addEventListener("click", async () => {
      try {
        await api("PUT", "/api/settings/machine", {
          values: {
            "paste_dispenser.pad_align.canny_low": Number(cannyLow.value),
            "paste_dispenser.pad_align.canny_high": Number(cannyHigh.value),
          },
        });
        toast("エッジ検出パラメータを設定に保存しました");
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
