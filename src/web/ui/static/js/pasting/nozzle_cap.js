"use strict";

// ノズル位置の設定ページ。記録ボタン → POST → 返却値で表示を更新する処理だけを持つ。
// クリーニングのテストはジョブなので、開始と経過の表示は job_console.js が受け持つ。

(() => {
  const { toast, api } = window.webui;

  // 要素ごとに配線する。まとめて早期 return すると、片方の節が無いページで
  // もう片方のボタンまで動かなくなる。表示文字列はサーバーが組んで返すので、そのまま入れる。
  const bindRecord = (buttonId, currentId, endpoint, message) => {
    const button = document.getElementById(buttonId);
    const currentEl = document.getElementById(currentId);
    if (!button || !currentEl) return;

    button.addEventListener("click", async () => {
      try {
        currentEl.textContent = (await api("POST", endpoint)).label;
        toast(message);
      } catch (err) {
        toast(err.message, false);
      }
    });
  };

  bindRecord(
    "nc-record",
    "nc-current",
    "/api/pasting/nozzle/cap/record",
    "ノズルキャップ位置を記録しました",
  );
  bindRecord(
    "ncl-record",
    "ncl-current",
    "/api/pasting/nozzle/clean/record",
    "ノズルクリーニング位置を記録しました",
  );
})();
