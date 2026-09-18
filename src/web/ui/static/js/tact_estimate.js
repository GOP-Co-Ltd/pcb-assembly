"use strict";

// はんだ塗布ページの所要時間見積り。
// 値（秒）はサーバーが算出して返し、ここは表示への流し込みと再取得のきっかけだけを持つ。

(() => {
  const { api, debounce, formatDuration } = window.webui;
  const root = document.querySelector("[data-testid='tact-estimate']");
  if (root === null) return;

  const valueEl = root.querySelector(".tact-estimate-value");
  const detailEl = root.querySelector(".tact-estimate-detail");
  let pending = null;

  async function refresh() {
    // 連打・イベント重複で古い応答が新しい表示を上書きしないよう、最後の 1 本だけ映す
    const request = api("GET", "/api/pasting/tact-estimate");
    pending = request;
    try {
      const estimate = await request;
      if (pending !== request) return;
      valueEl.textContent = formatDuration(estimate.total_seconds);
      detailEl.textContent =
        `${estimate.pad_count} pad` +
        `（前処理 ${formatDuration(estimate.setup_seconds)}` +
        ` + 塗布 ${formatDuration(estimate.dispense_seconds)}）`;
    } catch (err) {
      if (pending !== request) return;
      valueEl.textContent = "—";
      // PCB 未選択（409）も含め、見積もれない理由をそのまま出す
      detailEl.textContent = err.message;
    }
  }

  // PCB 切替・machine 設定の保存（state_changed）と pad 設定の編集で対象が変わる。
  // 連続編集では pad 全枚の経路生成を毎回やり直さないよう、最後の 1 回にまとめる。
  const scheduleRefresh = debounce(refresh, 400);
  document.addEventListener("webui:state-changed", scheduleRefresh);
  document.addEventListener("webui:pad-config-changed", scheduleRefresh);
  refresh();
})();
