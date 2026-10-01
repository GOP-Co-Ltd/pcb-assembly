"use strict";

// 吐出量キャリブレーションのメニュー操作パネル。メニュー段階の対話ジョブへ
// {type:"run_calib", which:...} コマンドを WS で送る。
// 実行中ジョブが accepts_commands で、かつ progress_stage が data-calib-stage と
// 一致するときに有効にする（loading_controls.js の update パターン）。
// 算出/判定は pcbasm 側で行い、ここは command 送信とボタン有効化だけを持つ。

(() => {
  const panel = document.getElementById("calibration-menu");
  if (!panel || !window.webui.jobs) return;

  const { jobs } = window.webui;
  const stages = new Set([panel.dataset.calibStage]);
  const buttons = [...panel.querySelectorAll("[data-calib]")];
  for (const button of buttons) {
    button.addEventListener("click", () =>
      jobs.sendCommandOrToast({ type: "run_calib", which: button.dataset.calib })
    );
  }

  function update(job) {
    const enabled = jobs.commandReady(job, { stages, name: panel.dataset.jobName });
    for (const button of buttons) button.disabled = !enabled;
  }

  jobs.onUpdate(update);
  update(jobs.currentJob());
})();
