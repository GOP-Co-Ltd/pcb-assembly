"use strict";

// トップバーの更新通知バッジ（全ページ共通）。
//
// このファイルはロジックを持たない。「どのホストに何件あるか」「どの更新ページへ
// 飛ぶか」はサーバ（GET /api/update-notice）が決めて表示文字列まで組む。
(() => {
  const badge = document.getElementById("update-badge");
  if (!badge) return;

  const { noticeUrl } = badge.dataset;
  const { frontendApi } = window.webui;

  // 更新の有無はサーバ側の定期 fetch（UpdateSettings.watch_interval = 30 分）が
  // 保つので、画面を細かく回しても新しい情報は増えない。1 回の取得で git が
  // 十数プロセス起きる（frontend 自身 + 機体 backend）ので粗く回す
  const INTERVAL_MS = 300000;

  async function refresh() {
    const notice = await frontendApi("GET", noticeUrl);
    badge.textContent = notice.label;
    badge.title = notice.detail;
    badge.href = notice.href;
    badge.hidden = !notice.available;
  }

  const tick = () => {
    // 通知は補助情報。取得に失敗してもトーストは出さず次の周期を待つ
    // （機体が落ちている間じゅう画面がエラーで埋まるのを避ける）
    refresh()
      .catch(() => {})
      .finally(() => setTimeout(tick, INTERVAL_MS));
  };

  tick();
})();
