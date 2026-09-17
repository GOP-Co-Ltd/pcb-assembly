"use strict";

// グローバル WS クライアント（/api/ws）+ ジョブコンソール UI。
// tab.html から全タブで読み込まれ、window.webui.jobs を公開する。
// ページに #job-console（data-job-names）があればコンソールを描画する。

(() => {
  const { toast, api, createBackoff, withBase } = window.webui;

  const TERMINAL = new Set(["succeeded", "failed", "aborted"]);
  const STATUS_LABELS = {
    idle: "待機中",
    pending: "開始待ち",
    running: "実行中",
    waiting_input: "入力待ち",
    succeeded: "成功",
    failed: "失敗",
    aborted: "中止",
  };

  const listeners = new Set();
  const beforeStart = new Set();
  let startPending = false;
  const originalTitle = document.title;
  const completionNotice = document.getElementById("job-completion-notice");
  const completionMessage = document.getElementById("job-completion-message");
  let socket = null;
  const reconnectBackoff = createBackoff(1000, 15000);
  let currentJob = null;
  let jobStatusVersion = 0;
  let abortRequestedJobId = null;
  let completionJobId = null;

  function isActive(job) {
    return job !== null && job !== undefined && !TERMINAL.has(job.status);
  }

  // 「対話コマンドを受け付けるジョブか」の共通述語。
  // stages: progress_stage がいずれかに一致する場合のみ / name: ジョブ名の限定。
  function commandReady(job, { stages = null, name = null } = {}) {
    return (
      isActive(job) &&
      job.accepts_commands &&
      (stages === null || stages.has(job.progress_stage)) &&
      (name === null || job.name === name)
    );
  }

  function send(message) {
    if (socket === null || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(message));
    return true;
  }

  function sendCommand(command) {
    if (!commandReady(currentJob)) return false;
    return send({ type: "command", command, expected_job_id: currentJob.id });
  }

  // command を送り、結果をトーストで通知する（successMessage=null で成功時無音）。
  function sendCommandOrToast(command, successMessage = "コマンドを送信しました") {
    if (sendCommand(command)) {
      if (successMessage !== null) toast(successMessage);
    } else {
      toast(
        commandReady(currentJob)
          ? "WebSocket 未接続のため送信できません"
          : "コマンドを受け付けるジョブが実行中ではありません",
        false
      );
    }
  }

  function dismissCompletionNotice() {
    if (completionNotice === null || completionMessage === null) return;
    completionNotice.hidden = true;
    delete completionNotice.dataset.status;
    completionMessage.textContent = "";
    document.title = originalTitle;
  }

  function showCompletionNotice(job) {
    if (completionNotice === null || completionMessage === null) return;
    const succeeded = job.status === "succeeded";
    completionNotice.dataset.status = succeeded ? "success" : "error";
    completionMessage.textContent = succeeded
      ? `${job.label}が完了しました`
      : `${job.label}に失敗しました: ${job.error || "不明なエラー"}`;
    completionNotice.hidden = false;
    document.title = `${succeeded ? "【成功】" : "【失敗】"}${originalTitle}`;
  }

  function notifyIfCompleted(job) {
    if (
      completionJobId === null ||
      job === null ||
      job === undefined ||
      job.id !== completionJobId ||
      !TERMINAL.has(job.status)
    ) {
      return;
    }
    completionJobId = null;
    if (job.status === "aborted") return;
    showCompletionNotice(job);
  }

  function armCompletionNotification(job) {
    if (!job?.notify_on_completion) return;
    completionJobId = job.id;
    // POST より先に終端 job_status が届く高速ジョブも取りこぼさない。
    notifyIfCompleted(currentJob);
  }

  // ---- 公開 API ----

  window.webui.jobs = {
    currentJob: () => currentJob,
    onUpdate: (callback) => listeners.add(callback),
    beforeStart: (callback) => beforeStart.add(callback),
    isActive,
    commandReady,
    sendCommand,
    sendCommandOrToast,
    abort: async () => {
      const job = currentJob;
      if (!isActive(job) || abortRequestedJobId === job.id) return;
      abortRequestedJobId = job.id;
      renderConsole();
      try {
        await api("POST", "/api/jobs/current/abort");
        toast("中止要求を送信しました");
      } catch (err) {
        abortRequestedJobId = null;
        renderConsole();
        toast(err.message, false);
      }
    },
  };

  // ---- WS 接続（指数バックオフ再接続 + 再同期）----

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const connection = new WebSocket(`${proto}://${location.host}${withBase("/api/ws")}`);
    socket = connection;
    connection.addEventListener("open", async () => {
      reconnectBackoff.reset();
      // 切断中の control_changed は届いていないので操作権も取り直す
      window.webui.control?.refresh();
      const version = jobStatusVersion;
      try {
        const data = await api("GET", "/api/jobs/current");
        // 同期中に届いた WS の全量通知や、次の接続の状態を古い HTTP 応答で戻さない。
        if (socket === connection && connection.readyState === WebSocket.OPEN
            && jobStatusVersion === version) {
          applyJob(data.job);
        }
      } catch {
        /* 再接続時に再試行される */
      }
    });
    connection.addEventListener("message", (event) => {
      if (socket === connection) handleEvent(JSON.parse(event.data));
    });
    connection.addEventListener("close", () => {
      if (socket !== connection) return;
      socket = null;
      setTimeout(connect, reconnectBackoff.next());
    });
  }

  function handleEvent(event) {
    switch (event.type) {
      case "job_status":
        jobStatusVersion += 1;
        applyJob(event.job);
        break;
      case "log":
        if (ownsEvent(event)) appendLog(event.line);
        break;
      case "progress":
        // currentJob へ反映し listeners へ通知する（loading_controls 等の状態追従用）
        if (currentJob && event.job_id === currentJob.id) {
          currentJob.progress_stage = event.stage;
          currentJob.progress_percent = event.percent;
          for (const callback of listeners) callback(currentJob);
        }
        if (ownsEvent(event)) renderProgress(event.stage, event.percent);
        break;
      case "prompt":
        if (ownsEvent(event)) openPrompt(event.prompt);
        break;
      case "prompt_resolved":
        if (ownsEvent(event) && activePrompt?.id === event.prompt_id) closePrompt();
        break;
      case "state_changed":
        refreshHeader();
        break;
      case "control_changed":
        // 保持者が変わったことは全 subscriber へ同一 payload で届く。「自分か」の
        // 判定は control.js が you.key と比べて行う
        window.webui.control?.applyControl(event.control);
        break;
      case "error":
        toast(event.detail, false);
        break;
    }
  }

  async function refreshHeader() {
    try {
      const state = await api("GET", "/api/state");
      const chip = document.getElementById("pcb-chip");
      if (chip) chip.textContent = state.pcb_file || "PCB未選択";
    } catch {
      /* 表示更新のみなので無視 */
    }
  }

  // ---- ジョブ状態の反映 ----

  function applyJob(job) {
    currentJob = job ?? null;
    if (!isActive(currentJob) || currentJob.id !== abortRequestedJobId) {
      abortRequestedJobId = null;
    }
    for (const callback of listeners) callback(currentJob);
    renderConsole();
    notifyIfCompleted(currentJob);
  }

  // ---- コンソール描画（#job-console があるページのみ）----

  const consoleEl = document.getElementById("job-console");
  const pageJobNames = new Set(
    (consoleEl ? consoleEl.dataset.jobNames || "" : "").split(/\s+/).filter(Boolean)
  );
  const forms = Array.from(document.querySelectorAll("form.job-form[data-job-name]"));

  function ownsJob(job) {
    return (
      consoleEl !== null &&
      job !== null &&
      job !== undefined &&
      pageJobNames.has(job.name)
    );
  }

  function ownsEvent(event) {
    return ownsJob(currentJob) && event.job_id === currentJob.id;
  }

  function el(id) {
    return document.getElementById(id);
  }

  // "/artifacts/..." パスをリンク化したログ 1 行分のノード列を作る
  // （テキストはテキストノードとして追加されるためエスケープ維持）
  function logLineNodes(line) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const match of line.matchAll(/\/artifacts\/\S+/g)) {
      fragment.append(line.slice(last, match.index));
      const link = document.createElement("a");
      // ログ本文の成果物パスは backend 相対（"/artifacts/...")。表示は原文のまま
      link.href = withBase(match[0]);
      link.target = "_blank";
      link.textContent = match[0];
      fragment.append(link);
      last = match.index + match[0].length;
    }
    fragment.append(line.slice(last));
    return fragment;
  }

  function renderLogLines(lines) {
    const log = el("jc-log");
    log.replaceChildren();
    lines.forEach((line, index) => {
      if (index > 0) log.append("\n");
      log.append(logLineNodes(line));
    });
    log.scrollTop = log.scrollHeight;
  }

  function appendLog(line) {
    const log = el("jc-log");
    if (log.textContent) log.append("\n");
    log.append(logLineNodes(line));
    log.scrollTop = log.scrollHeight;
  }

  function renderProgress(stage, percent) {
    const hasPercent = percent !== null && percent !== undefined;
    el("jc-progress-bar").style.width = hasPercent ? `${percent}%` : "0%";
    el("jc-progress-text").textContent = hasPercent
      ? `${stage}（${Math.round(percent)}%）`
      : stage || "";
  }

  function renderStartButtons() {
    // 保存・開始要求の待機中も、重ねて開始要求を送らない。
    for (const jobForm of forms) {
      const submit = jobForm.querySelector("button[type='submit']");
      if (submit) submit.disabled = startPending || isActive(currentJob);
    }
  }

  function renderConsole() {
    if (!consoleEl) return;
    const job = currentJob;
    renderStartButtons();

    if (!ownsJob(job)) {
      el("jc-status").textContent = STATUS_LABELS.idle;
      el("jc-status").dataset.status = "idle";
      el("jc-abort").disabled = true;
      el("jc-abort").textContent = "中止";
      el("jc-result").hidden = true;
      closePrompt();
      return;
    }

    el("jc-status").textContent = STATUS_LABELS[job.status] || job.status;
    el("jc-status").dataset.status = job.status;
    const abortRequested = abortRequestedJobId === job.id;
    el("jc-abort").disabled = !isActive(job) || abortRequested;
    el("jc-abort").textContent = abortRequested ? "中止要求中" : "中止";

    // job_status はログ全量を持つため毎回同期する（再接続にも追従）
    const log = el("jc-log");
    const text = job.log_tail.join("\n");
    if (log.textContent !== text) {
      renderLogLines(job.log_tail);
    }
    renderProgress(job.progress_stage, job.progress_percent);

    if (job.pending_prompt) {
      openPrompt(job.pending_prompt);
    } else {
      closePrompt();
    }

    renderResult(job);
  }

  function renderResult(job) {
    const resultEl = el("jc-result");
    if (!TERMINAL.has(job.status)) {
      resultEl.hidden = true;
      return;
    }
    resultEl.hidden = false;
    const summary = el("jc-summary");
    if (job.status === "failed") {
      summary.textContent = `エラー: ${job.error || "不明"}`;
    } else if (job.status === "aborted") {
      summary.textContent = "中止しました";
    } else {
      summary.textContent = job.result?.summary || "完了";
    }

    const artifactsEl = el("jc-artifacts");
    artifactsEl.replaceChildren();
    for (const artifact of job.result?.artifacts || []) {
      // artifact.url は backend 相対（"/artifacts/...")。機体 prefix はここで 1 回付ける
      const url = withBase(artifact.url);
      if (artifact.kind === "image") {
        const figure = document.createElement("figure");
        const img = document.createElement("img");
        img.src = url;
        img.alt = artifact.label;
        img.className = "jc-artifact-image";
        const caption = document.createElement("figcaption");
        const link = document.createElement("a");
        link.href = url;
        link.download = "";
        link.textContent = `${artifact.label}（ダウンロード）`;
        caption.appendChild(link);
        figure.append(img, caption);
        artifactsEl.appendChild(figure);
      } else {
        const link = document.createElement("a");
        link.href = url;
        link.download = "";
        link.className = "jc-artifact-file";
        link.textContent = artifact.label;
        artifactsEl.appendChild(link);
      }
    }

    const applyEl = el("jc-apply");
    if (job.apply_available && job.result?.apply) {
      applyEl.hidden = false;
      el("jc-apply-label").textContent = job.result.apply.label;
    } else {
      applyEl.hidden = true;
    }
  }

  // ---- プロンプトモーダル ----

  let activePrompt = null;

  function configurePromptButtons(prompt) {
    const noButton = el("jc-prompt-no");
    const okButton = el("jc-prompt-ok");
    const isConfirm = prompt.kind === "confirm";
    // confirm は常に 2 ボタン。number は false_label があるときだけ中止ボタンを出す。
    const cancelable = prompt.kind === "number" && prompt.false_label != null;
    noButton.hidden = !(isConfirm || cancelable);
    noButton.textContent = prompt.false_label ?? "いいえ";
    okButton.textContent = isConfirm ? (prompt.true_label ?? "はい") : "OK";
  }

  function renderPromptField(prompt) {
    const field = el("jc-prompt-field");
    field.replaceChildren();
    if (prompt.kind === "number" || prompt.kind === "text") {
      const input = document.createElement("input");
      input.type = prompt.kind;
      if (prompt.kind === "number") {
        input.step = "any";
        input.required = true;
      }
      input.id = "jc-prompt-input";
      input.setAttribute("aria-labelledby", "jc-prompt-message");
      if (prompt.default !== null && prompt.default !== undefined) input.value = prompt.default;
      field.appendChild(input);
    } else if (prompt.kind === "choice") {
      const select = document.createElement("select");
      select.id = "jc-prompt-input";
      select.setAttribute("aria-labelledby", "jc-prompt-message");
      for (const choice of prompt.choices) {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice;
        option.selected = choice === prompt.default;
        select.appendChild(option);
      }
      field.appendChild(select);
    }
  }

  function answerFromPromptSubmit(prompt, event) {
    if (prompt.kind === "confirm") {
      return event.submitter?.id !== "jc-prompt-no";
    }
    if (prompt.kind === "number") {
      // 中止ボタン（false_label 付き number のみ表示）は false を返す。
      if (event.submitter?.id === "jc-prompt-no") return false;
      return Number(el("jc-prompt-input").value);
    }
    return el("jc-prompt-input").value;
  }

  function openPrompt(prompt) {
    const dialog = el("jc-prompt");
    if (activePrompt && activePrompt.id === prompt.id && dialog.open) return;
    activePrompt = prompt;
    el("jc-prompt-message").textContent = prompt.message;
    configurePromptButtons(prompt);
    renderPromptField(prompt);
    if (!dialog.open) {
      // showModal() にしない。::backdrop が緊急停止（#estop）と中止（#jc-abort）を
      // 覆うため、応答権を持たない閲覧者は「応答できない・中止もできない」で詰む。
      // 保持者も応答待ちの間だけ緊急停止を押せなくなる（安全機能なので塞がない）。
      // 中央寄せは CSS の .jc-prompt[open] が持つ。
      dialog.show();
      // 非モーダルは自動でフォーカスが移らないので、Enter の暗黙送信のために自前で移す
      (el("jc-prompt-input") ?? el("jc-prompt-ok")).focus();
    }
  }

  function closePrompt() {
    activePrompt = null;
    const dialog = el("jc-prompt");
    if (dialog && dialog.open) dialog.close();
  }

  if (consoleEl) {
    el("jc-prompt-form").addEventListener("submit", (event) => {
      // 回答が拒否されたり未送信だった場合も、入力待ちと編集内容を残す。
      // 閉じるのはサーバーの prompt_resolved / job_status を受けたとき。
      event.preventDefault();
      const prompt = activePrompt;
      if (!prompt) return;
      const answer = answerFromPromptSubmit(prompt, event);
      if (!send({ type: "respond_prompt", prompt_id: prompt.id, answer })) {
        toast("WebSocket 未接続のため回答を送信できません", false);
      }
    });

    el("jc-abort").addEventListener("click", () => {
      window.webui.jobs.abort();
    });

    el("jc-apply-btn").addEventListener("click", async () => {
      if (!ownsJob(currentJob) || !currentJob.apply_available) return;
      const expectedJobId = currentJob?.id;
      try {
        const data = await api("POST", "/api/jobs/last/apply", {
          expected_job_id: expectedJobId,
        });
        toast(`設定に反映しました: ${JSON.stringify(data.applied)}`);
        if (currentJob?.id === expectedJobId) el("jc-apply").hidden = true;
      } catch (err) {
        toast(err.message, false);
      }
    });

    el("jc-discard-btn").addEventListener("click", async () => {
      if (!ownsJob(currentJob) || !currentJob.apply_available) return;
      const expectedJobId = currentJob?.id;
      try {
        await api("POST", "/api/jobs/last/discard", {
          expected_job_id: expectedJobId,
        });
        toast("計測結果を破棄しました");
        if (currentJob?.id === expectedJobId) el("jc-apply").hidden = true;
      } catch (err) {
        toast(err.message, false);
      }
    });
  }

  const completionDismiss = document.getElementById("job-completion-dismiss");
  if (completionDismiss) {
    completionDismiss.addEventListener("click", dismissCompletionNotice);
  }

  // ---- ジョブ開始フォーム ----

  for (const jobForm of forms) {
    jobForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (startPending || isActive(currentJob)) return;
      startPending = true;
      renderStartButtons();
      completionJobId = null;
      dismissCompletionNotice();
      const params = {};
      for (const input of jobForm.querySelectorAll("[data-param-type]")) {
        const type = input.dataset.paramType;
        if (input.dataset.paramOptional === "true" && input.value === "") {
          continue;
        }
        if (type === "bool") {
          params[input.name] = input.checked;
        } else if (type === "float") {
          params[input.name] = Number(input.value);
        } else if (type === "int") {
          params[input.name] = parseInt(input.value, 10);
        } else {
          params[input.name] = input.value;
        }
      }
      try {
        for (const prepare of beforeStart) await prepare();
        el("jc-log").textContent = "";
        el("jc-result").hidden = true;
        renderProgress("", null);
        // 状態は WS の job_status を単一の真実とする。start() が開始時に即 publish し、
        // _send_loop は送信時に最新状態を再構築するため、POST 応答（開始時点で古く
        // なり得るスナップショット）は state には使わない（成功確定とエラー通知のみ）。
        const data = await api("POST", `/api/jobs/${jobForm.dataset.jobName}`, {
          params,
        });
        armCompletionNotification(data.job);
      } catch (err) {
        toast(err.message, false);
      } finally {
        startPending = false;
        renderStartButtons();
      }
    });
  }

  connect();
})();
