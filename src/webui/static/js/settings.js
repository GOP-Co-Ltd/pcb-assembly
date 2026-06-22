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
    const text = input.value.trim();
    if (text === "") return null;
    if (input.dataset.type === "str" || input.dataset.type === "dispense_mode") {
      return text;
    }
    if (input.dataset.type === "float_or_auto" && text === "auto") {
      return "auto";
    }

    const value = parseNumber(text, input.name);
    if (input.dataset.type === "int" && !Number.isInteger(value)) {
      throw new Error(`${input.name} は整数で入力してください`);
    }
    return input.dataset.type === "int" ? value : Number(value);
  }

  function pairValue(group) {
    const key = group.dataset.pairKey;
    const inputs = Array.from(group.querySelectorAll("input[data-pair-index]")).sort(
      (a, b) => Number(a.dataset.pairIndex) - Number(b.dataset.pairIndex)
    );
    const texts = inputs.map((input) => input.value.trim());
    if (texts.every((text) => text === "")) return null;
    if (texts.some((text) => text === "")) return null;
    return texts.map((text) => parseNumber(text, key));
  }

  function valuesForControl(control) {
    const group = control.closest("[data-pair-key].settings-pair");
    if (group) {
      const value = pairValue(group);
      return value === null ? null : { [group.dataset.pairKey]: value };
    }

    if (!control.name) return null;
    const value = scalarValue(control);
    return value === null ? null : { [control.name]: value };
  }

  function controlKey(control) {
    const group = control.closest("[data-pair-key].settings-pair");
    return group ? group.dataset.pairKey : control.name;
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
    const key = controlKey(control);
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
