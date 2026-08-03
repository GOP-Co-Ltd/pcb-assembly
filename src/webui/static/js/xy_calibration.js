"use strict";

(() => {
  const recordButton = document.getElementById("xyc-record");
  const quitButton = document.getElementById("xyc-quit");
  if (!recordButton || !quitButton || !window.webui.jobs) return;

  const { jobs } = window.webui;
  function update(job) {
    const active = jobs.commandReady(job, { name: "xy_calibration" });
    recordButton.disabled = !active;
    quitButton.disabled = !active;
  }
  recordButton.addEventListener("click", () =>
    jobs.sendCommandOrToast({ type: "record" }, null)
  );
  quitButton.addEventListener("click", () =>
    jobs.sendCommandOrToast({ type: "quit" }, null)
  );
  jobs.onUpdate(update);
  update(jobs.currentJob());
})();
