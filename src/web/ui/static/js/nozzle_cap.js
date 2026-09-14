"use strict";

// ノズル位置の設定ページ: 記録ボタン → POST → 返却値で表示更新のみ。

(() => {
  const { toast, api } = window.webui;

  // 要素ごとに配線する。まとめて早期 return すると、片方の節が無いページで
  // もう片方のボタンまで死ぬ。
  const bindRecord = (buttonId, currentId, endpoint, render, message) => {
    const button = document.getElementById(buttonId);
    const currentEl = document.getElementById(currentId);
    if (!button || !currentEl) return;

    button.addEventListener("click", async () => {
      try {
        const recorded = await api("POST", endpoint);
        currentEl.textContent = render(recorded);
        toast(message);
      } catch (err) {
        toast(err.message, false);
      }
    });
  };

  bindRecord(
    "nc-record",
    "nc-current",
    "/api/pasting/nozzle-cap/record",
    (p) => `X ${p.x.toFixed(3)} / Y ${p.y.toFixed(3)} / Z ${p.z.toFixed(3)}`,
    "ノズルキャップ位置を記録しました",
  );

  // クリーニング側は表示文字列をサーバーが組んで返すので、そのまま入れる。
  bindRecord(
    "ncl-record",
    "ncl-current",
    "/api/pasting/nozzle-clean/record",
    (recorded) => recorded.label,
    "ノズルクリーニング位置を記録しました",
  );
})();
