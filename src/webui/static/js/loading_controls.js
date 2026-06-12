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
  const buttons = ["lc-extrude", "lc-suck", "lc-finish"].map((id) =>
    document.getElementById(id)
  );

  function update(job) {
    const enabled =
      job !== null &&
      job !== undefined &&
      !TERMINAL.has(job.status) &&
      job.accepts_commands &&
      job.progress_stage === loadingStage;
    for (const button of buttons) button.disabled = !enabled;
  }

  window.webui.jobs.onUpdate(update);
  update(window.webui.jobs.currentJob());

  function sendAction(type) {
    const command = { type };
    if (type !== "finish") {
      const amount = Number(amountInput.value);
      if (!(amount > 0)) {
        toast("量には正の数値を入力してください", false);
        return;
      }
      command.amount = amount;
    }
    if (window.webui.jobs.sendCommand(command)) {
      toast("コマンドを送信しました");
    } else {
      toast("WebSocket 未接続のため送信できません", false);
    }
  }

  document.getElementById("lc-extrude").addEventListener("click", () => sendAction("extrude"));
  document.getElementById("lc-suck").addEventListener("click", () => sendAction("suck"));
  document.getElementById("lc-finish").addEventListener("click", () => sendAction("finish"));
})();
