"use strict";

// 設定画面: フォーム値を収集して PUT。

(() => {
  const { toast, api } = window.webui;

  function collectValues(form) {
    const values = {};
    for (const input of form.querySelectorAll("input[name]")) {
      const text = input.value.trim();
      if (text === "") continue; // 未入力の項目は送信しない
      if (input.dataset.type === "str") {
        values[input.name] = text;
      } else if (input.dataset.type === "int") {
        values[input.name] = parseInt(text, 10);
      } else {
        values[input.name] = Number(text);
      }
    }
    for (const group of form.querySelectorAll("[data-pair-key]")) {
      if (!group.classList.contains("settings-pair")) continue;
      const key = group.dataset.pairKey;
      const inputs = Array.from(group.querySelectorAll("input[data-pair-index]"))
        .sort((a, b) => Number(a.dataset.pairIndex) - Number(b.dataset.pairIndex));
      const texts = inputs.map((input) => input.value.trim());
      if (texts.every((text) => text === "")) continue;
      if (texts.some((text) => text === "")) {
        throw new Error(`${key} は2つの数値で入力してください`);
      }
      const pair = texts.map((text) => Number(text));
      if (pair.some((value) => Number.isNaN(value))) {
        throw new Error(`${key} は2つの数値で入力してください`);
      }
      values[key] = pair;
    }
    return values;
  }

  const machineForm = document.getElementById("machine-settings-form");
  if (machineForm) {
    machineForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await api("PUT", machineForm.dataset.endpoint, {
          values: collectValues(machineForm),
        });
        toast("マシン設定を保存しました");
      } catch (err) {
        toast(`保存失敗: ${err.message}`, false);
      }
    });
  }

})();
