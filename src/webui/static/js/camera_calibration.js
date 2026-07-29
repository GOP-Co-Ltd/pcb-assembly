"use strict";

// カメラキャリブレーションページ: 数値入力の入力時復元保存。
// どのパラメータを保存するかはサーバの persisted_params が決める（JS は絞らない）。
// クロップ設定は settings ページの汎用即保存フォームへ統一（このページには置かない）。

(() => {
  const { api, debounce } = window.webui;
  const DEBOUNCE_MS = 400;

  function bindNumberPersist() {
    const form = document.getElementById("job-form");
    if (!form) return;
    const jobName = form.dataset.jobName;

    for (const input of form.querySelectorAll('input[type="number"][name]')) {
      const persist = debounce(() => {
        const text = input.value.trim();
        if (text === "") return;
        const value = Number(text);
        if (!Number.isFinite(value)) return;
        api("POST", `/api/jobs/${jobName}/param-defaults`, {
          values: { [input.name]: value },
        }).catch(() => {});
      }, DEBOUNCE_MS);

      input.addEventListener("input", persist);
    }
  }

  bindNumberPersist();
})();
