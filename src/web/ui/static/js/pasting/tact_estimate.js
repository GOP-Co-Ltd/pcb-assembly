"use strict";

// はんだ塗布ページの予測所要時間。pad 編集欄が基板の設定を読み直したときと、machine 全体設定欄を保存したときに取り直す。
// 時間・内訳・予測できない理由の文言はサーバーが組むので、ここはそのまま出す。

(() => {
  const label = document.getElementById("tact-estimate-label");
  const detail = document.getElementById("tact-estimate-detail");
  if (!label || !detail) return;

  const { api } = window.webui;
  let latest = 0;

  async function refresh() {
    // 続けて編集したとき、先に送った要求の応答で新しい表示を上書きしない
    const request = ++latest;
    try {
      const estimate = await api("GET", "/api/pasting/board/tact-estimate");
      if (request !== latest) return;
      label.textContent = estimate.label ?? "—";
      detail.textContent = estimate.detail ?? estimate.error ?? "";
    } catch (err) {
      if (request !== latest) return;
      label.textContent = "—";
      detail.textContent = err.message;
    }
  }

  document.addEventListener("pasting:board-loaded", refresh);
  // Auto しきい値と pad 補正は塗布方式と経路を変える
  document.addEventListener("machine-settings:saved", refresh);
})();
