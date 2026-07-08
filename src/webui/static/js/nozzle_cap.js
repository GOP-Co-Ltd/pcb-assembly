"use strict";

// ノズルキャップ位置の設定ページ: 記録ボタン → POST → 返却値で表示更新のみ。

(() => {
  const { toast, api } = window.webui;
  const recordButton = document.getElementById("nc-record");
  const currentEl = document.getElementById("nc-current");
  if (!recordButton || !currentEl) return;

  recordButton.addEventListener("click", async () => {
    try {
      const p = await api("POST", "/api/pasting/nozzle-cap/record");
      currentEl.textContent =
        `X ${p.x.toFixed(3)} / Y ${p.y.toFixed(3)} / Z ${p.z.toFixed(3)}`;
      toast("ノズルキャップ位置を記録しました");
    } catch (err) {
      toast(err.message, false);
    }
  });
})();
