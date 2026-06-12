"use strict";

// Reference Point Setup: Record / Quit を実行中ジョブの WS command として送る。
// ジョブが実行中（accepts_commands）のときのみボタンを有効化する。

(() => {
  const { toast } = window.webui;
  const recordButton = document.getElementById("rps-record");
  const quitButton = document.getElementById("rps-quit");
  if (!recordButton || !quitButton || !window.webui.jobs) return;

  const TERMINAL = new Set(["succeeded", "failed", "aborted"]);

  function update(job) {
    const active =
      job !== null &&
      job !== undefined &&
      job.name === "reference_point_setup" &&
      !TERMINAL.has(job.status) &&
      job.accepts_commands;
    recordButton.disabled = !active;
    quitButton.disabled = !active;
  }

  function sendCommand(command) {
    if (!window.webui.jobs.sendCommand(command)) {
      toast("WebSocket 未接続のため送信できません", false);
    }
  }

  recordButton.addEventListener("click", () => sendCommand({ type: "record" }));
  quitButton.addEventListener("click", () => sendCommand({ type: "quit" }));

  window.webui.jobs.onUpdate(update);
  update(window.webui.jobs.currentJob());
})();
