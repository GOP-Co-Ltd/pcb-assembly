"use strict";

// 一括管理ページ（/bulk）の行操作。行（[data-row-base]）1 つが機体 1 台に対応する。
// 操作権・状態・WS はすべて機体ごとに別で、行ごとに独立して動く。
//
// 宛先: このページはマシン非依存なので、app.js の機体 prefix（BASE）が空になる。
// 行の base は data-row-base の値（"/m/<machine_id>"）。
// - HTTP: api() に `${base}/api/...` をそのまま渡す（api() は空の BASE しか付けない）
// - WS: api() を通らないので、withBase("/api/ws", base) で機体宛ての URL を組む
//
// 行ごとに WS を張り続ける理由: backend（ControlLease）は保持者の WS 在線で操作権の
// 生存を判定し、保持者の接続が 0 本のまま 30 秒経つと解放する。この失効はジョブ実行中も
// 止まらない。WS を張らないと、行で取った操作権が塗布の途中でも失効する。
//
// state の値: held = 自分が保持 / viewer = 他の端末が保持 / free = 空き /
// unknown = 操作権の状態を取得できない（機体が不通など）。
// state ごとのボタン（隠す = hidden、押せない = 表示したまま disabled）:
//   state   | 取得     | 解放   | 更新・塗布実行
//   free    | 押せる   | 隠す   | 押せない
//   viewer  | 押せない | 隠す   | 押せない
//   held    | 隠す     | 押せる | 押せる
//   unknown | 隠す     | 隠す   | 押せない
// 送信中（pending）は、表で「押せる」のボタンも押せなくする。
//
// 状態（GET /api/state）を取り直す契機は次の 3 つ。ポーリングはしない。
// - WS の open と close
// - WS の control_changed / state_changed
// - 行のボタン操作が終わったとき（成功・失敗を問わない）
// 更新の要約（GET /api/update/status）は WS の open のときだけ取り直す。
//
// 表示する値（更新の要約・PCB 名・保持者名）はサーバの応答をそのまま流す。

(() => {
  const { toast, api, createBackoff, withBase } = window.webui;

  // 塗布実行で開始するジョブ。パラメータは送らない（ジョブ定義の既定値で動く）
  const PASTE_JOB = "paste_solder";
  // 状態を取り直す WS イベント
  const REFRESH_EVENTS = new Set(["control_changed", "state_changed"]);

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

    // 冒頭の state の値（control.js と同じ区分）。最初の応答までは null で、
    // SSR の「確認中…」を残す
    let state = null;
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
      updateButton.disabled = blocked;
      // 不通の機体では前回の PCB を出し続けない
      if (pcbEl) pcbEl.textContent = state === "unknown" ? "---" : pcbFile ?? "PCB未選択";
      if (runButton) runButton.disabled = blocked;
    }

    async function refresh() {
      try {
        applySnapshot(await api("GET", `${base}/api/state`));
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

    const backoff = createBackoff(1000, 30000);

    function connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${proto}://${location.host}${withBase("/api/ws", base)}`);
      socket.addEventListener("open", () => {
        backoff.reset();
        // 切断中のイベントは届いていない。更新後の再起動から戻ったときもここを通る
        refresh();
        refreshUpdate();
      });
      socket.addEventListener("message", (event) => {
        if (REFRESH_EVENTS.has(JSON.parse(event.data).type)) refresh();
      });
      socket.addEventListener("close", () => {
        // 機体が落ちたなら GET も失敗し、行が unknown になる
        refresh();
        setTimeout(connect, backoff.next());
      });
    }

    connect();
  }

  document.querySelectorAll("[data-row-base]").forEach(setupRow);
})();
