"use strict";

// トップバーの更新通知バッジ（全ページ共通）。
//
// このファイルはロジックを持たない。「どのホストに何件あるか」「どの更新ページへ
// 遷移するか」はサーバ（GET /api/update-notice）が決め、表示文字列まで組み立てる。
(() => {
  const badge = document.getElementById("update-badge");
  if (!badge) return;

  const { noticeUrl } = badge.dataset;
  const { frontendApi } = window.webui;

  // 更新の有無はサーバ側の定期 fetch（UpdateSettings.watch_interval = 30 分）で
  // 更新されるので、画面から頻繁に取得しても新しい情報は増えない。1 回の取得で git が
  // 十数プロセス起動する（frontend 自身 + 機体 backend）ので、取得間隔を長くする
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
    // （機体が停止している間ずっと、画面がエラー表示で埋まるのを避ける）
    refresh()
      .catch(() => {})
      .finally(() => setTimeout(tick, INTERVAL_MS));
  };

  tick();
})();
