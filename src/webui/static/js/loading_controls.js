"use strict";

// ローディング操作パネル: ローディング段階の対話ジョブへ
// extrude / suck / finish コマンドを WS で送る。
// 有効化条件: 実行中ジョブが accepts_commands かつ
// progress_stage が data-loading-stage と一致。

(() => {
  const { toast } = window.webui;
  const panel = document.getElementById("loading-controls");
  if (!panel || !window.webui.jobs) return;

  const TERMINAL = new Set(["succeeded", "failed", "aborted"]);
  const loadingStage = panel.dataset.loadingStage;
  const amountInput = document.getElementById("lc-amount");
  const rotationsInput = document.getElementById("lc-rotations");
  const rateInput = document.getElementById("lc-rate");
  const accelInput = document.getElementById("lc-accel");
  const buttons = [];
  for (const [id, type] of [
    ["lc-extrude", "extrude"],
    ["lc-suck", "suck"],
    ["lc-extrude-rotations", "extrude_rotations"],
    ["lc-suck-rotations", "suck_rotations"],
    ["lc-finish", "finish"],
  ]) {
    const button = document.getElementById(id);
    if (!button) continue;
    button.addEventListener("click", () => sendAction(type));
    buttons.push(button);
  }

  function update(job) {
    const enabled =
      job != null &&
      !TERMINAL.has(job.status) &&
      job.accepts_commands &&
      job.progress_stage === loadingStage;
    for (const button of buttons) button.disabled = !enabled;
  }

  window.webui.jobs.onUpdate(update);
  update(window.webui.jobs.currentJob());
  bindLoadingParamSync();

  function sendAction(type) {
    const command = { type };
    if (type === "extrude" || type === "suck") {
      const amount = Number(amountInput.value);
      if (!(amount > 0)) {
        toast("量には正の数値を入力してください", false);
        return;
      }
      command.amount = amount;
    } else if (type === "extrude_rotations" || type === "suck_rotations") {
      const rotations = Number(rotationsInput.value);
      const rate = Number(rateInput.value);
      const accel = Number(accelInput.value);
      if (!(rotations > 0 && rate > 0 && accel > 0)) {
        toast("回転数・速度・加速度には正の数値を入力してください", false);
        return;
      }
      command.rotations = rotations;
      command.rate = rate;
      command.accel = accel;
    }
    if (window.webui.jobs.sendCommand(command)) {
      toast("コマンドを送信しました");
    } else {
      toast("WebSocket 未接続のため送信できません", false);
    }
  }

  function bindLoadingParamSync() {
    const form = document.getElementById("job-form");
    if (!form || !form.classList.contains("loading-run-form")) return;
    const bindings = [
      ["amount", amountInput],
      ["rotations", rotationsInput],
      ["rate", rateInput],
      ["accel", accelInput],
    ];
    function sync() {
      for (const [name, source] of bindings) {
        const target = form.querySelector(`[name="${name}"]`);
        if (target && source) target.value = source.value;
      }
    }
    for (const [, input] of bindings) {
      if (input) input.addEventListener("input", sync);
    }
    form.addEventListener("submit", sync, { capture: true });
    sync();
  }
})();
</content>
