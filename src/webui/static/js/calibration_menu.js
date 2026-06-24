"use strict";

// 吐出量キャリブレーションのメニュー操作パネル: メニュー段階の対話ジョブへ
// {type:"run_calib", which:...} コマンドを WS で送る。
// 有効化条件: 実行中ジョブが accepts_commands かつ
// progress_stage が data-calib-stage と一致（loading_controls.js の update パターン）。
// 算出/判定は pcbasm 側。ここは command 送信とボタン有効化のみ。

(() => {
  const { toast } = window.webui;
  const panel = document.getElementById("calibration-menu");
  if (!panel || !window.webui.jobs) return;

  const TERMINAL = new Set(["succeeded", "failed", "aborted"]);
  const calibStage = panel.dataset.calibStage;
  const buttons = [...panel.querySelectorAll("[data-calib]")];
  for (const button of buttons) {
    button.addEventListener("click", () => sendRunCalib(button.dataset.calib));
  }

  function update(job) {
    const enabled =
      job != null &&
      !TERMINAL.has(job.status) &&
      job.accepts_commands &&
      job.progress_stage === calibStage;
    for (const button of buttons) button.disabled = !enabled;
  }

  window.webui.jobs.onUpdate(update);
  update(window.webui.jobs.currentJob());

  function sendRunCalib(which) {
    if (window.webui.jobs.sendCommand({ type: "run_calib", which })) {
      toast("コマンドを送信しました");
    } else {
      toast("WebSocket 未接続のため送信できません", false);
    }
  }
})();
</content>
