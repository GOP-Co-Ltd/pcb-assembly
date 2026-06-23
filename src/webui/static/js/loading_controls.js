"use strict";

// ローディング操作パネル: ローディング段階の対話ジョブへ
// extrude / suck / finish コマンドを WS で送る。
// 有効化条件: 実行中ジョブが accepts_commands かつ
// progress_stage が data-loading-stage と一致。

(() => {
  const { toast, api } = window.webui;
  const panel = document.getElementById("loading-controls");
  if (!panel || !window.webui.jobs) return;

  const TERMINAL = new Set(["succeeded", "failed", "aborted"]);
  const loadingStage = panel.dataset.loadingStage;
  const amountInput = document.getElementById("lc-amount");
  const rotationsInput = document.getElementById("lc-rotations");
  const rateInput = document.getElementById("lc-rate");
  const accelInput = document.getElementById("lc-accel");
  const massInput = document.getElementById("lc-mass-mg");
  const massCalibration = document.getElementById("loading-mass-calibration");
  let calculatedRotationsPerUl = null;
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
  bindMassCalibration();

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

  function bindMassCalibration() {
    if (!massCalibration || !massInput || !rotationsInput || !rateInput || !accelInput) {
      return;
    }
    const applyButton = document.getElementById("lc-apply-rotations-per-ul");
    applyButton.addEventListener("click", applyRotationsPerUl);
    for (const input of [massInput, rotationsInput, rateInput, accelInput]) {
      input.addEventListener("input", renderMassCalibration);
    }
    renderMassCalibration();
  }

  function renderMassCalibration() {
    calculatedRotationsPerUl = null;
    const density = Number(massCalibration.dataset.density);
    const massMg = Number(massInput.value);
    const rotations = Number(rotationsInput.value);
    const rate = Number(rateInput.value);
    const accel = Number(accelInput.value);
    const rawEffectiveRotations = rotations - (rate * rate) / accel;
    const effectiveRotations = Math.max(0, rawEffectiveRotations);
    setOutput("lc-effective-rotations", effectiveRotations);
    setOutput("lc-volume-ul", massMg > 0 && density > 0 ? massMg / density : null);
    setOutput("lc-rotations-per-ul", null);

    const message = document.getElementById("lc-calibration-message");
    const applyButton = document.getElementById("lc-apply-rotations-per-ul");
    applyButton.disabled = true;
    if (!(massMg > 0 && density > 0 && rotations > 0 && rate > 0 && accel > 0)) {
      message.textContent = "";
      return;
    }
    if (!(effectiveRotations > 0)) {
      message.textContent = "定速区間がないため計算できません";
      return;
    }

    const volumeUl = massMg / density;
    calculatedRotationsPerUl = effectiveRotations / volumeUl;
    setOutput("lc-rotations-per-ul", calculatedRotationsPerUl);
    message.textContent = "";
    applyButton.disabled = false;
  }

  function setOutput(id, value) {
    const output = document.getElementById(id);
    if (!output) return;
    output.value = value === null ? "-" : value.toFixed(6);
    output.textContent = output.value;
  }

  async function applyRotationsPerUl() {
    if (!(calculatedRotationsPerUl > 0)) return;
    const value = Number(calculatedRotationsPerUl.toFixed(6));
    const applyButton = document.getElementById("lc-apply-rotations-per-ul");
    applyButton.disabled = true;
    try {
      await api("PUT", "/api/settings/machine", {
        values: { "paste_dispenser.rotations_per_ul": value },
      });
      document.getElementById("lc-current-rotations-per-ul").textContent =
        value.toFixed(6);
      toast(`rotations_per_ul を ${value.toFixed(6)} に設定しました`);
    } catch (err) {
      toast(err.message, false);
    } finally {
      renderMassCalibration();
    }
  }
})();
