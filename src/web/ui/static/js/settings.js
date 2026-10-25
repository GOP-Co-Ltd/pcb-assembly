"use strict";

// machine.toml の即保存フォーム（form[data-machine-settings]）。入力が止まったら項目ごとに保存する。
// 設定ページと、機能のページに置いた設定欄（はんだ塗布・ノズル位置・通知音）が共用する。
// 保存は api.js の pendingSaves を通すので、ジョブを始める前に送り終える。

(() => {
  const { api, pendingSaves } = window.webui;
  const SAVE_DELAY_MS = 350;

  const forms = document.querySelectorAll("form[data-machine-settings]");
  if (forms.length === 0) return;

  // 同じ項目を続けて保存したとき、先の値が後から届いて上書きしないよう順に送る
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
    // 数値の欄（number / range）だけ数値で送る。文字列の欄と select は文字列のまま送り、
    // 型と範囲の検証はサーバが 400 で返す
    if (input.type === "number" || input.type === "range") {
      return parseNumber(text, input.name);
    }
    return text;
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

  // 読めない入力（数値欄の文字など）も保存の失敗として数える（ジョブを始めさせない）
  function save(control) {
    const previous = saveQueue;
    saveQueue = pendingSaves.run("保存失敗", async () => {
      const values = valuesForControl(control);
      if (values === null) return;
      await previous;
      await api("PUT", control.form.dataset.endpoint, { values });
    });
  }

  function scheduleSave(control) {
    const key = controlKey(control);
    if (!key || !control.form) return;
    pendingSaves.schedule(`machine-settings:${key}`, () => save(control), SAVE_DELAY_MS);
  }

  for (const machineForm of forms) {
    // 個々の control ではなく form でイベントを受ける。校正ファイルの入力は一覧 API の
    // 取得後に <select> へ差し替わるので、読み込み時に直接リスナーを付けると、差し替え後の
    // 要素にはリスナーが付かない（input / change はどちらも bubble する）
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
