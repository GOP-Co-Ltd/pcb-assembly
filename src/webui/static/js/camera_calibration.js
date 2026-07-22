"use strict";

// カメラキャリブレーションページ: クロップ設定の即保存 + square_size の入力時復元保存。

(() => {
  const { toast, api, debounce } = window.webui;
  const DEBOUNCE_MS = 400;

  function bindCropAutoSave() {
    const inputs = document.querySelectorAll("[data-machine-key]");
    if (inputs.length === 0) return;

    const save = debounce(async () => {
      const values = {};
      for (const input of inputs) {
        const text = input.value.trim();
        if (text === "") continue;
        const value = Number(text);
        if (!Number.isFinite(value)) continue;
        values[input.dataset.machineKey] = value;
      }
      if (Object.keys(values).length === 0) return;
      let result;
      try {
        result = await api("PUT", "/api/settings/machine", { values });
      } catch (err) {
        toast(err.message, false);
        return;
      }
      for (const input of inputs) {
        const field = result.fields.find(
          (f) => f.key === input.dataset.machineKey
        );
        if (field && field.value !== null && field.value !== undefined) {
          input.value = field.value;
        }
      }
      toast("クロップ設定を保存しました");
    }, DEBOUNCE_MS);

    for (const input of inputs) {
      input.addEventListener("input", save);
    }
  }

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

  bindCropAutoSave();
  bindSquareSizePersist();
})();
