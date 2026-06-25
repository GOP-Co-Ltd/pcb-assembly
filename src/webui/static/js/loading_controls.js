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
  const retractRotationsInput = document.getElementById("lc-retract-rotations");
  const massInput = document.getElementById("lc-mass-mg");
  const massCalibration = document.getElementById("loading-mass-calibration");
  const CALIBRATION_DELAY_MS = 250;
  // 入力時即保存（/api/jobs/<name>/param-defaults へ POST）の debounce
  const PARAM_SAVE_DELAY_MS = 400;
  // 設定キー -> 現在値 output id（適用成功時に表示を更新するため）
  const CURRENT_OUTPUT_FOR = {
    "paste_dispenser.rotations_per_ul": "lc-current-rotations-per-ul",
    "paste_dispenser.max_dispense_rate": "lc-current-dispense-rate",
    "paste_dispenser.dispense_accel": "lc-current-dispense-accel",
  };
  // 最後に取得・表示した算出値（適用ボタンが送る値）。
  let computed = { rpu: null, rate: null, accel: null };
  let fetchTimer = null;
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
      if (type === "extrude_rotations") {
        // 押出に引き戻しを 1 セットで付随（負値・非数は 0＝引き戻しなし）。
        const retract = Number(retractRotationsInput.value);
        command.retract_rotations = retract >= 0 ? retract : 0;
      }
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
    const jobName = form.dataset.jobName;
    const bindings = [
      ["amount", amountInput],
      ["rotations", rotationsInput],
      ["rate", rateInput],
      ["accel", accelInput],
      ["retract_rotations", retractRotationsInput],
    ];
    let saveTimer = null;
    function sync() {
      for (const [name, source] of bindings) {
        const target = form.querySelector(`[name="${name}"]`);
        if (target && source) target.value = source.value;
      }
    }
    // 「実行」を待たず、入力するそばから次回フォーム既定値として保存する
    // （質量キャリブのブートストラップ等、ジョブ未実行でもリロードで復元される）。
    function persistDefaults() {
      if (!jobName) return;
      const values = {};
      for (const [name, source] of bindings) {
        if (!source || source.value === "") continue;
        const value = Number(source.value);
        if (Number.isFinite(value)) values[name] = value;
      }
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        api("POST", `/api/jobs/${jobName}/param-defaults`, { values }).catch(
          () => {}
        );
      }, PARAM_SAVE_DELAY_MS);
    }
    function onInput() {
      sync();
      persistDefaults();
    }
    for (const [, input] of bindings) {
      if (input) input.addEventListener("input", onInput);
    }
    form.addEventListener("submit", sync, { capture: true });
    sync();
  }

  function bindMassCalibration() {
    if (!massCalibration || !massInput || !rotationsInput || !rateInput || !accelInput) {
      return;
    }
    for (const input of [massInput, rotationsInput, rateInput, accelInput]) {
      input.addEventListener("input", scheduleFetch);
    }
    bindApply("lc-apply-rotations-per-ul", () => ({
      "paste_dispenser.rotations_per_ul": computed.rpu,
    }));
    bindApply("lc-apply-dispense-rate", () => ({
      "paste_dispenser.max_dispense_rate": computed.rate,
    }));
    bindApply("lc-apply-dispense-accel", () => ({
      "paste_dispenser.dispense_accel": computed.accel,
    }));
    bindApply("lc-apply-all", () => ({
      "paste_dispenser.rotations_per_ul": computed.rpu,
      "paste_dispenser.max_dispense_rate": computed.rate,
      "paste_dispenser.dispense_accel": computed.accel,
    }));
    scheduleFetch();
  }

  function bindApply(buttonId, valuesFor) {
    const button = document.getElementById(buttonId);
    if (!button) return;
    button.addEventListener("click", () => applyValues(valuesFor(), buttonId));
  }

  function scheduleFetch() {
    clearTimeout(fetchTimer);
    fetchTimer = setTimeout(fetchCalibration, CALIBRATION_DELAY_MS);
  }

  async function fetchCalibration() {
    const params = new URLSearchParams({
      mass_mg: massInput.value || "0",
      rotations: rotationsInput.value || "0",
      rate: rateInput.value || "0",
      accel: accelInput.value || "0",
    });
    const message = document.getElementById("lc-calibration-message");
    let result;
    try {
      result = await api(
        "GET",
        "/api/pasting/loading/calibration?" + params.toString()
      );
    } catch (err) {
      message.textContent = err.message;
      return;
    }
    message.textContent = "";
    computed = {
      rpu: result.rotations_per_ul,
      rate: result.max_dispense_rate,
      accel: result.dispense_accel,
    };
    setOutput("lc-volume-ul", result.volume_ul);
    setOutput("lc-rotations-per-ul", result.rotations_per_ul);
    setOutput("lc-dispense-rate", result.max_dispense_rate);
    setOutput("lc-dispense-accel", result.dispense_accel);
    updateApplyButtons();
  }

  function updateApplyButtons() {
    setDisabled("lc-apply-rotations-per-ul", !(computed.rpu > 0));
    setDisabled("lc-apply-dispense-rate", !(computed.rate > 0));
    setDisabled("lc-apply-dispense-accel", !(computed.accel > 0));
    setDisabled(
      "lc-apply-all",
      !(computed.rpu > 0 && computed.rate > 0 && computed.accel > 0)
    );
  }

  function setDisabled(id, disabled) {
    const button = document.getElementById(id);
    if (button) button.disabled = disabled;
  }

  function setOutput(id, value) {
    const output = document.getElementById(id);
    if (!output) return;
    output.value = value === null || value === undefined ? "-" : value.toFixed(6);
    output.textContent = output.value;
  }

  async function applyValues(valuesObject, buttonId) {
    const values = {};
    for (const [key, raw] of Object.entries(valuesObject)) {
      if (!(raw > 0)) return;
      values[key] = Number(raw.toFixed(6));
    }
    setDisabled(buttonId, true);
    try {
      await api("PUT", "/api/settings/machine", { values });
      const labels = [];
      for (const [key, value] of Object.entries(values)) {
        const output = document.getElementById(CURRENT_OUTPUT_FOR[key]);
        if (output) output.textContent = value.toFixed(6);
        labels.push(`${key} を ${value.toFixed(6)}`);
      }
      toast(`${labels.join(", ")} に設定しました`);
    } catch (err) {
      toast(err.message, false);
    } finally {
      updateApplyButtons();
    }
  }
})();
