"use strict";

// 吐出量キャリブの実行中パラメータ編集（dispense_calibration のページで読む）。
// data-runtime-editable な欄を編集すると PUT /api/jobs/current/params で
// 実行中ジョブへ即反映する（保存は pendingSaves を通す）。
// このフォームのジョブがアクティブ（非終端 かつ accepts_commands）な間だけ送る。
// 切替時は送信待ちを破棄し、送信した要求にも対象 ID を付ける。未実行時は何もしない
// （次回の既定値はジョブフォームの即保存が受け持つ）。
// 検証・ドメインロジックはサーバ（runtime_editable / 正値 / 型 / 負 offset）が持つ。
// ここは空欄スキップと Number.isFinite のパース可否だけを見て、サーバの 400 を toast する。

(() => {
  const { api, jobs, pendingSaves } = window.webui;
  const form = document.getElementById("job-form");
  if (!form || !jobs) return;

  const inputs = [...form.querySelectorAll('input[data-runtime-editable="true"]')];
  if (inputs.length === 0) return;

  const SAVE_DELAY_MS = 400;
  const keyOf = (input) => `runtime-params:${input.name}`;

  let activeJobId = null;

  function update(job) {
    const nextId = jobs.commandReady(job, { name: form.dataset.jobName })
      ? job.id : null;
    if (nextId !== activeJobId) {
      for (const input of inputs) pendingSaves.cancel(keyOf(input));
    }
    activeJobId = nextId;
  }

  jobs.onUpdate(update);
  update(jobs.currentJob());

  function save(input, expectedJobId) {
    if (input.value === "" || activeJobId !== expectedJobId) return;
    const value = Number(input.value);
    if (!Number.isFinite(value)) return;
    pendingSaves.run("実行中の変更を反映できません", () =>
      api("PUT", "/api/jobs/current/params", {
        values: { [input.name]: value },
        persist: true,
        expected_job_id: expectedJobId,
      })
    );
  }

  for (const input of inputs) {
    input.addEventListener("input", () => {
      const expectedJobId = activeJobId;
      if (expectedJobId === null) return;
      pendingSaves.schedule(keyOf(input), () => save(input, expectedJobId), SAVE_DELAY_MS);
    });
  }
})();
