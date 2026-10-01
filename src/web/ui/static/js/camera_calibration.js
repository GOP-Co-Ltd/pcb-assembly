"use strict";

// カメラキャリブレーションページ。square_size を入力時に保存し、次回表示時に復元する。
// クロップ設定は settings ページの汎用即保存フォームで扱う（このページには置かない）。

(() => {
  const { api, debounce } = window.webui;
  const DEBOUNCE_MS = 400;

  function bindSquareSizePersist() {
    const form = document.getElementById("job-form");
    if (!form) return;
    const input = form.querySelector('[name="square_size"]');
    if (!input) return;
    const jobName = form.dataset.jobName;

    const persist = debounce(() => {
      const text = input.value.trim();
      if (text === "") return;
      const value = Number(text);
      if (!Number.isFinite(value)) return;
      api("POST", `/api/jobs/${jobName}/param-defaults`, {
        values: { square_size: value },
      }).catch(() => {});
    }, DEBOUNCE_MS);

    input.addEventListener("input", persist);
  }

  bindSquareSizePersist();
})();
