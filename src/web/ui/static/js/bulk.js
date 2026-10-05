"use strict";

// 一括管理ページの行操作。行（[data-row-base]）ごとに、その機体の backend
// （"/m/<machine_id>/api/..."）を api() で叩く。このページは machine prefix が空なので、
// api() へは機体 prefix 付きのパスをそのまま渡す。
//
// 操作権は機体ごとに別。行の「取得」「解放」はその機体のリースだけを動かし、
// 更新・塗布実行はその行の操作権を保持しているときだけ押せる。
// 表示する値（更新の要約・PCB 名・保持者名）はサーバの応答をそのまま流す。

(() => {
  const { toast, api } = window.webui;

  // 他の端末による取得/解放を拾う間隔
  const POLL_MS = 5000;
  // 塗布実行で開始するジョブ。パラメータは送らない（ジョブ定義の既定値で動く）
  const PASTE_JOB = "paste_solder";

  function setupRow(row) {
    const base = row.dataset.rowBase;
    const label = row.dataset.label;
    const part = (id) => row.querySelector(`[data-testid="${id}"]`);
    const holderEl = part("bulk-holder");
    const acquireButton = part("bulk-acquire");
    const releaseButton = part("bulk-release");
    const updateSummary = part("bulk-update-summary");
    const updateButton = part("bulk-update");
    // ペーストの行にだけある
    const pcbEl = part("bulk-pcb");
    const runButton = part("bulk-paste-run");

    // "held" | "viewer" | "free" | "unknown"（control.js と同じ区分）
    let state = "unknown";
    let holderName = "";
    let myKey = null;
    let pcbFile = null;
    // 要求の送信中はその行のボタンを押せなくする（二重送信を防ぐ）
    let pending = false;

    function applySnapshot(data) {
      if (data.you?.key) myKey = data.you.key;
      const control = data.control ?? null;
      if (!control) {
        state = "unknown";
      } else if (!control.held) {
        state = "free";
      } else {
        state = control.key === myKey ? "held" : "viewer";
      }
      holderName = control?.display_name || "他の端末";
      if ("pcb_file" in data) pcbFile = data.pcb_file;
      render();
    }

    function render() {
      holderEl.textContent = {
        held: "あなた",
        free: "空き",
        viewer: `${holderName} が保持中`,
        unknown: "接続できません",
      }[state];
      acquireButton.hidden = state === "held" || state === "unknown";
      acquireButton.disabled = pending || state === "viewer";
      releaseButton.hidden = state !== "held";
      releaseButton.disabled = pending;
      const blocked = pending || state !== "held";
      updateButton.disabled = blocked;
      if (pcbEl) pcbEl.textContent = pcbFile ?? "PCB未選択";
      if (runButton) runButton.disabled = blocked;
    }

    async function refresh() {
      try {
        applySnapshot(await api("GET", `${base}/api/state`));
      } catch {
        // 不通の機体は操作不可のまま表示だけ変える（ポーリングのたびにトーストを出さない）
        state = "unknown";
        render();
      }
    }

    async function refreshUpdate() {
      try {
        updateSummary.textContent = (await api("GET", `${base}/api/update/status`)).summary;
      } catch {
        updateSummary.textContent = "---";
      }
    }

    // 要求中はボタンを止め、失敗はトーストで出す。終わったら状態を取り直す
    async function run(action) {
      pending = true;
      render();
      try {
        await action();
      } catch (err) {
        toast(`${label}: ${err.message}`, false);
      } finally {
        pending = false;
        await refresh();
      }
    }

    acquireButton.addEventListener("click", () =>
      run(async () => applySnapshot(await api("POST", `${base}/api/control/acquire`)))
    );
    releaseButton.addEventListener("click", () =>
      run(async () => applySnapshot(await api("POST", `${base}/api/control/release`)))
    );

    updateButton.addEventListener("click", () =>
      run(async () => {
        const status = await api("POST", `${base}/api/update/check`);
        updateSummary.textContent = status.summary;
        if (!status.update_available) {
          toast(`${label}: ${status.summary}`);
          return;
        }
        // 文言はサーバが組んだものをそのまま出す（update.js と同じ）
        if (!window.confirm(`${status.hostname}\n${status.restart_notice}\n実行しますか？`)) {
          return;
        }
        await api("POST", `${base}/api/update/run`, {
          expected_head: status.repository.head,
        });
        toast(`${label}: 更新を開始しました`);
      })
    );

    if (runButton) {
      runButton.addEventListener("click", () =>
        run(async () => {
          if (!window.confirm(`${label}\nPCB: ${pcbFile ?? "PCB未選択"}\n塗布を実行しますか？`)) {
            return;
          }
          await api("POST", `${base}/api/jobs/${PASTE_JOB}`, { params: {} });
          toast(`${label}: 塗布を開始しました。進行は塗布ページで確認できます`);
        })
      );
    }

    render();
    refresh();
    refreshUpdate();
    return refresh;
  }

  const refreshers = [...document.querySelectorAll("[data-row-base]")].map(setupRow);
  setInterval(() => refreshers.forEach((refresh) => refresh()), POLL_MS);
})();
