"use strict";

// 設定画面: フォーム値を収集して PUT。

(() => {
  const { toast, api } = window.webui;

  function parseFloatPair(text, name) {
    const trimmed = text.replace(/^\[/, "").replace(/\]$/, "");
    const parts = trimmed.split(/[\s,]+/).filter(Boolean);
    if (parts.length !== 2) {
      throw new Error(`${name} は2つの数値で入力してください`);
    }
    const pair = parts.map((part) => Number(part));
    if (pair.some((value) => Number.isNaN(value))) {
      throw new Error(`${name} は2つの数値で入力してください`);
    }
    return pair;
  }

  function collectValues(form) {
    const values = {};
    for (const input of form.querySelectorAll("input[name]")) {
      const text = input.value.trim();
      if (text === "") continue; // 未入力の項目は送信しない
      if (input.dataset.type === "str") {
        values[input.name] = text;
      } else if (input.dataset.type === "int") {
        values[input.name] = parseInt(text, 10);
      } else if (input.dataset.type === "float_pair") {
        values[input.name] = parseFloatPair(text, input.name);
      } else {
        values[input.name] = Number(text);
      }
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
