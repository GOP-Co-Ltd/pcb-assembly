"use strict";

// 設定画面: フォーム値を収集して PUT。モーション設定は RESTART 確認付き。

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

  const motionForm = document.getElementById("motion-settings-form");
  if (motionForm) {
    motionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const restart = document.getElementById("motion-restart").checked;
      if (restart && !window.confirm("Klipper を RESTART します。よろしいですか？")) {
        return;
      }
      try {
        const result = await api("PUT", motionForm.dataset.endpoint, {
          values: collectValues(motionForm),
          restart,
        });
        if (!result.restart_requested) {
          toast("モーション設定を保存しました（RESTART なし）");
        } else if (result.restart_ok) {
          toast("モーション設定を保存し、Klipper を RESTART しました");
        } else {
          toast(`保存しましたが RESTART に失敗: ${result.restart_error}`, false);
        }
      } catch (err) {
        toast(`保存失敗: ${err.message}`, false);
      }
    });
  }
})();
