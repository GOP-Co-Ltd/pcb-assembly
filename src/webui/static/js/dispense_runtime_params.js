"use strict";

// 吐出量キャリブの実行中パラメータ編集: data-runtime-editable な input を編集すると
// PUT /api/jobs/current/params で実行中ジョブへ即反映する（debounce でまとめ送り）。
// ジョブがアクティブ（非終端 かつ accepts_commands）な間だけ送る。未実行時は何もしない
// （従来どおり起動時の persisted_params 経路に任せる）。
// 検証・ドメインロジックはサーバ（runtime_editable / 正値 / 型 / 負 offset）が持つ。
// ここは空欄スキップと Number.isFinite のパース可否だけを見て、サーバの 400 を toast する。

(() => {
  const { toast, api } = window.webui;
  const form = document.getElementById("job-form");
  if (!form || !form.classList.contains("dispense-calibration-form")) return;
  if (!window.webui.jobs) return;

  const inputs = [...form.querySelectorAll('input[data-runtime-editable="true"]')];
  if (inputs.length === 0) return;

  const TERMINAL = new Set(["succeeded", "failed", "aborted"]);
  const SAVE_DELAY_MS = 400;

  let active = false;
  const pending = new Map();
  let saveTimer = null;

  function update(job) {
    active = job != null && !TERMINAL.has(job.status) && job.accepts_commands;
  }

  window.webui.jobs.onUpdate(update);
  update(window.webui.jobs.currentJob());

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, SAVE_DELAY_MS);
  }

  async function flush() {
    // 実行中（アクティブ）でなければ送らずに破棄する。未実行時のフォーム既定は
    // 起動時に送られるので、ここでの編集をライブ反映するのは実行中だけでよい。
    if (!active) {
      pending.clear();
      return;
    }
    const values = {};
    for (const [name, raw] of pending) {
      if (raw === "") continue;
      const value = Number(raw);
      if (Number.isFinite(value)) values[name] = value;
    }
    pending.clear();
    if (Object.keys(values).length === 0) return;
    try {
      await api("PUT", "/api/jobs/current/params", { values, persist: true });
    } catch (err) {
      // サーバの 400（固定キー / 負値 / 型不一致）を表示する。
      toast(err.message, false);
    }
  }

  for (const input of inputs) {
    input.addEventListener("input", () => {
      pending.set(input.name, input.value);
      scheduleSave();
    });
  }
})();
