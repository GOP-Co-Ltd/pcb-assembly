"use strict";

// ローディング操作パネル。ローディング段階の対話ジョブへ
// extrude / suck / finish コマンドを WS で送る。
// 実行中ジョブが accepts_commands で、かつ progress_stage が
// data-loading-stage（カンマ区切りの複数可）のいずれかと一致するときに有効にする。

(() => {
  const { toast, api, debounce, jobs } = window.webui;
  const panel = document.getElementById("loading-controls");
  if (!panel || !jobs) return;

  // data-loading-stage はカンマ区切りで複数 stage を許す（吐出量キャリブはメニュー段階の
  // プライムと ① 専用ローディング段階の両方でボタンを有効化する）。
  const loadingStages = new Set(
    (panel.dataset.loadingStage || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
  );
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
  let computed = null;
  let calibrationVersion = 0;
  const scheduleFetch = debounce(fetchCalibration, CALIBRATION_DELAY_MS);
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
    const enabled = jobs.commandReady(job, {
      stages: loadingStages, name: panel.dataset.jobName,
    });
    for (const button of buttons) button.disabled = !enabled;
  }

  jobs.onUpdate(update);
  update(jobs.currentJob());
  bindLoadingParamSync();
  bindMassCalibration();

  // 値の検証はサーバ（parse_loading_command）だけで行う。不正値は
  // InvalidLoadingCommand としてジョブコンソールのログに理由が出る。
  function sendAction(type) {
    const command = { type };
    if (type === "extrude" || type === "suck") {
      command.amount = Number(amountInput.value);
    } else if (type === "extrude_rotations" || type === "suck_rotations") {
      command.rotations = Number(rotationsInput.value);
      command.rate = Number(rateInput.value);
      command.accel = Number(accelInput.value);
      if (type === "extrude_rotations") {
        // 押出と引き戻しを 1 セットで送る
        command.retract_rotations = Number(retractRotationsInput.value);
      }
    }
    jobs.sendCommandOrToast(command);
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
    function sync() {
      for (const [name, source] of bindings) {
        const target = form.querySelector(`[name="${name}"]`);
        if (target && source) target.value = source.value;
      }
    }
    // 「実行」を待たず、入力するそばから次回フォーム既定値として保存する
    // （質量キャリブのブートストラップ等、ジョブ未実行でもリロードで復元される）。
    const persistDefaults = debounce(() => {
      if (!jobName) return;
      const values = {};
      for (const [name, source] of bindings) {
        if (!source || source.value === "") continue;
        const value = Number(source.value);
        if (Number.isFinite(value)) values[name] = value;
      }
      api("POST", `/api/jobs/${jobName}/param-defaults`, { values }).catch(
        () => {}
      );
    }, PARAM_SAVE_DELAY_MS);
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
    const invalidate = () => {
      // debounce 中も古い値を適用させない。応答はこの入力版と一致するときだけ使う。
      calibrationVersion += 1;
      renderCalibration(null);
      document.getElementById("lc-calibration-message").textContent = "";
      scheduleFetch();
    };
    for (const input of [massInput, rotationsInput, rateInput, accelInput]) {
      input.addEventListener("input", invalidate);
    }
    bindApply("lc-apply-rotations-per-ul", () => ({
      "paste_dispenser.rotations_per_ul": computed?.rotations_per_ul,
    }));
    bindApply("lc-apply-dispense-rate", () => ({
      "paste_dispenser.max_dispense_rate": computed?.max_dispense_rate,
    }));
    bindApply("lc-apply-dispense-accel", () => ({
      "paste_dispenser.dispense_accel": computed?.dispense_accel,
    }));
    bindApply("lc-apply-all", () => ({
      "paste_dispenser.rotations_per_ul": computed?.rotations_per_ul,
      "paste_dispenser.max_dispense_rate": computed?.max_dispense_rate,
      "paste_dispenser.dispense_accel": computed?.dispense_accel,
    }));
    invalidate();
  }

  function bindApply(buttonId, valuesFor) {
    const button = document.getElementById(buttonId);
    if (!button) return;
    button.addEventListener("click", () => applyValues(valuesFor(), buttonId));
  }

  async function fetchCalibration() {
    const version = calibrationVersion;
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
      if (version !== calibrationVersion) return;
      message.textContent = err.message;
      return;
    }
    if (version !== calibrationVersion) return;
    message.textContent = "";
    renderCalibration(result);
  }

  function renderCalibration(result) {
    computed = result;
    setOutput("lc-volume-ul", result?.volume_ul);
    setOutput("lc-rotations-per-ul", result?.rotations_per_ul);
    setOutput("lc-dispense-rate", result?.max_dispense_rate);
    setOutput("lc-dispense-accel", result?.dispense_accel);
    updateApplyButtons();
  }

  // サーバ（MassFlowEstimate.estimate）が算出できない値を null で返す契約に依存する。
  function updateApplyButtons() {
    setDisabled("lc-apply-rotations-per-ul", computed?.rotations_per_ul == null);
    setDisabled("lc-apply-dispense-rate", computed?.max_dispense_rate == null);
    setDisabled("lc-apply-dispense-accel", computed?.dispense_accel == null);
    setDisabled(
      "lc-apply-all",
      computed?.rotations_per_ul == null ||
        computed?.max_dispense_rate == null ||
        computed?.dispense_accel == null
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
      if (raw == null) return;
      // サーバの算出値（丸め済み）をそのまま送る
      values[key] = raw;
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
