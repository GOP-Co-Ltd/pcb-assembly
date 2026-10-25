"use strict";

// 機体一覧（/machines）の行操作。行（[data-row-base]）1 つが機体 1 台に対応する。
// 操作権・状態・WS はすべて機体ごとに別で、行ごとに独立して動く。
//
// 宛先: このページはマシン非依存なので、api.js の機体 prefix（BASE）が空になる。
// 行の base は data-row-base の値（"/m/<machine_id>"）。
// - HTTP: api() に `${base}/api/...` をそのまま渡す（api() は空の BASE しか付けない）
// - WS: ws.js の connectEvents に base を渡す
//
// 行ごとに WS を張り続ける理由: 他の端末による取得・解放や PCB の選び直しを、
// ポーリングせずに WS のイベントで知るため。操作権は WS が切れても失効しない
// （外れるのは解放・奪取・backend の再起動だけ）。
//
// state の値: held = 自分が保持 / viewer = 他の端末が保持 / free = 空き（ここまでは backend の control.state）/
// unknown = 操作権の状態を取得できない（機体が不通など）。
// state ごとのボタン（隠す = hidden、押せない = 表示したまま disabled）:
//   state   | 取得     | 解放   | 塗布実行
//   free    | 押せる   | 隠す   | 押せない
//   viewer  | 押せない | 隠す   | 押せない
//   held    | 隠す     | 押せる | 押せる
//   unknown | 隠す     | 隠す   | 押せない
// 送信中（pending）は、表で「押せる」のボタンも押せなくする。
//
// 状態（GET /api/session/state）を取り直す契機は次の 3 つ。ポーリングはしない。
// - WS の open と close
// - WS の control_changed / state_changed
// - 行のボタン操作が終わったとき（成功・失敗を問わない）
// 更新の要約（GET /api/update/status）は WS の open のときだけ取り直す。更新の操作は /update で行う。
// 立ち上げの要約（GET /api/readiness）は WS の open と state_changed（machine.toml の書き込み）、
// job_status（ジョブの開始・質問・終了。装置診断や校正のジョブは終わるときに結果のファイルを書く）で取り直す。
//
// 表示する値（更新と立ち上げの要約・PCB 名・保持者名）はサーバの応答をそのまま流す。

(() => {
  const { toast, api, connectEvents } = window.webui;

  // 塗布実行で開始するジョブ。パラメータは送らない（ジョブ定義の既定値で動く）
  const PASTE_JOB = "paste_solder";
  // 状態を取り直す WS イベント
  const REFRESH_EVENTS = new Set(["control_changed", "state_changed"]);
  // 立ち上げの要約を取り直す WS イベント
  const READINESS_EVENTS = new Set(["state_changed", "job_status"]);

  function setupRow(row) {
    const base = row.dataset.rowBase;
    const label = row.dataset.label;
    const part = (id) => row.querySelector(`[data-testid="${id}"]`);
    const holderEl = part("machines-holder");
    const acquireButton = part("machines-acquire");
    const releaseButton = part("machines-release");
    const updateSummary = part("machines-update-summary");
    const readinessEl = part("machines-readiness");
    // ペーストの行にだけある
    const pcbEl = part("machines-pcb");
    const runButton = part("machines-paste-run");

    // 冒頭の state の値（control.js と同じ区分）。最初の応答までは null で、
    // SSR の「確認中…」を残す
    let state = null;
    let holderName = "";
    let pcbFile = null;
    // 要求の送信中はその行のボタンを押せなくする（二重送信を防ぐ）
    let pending = false;

    function applySnapshot(data) {
      state = data.control.state;
      holderName = data.control.display_name || "他の端末";
      if ("pcb_file" in data) pcbFile = data.pcb_file;
      render();
    }

    function render() {
      if (state === null) return;
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
      // 不通の機体では前回の PCB を出し続けない
      if (pcbEl) pcbEl.textContent = state === "unknown" ? "---" : pcbFile ?? "PCB未選択";
      if (runButton) runButton.disabled = blocked;
    }

    async function refresh() {
      try {
        applySnapshot(await api("GET", `${base}/api/session/state`));
      } catch {
        // 不通の機体は操作不可のまま表示だけ変える（再接続のたびにトーストを出さない）
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

    async function refreshReadiness() {
      try {
        readinessEl.textContent = (await api("GET", `${base}/api/readiness`)).label;
      } catch {
        readinessEl.textContent = "---";
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
      run(async () => applySnapshot(await api("POST", `${base}/api/session/control/acquire`)))
    );
    releaseButton.addEventListener("click", () =>
      run(async () => applySnapshot(await api("POST", `${base}/api/session/control/release`)))
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

    connectEvents({
      base,
      onOpen: () => {
        // 切断中のイベントは届いていない。更新後の再起動から戻ったときもここを通る
        refresh();
        refreshUpdate();
        refreshReadiness();
      },
      onEvent: (event) => {
        if (REFRESH_EVENTS.has(event.type)) refresh();
        if (READINESS_EVENTS.has(event.type)) refreshReadiness();
      },
      // 機体が落ちたなら GET も失敗し、行が unknown になる
      onClose: refresh,
    });
  }

  document.querySelectorAll("[data-row-base]").forEach(setupRow);
})();
