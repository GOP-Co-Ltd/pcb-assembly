"use strict";

// 設定画面: 入力変更を debounce して machine.toml に保存する。

(() => {
  const { toast, api } = window.webui;
  const SAVE_DELAY_MS = 350;

  const machineForm = document.getElementById("machine-settings-form");
  if (!machineForm) return;

  const pendingTimers = new Map();
  let saveQueue = Promise.resolve();

  function parseNumber(text, label) {
    const value = Number(text);
    if (!Number.isFinite(value)) {
      throw new Error(`${label} は数値で入力してください`);
    }
    return value;
  }

  function scalarValue(input) {
    if (input.dataset.type === "bool") return input.checked;
    const text = input.value.trim();
    if (text === "") return null;
    if (input.dataset.type === "str" || input.dataset.type === "dispense_mode") {
      return text;
    }
    if (input.dataset.type === "float_or_auto" && text === "auto") {
      return "auto";
    }

    // 整数制約はサーバ（config_store の int 検証）が 400 で弾く。
    // ここでは数値化可否（parseNumber）のみ確認する。
    return parseNumber(text, input.name);
  }

  function valuesForControl(control) {
    if (!control.name) return null;
    const value = scalarValue(control);
    return value === null ? null : { [control.name]: value };
  }

  async function saveValues(values) {
    try {
      await api("PUT", machineForm.dataset.endpoint, { values });
    } catch (err) {
      toast(`保存失敗: ${err.message}`, false);
    }
  }

  function enqueueSave(values) {
    saveQueue = saveQueue.then(() => saveValues(values), () => saveValues(values));
  }

  function scheduleSave(control) {
    const key = control.name;
    if (!key) return;
    clearTimeout(pendingTimers.get(key));
    pendingTimers.set(
      key,
      setTimeout(() => {
        pendingTimers.delete(key);
        let values = null;
        try {
          values = valuesForControl(control);
        } catch (err) {
          toast(`保存失敗: ${err.message}`, false);
          return;
        }
        if (values !== null) enqueueSave(values);
      }, SAVE_DELAY_MS)
    );
  }

  for (const control of machineForm.querySelectorAll("input, select")) {
    control.addEventListener("input", () => scheduleSave(control));
    control.addEventListener("change", () => scheduleSave(control));
  }

  machineForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (
      document.activeElement instanceof HTMLInputElement ||
      document.activeElement instanceof HTMLSelectElement
    ) {
      scheduleSave(document.activeElement);
      document.activeElement.blur();
    }
  });
})();
