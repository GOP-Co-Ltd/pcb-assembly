"use strict";

// マシン選択ドロップダウン: 選択された遷移先へ移動するだけ。
// 表示名（label）と遷移先 URL はサーバがテンプレートで組んでいる。

(() => {
  const select = document.getElementById("machine-select");
  if (!select) return;

  select.addEventListener("change", () => {
    window.location.assign(select.value);
  });
})();
