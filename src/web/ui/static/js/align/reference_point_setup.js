"use strict";

// Reference Point Setup ページ。Record / Quit を実行中ジョブの WS command として送る。
// ジョブが実行中（accepts_commands）のときのみボタンを有効化する。

(() => {
  const recordButton = document.getElementById("rps-record");
  const quitButton = document.getElementById("rps-quit");
  if (!recordButton || !quitButton || !window.webui.jobs) return;

  const { jobs } = window.webui;

  function update(job) {
    const active = jobs.commandReady(job, { name: "reference_point_setup" });
    recordButton.disabled = !active;
    quitButton.disabled = !active;
  }

  // 成功トーストは出さない（現行挙動の維持）
  recordButton.addEventListener("click", () =>
    jobs.sendCommandOrToast({ type: "record" }, null)
  );
  quitButton.addEventListener("click", () =>
    jobs.sendCommandOrToast({ type: "quit" }, null)
  );

  jobs.onUpdate(update);
  update(jobs.currentJob());
})();
