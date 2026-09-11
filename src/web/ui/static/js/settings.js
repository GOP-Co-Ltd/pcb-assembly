"use strict";

// 設定画面: 入力変更を debounce して machine.toml に保存する。

(() => {
  const { toast, api } = window.webui;
  const SAVE_DELAY_MS = 350;

  // 設定画面とはんだ塗布ページの両方で同じ即保存フォームを扱う。
  const forms = document.querySelectorAll("form[data-machine-settings]");
  if (forms.length === 0) return;

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
    if (text === "") {
      // 空欄は既定で「未入力」＝保存しない。空文字そのものが「無効にする」を
      // 意味する項目だけ data-allow-empty で明示的に送る
      return input.dataset.allowEmpty === "true" ? "" : null;
    }
    if (
      input.dataset.type === "str" ||
      input.dataset.type === "dispense_mode" ||
      input.dataset.type === "line_direction"
    ) {
      return text;
    }
    if (input.dataset.type === "float_or_auto" && text === "auto") {
      return "auto";
    }

    // 整数制約はサーバ（config_store の int 検証）が 400 で弾く。
    // ここでは数値化可否（parseNumber）のみ確認する。
    return parseNumber(text, input.name);
  }

  function pairValue(group) {
    const key = group.dataset.pairKey;
    const inputs = Array.from(group.querySelectorAll("input[data-pair-index]")).sort(
      (a, b) => Number(a.dataset.pairIndex) - Number(b.dataset.pairIndex)
    );
    const texts = inputs.map((input) => input.value.trim());
    // 両方空 = 未入力（保存しない）。片方空も不完全なので保存しない。
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

  async function saveValues(endpoint, values) {
    try {
      await api("PUT", endpoint, { values });
    } catch (err) {
      toast(`保存失敗: ${err.message}`, false);
    }
  }

  function enqueueSave(endpoint, values) {
    saveQueue = saveQueue.then(
      () => saveValues(endpoint, values),
      () => saveValues(endpoint, values)
    );
  }

  function scheduleSave(control) {
    const key = controlKey(control);
    if (!key || !control.form) return;
    const endpoint = control.form.dataset.endpoint;
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
        if (values !== null) enqueueSave(endpoint, values);
      }, SAVE_DELAY_MS)
    );
  }

  for (const machineForm of forms) {
    // 個々の control ではなく form で受ける。校正ファイルの入力は一覧 API の
    // 取得後に <select> へ差し替わるので、読み込み時に直接貼ると差し替え後の
    // 要素にリスナーが付かない（input / change はどちらも bubble する）
    const onEdit = (event) => {
      const control = event.target;
      if (
        control instanceof HTMLInputElement ||
        control instanceof HTMLSelectElement
      ) {
        scheduleSave(control);
      }
    };
    machineForm.addEventListener("input", onEdit);
    machineForm.addEventListener("change", onEdit);

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
  }
})();
