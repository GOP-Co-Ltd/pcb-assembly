"use strict";

// 操作権リース（閲覧モード）の UI。
//
// body.dataset.control（"held" | "viewer" | "free" | "unknown"）の 1 箇所で全体を
// 切り替え、data-requires-control の要素に inert を付ける。無効化手段を inert に
// 統一しているのは、ジョブ状態を見て .disabled を書くモジュールと同じ属性を使うと
// 「ジョブ終了時に閲覧者のボタンが復活する」二重管理バグになるため。
//
// SSR は backend へのサーバ間通信でブラウザの cookie を持たないため「自分が保持者か」を
// 判定できない。したがって初期値は viewer（fail-closed）で、更新源は 3 つだけ:
// (a) ページロード後の GET /api/state、(b) WS の control_changed（job_console.js が
// 中継）、(c) api() の 423（app.js が onDenied を呼ぶ）。

(() => {
  const { toast, api } = window.webui;

  // fail-closed: サーバの事実が届くまでは閲覧者として扱う（先に押せると保持者の
  // 操作に割り込む）。held 以外（unknown を含む）はすべて塞ぐ。
  const INITIAL_STATE = "viewer";

  // 表示名の cookie。ProxyApp が backend へのヘッダに翻訳する（httpOnly にしない）
  const NAME_COOKIE = "pcbasm_name";
  const NAME_MAX_AGE = 60 * 60 * 24 * 365;

  const holderEl = document.getElementById("control-holder");
  const nameInput = document.getElementById("control-name");
  const acquireButton = document.getElementById("control-acquire");
  const releaseButton = document.getElementById("control-release");
  const takeoverButton = document.getElementById("control-takeover");
  // プロンプトの案内（ジョブコンソールを持つページのみ）。ダイアログ内にあるので
  // 「プロンプトが出ているときだけ」見える = 保留中かを知る必要がない
  const promptHint = document.getElementById("jc-prompt-hint");

  let state = INITIAL_STATE;
  // LeaseInfo（{key, display_name, held, connections}）。誰も保持していなければ null
  let holder = null;
  // GET /api/state の you.key（届くまでは null = 保持者と一致しない = 閲覧者）
  let myKey = null;

  function stateOf(control) {
    if (!control) return "unknown";
    if (!control.held) return "free";
    return control.key === myKey ? "held" : "viewer";
  }

  // {control, you} を反映する（GET /api/state と /api/control/* の戻りが同じ形）
  function applySnapshot(data) {
    if (data.you?.key) myKey = data.you.key;
    const control = data.control ?? null;
    holder = control?.held ? control : null;
    state = stateOf(control);
    render();
  }

  // 423 の body（{detail, holder}）。holder は誰も保持していなければ null
  function onDenied(data) {
    holder = data.holder?.held ? data.holder : null;
    state = holder ? "viewer" : "free";
    render();
  }

  function holderName() {
    return holder?.display_name || "他の端末";
  }

  function render() {
    document.body.dataset.control = state;
    const blocked = state !== "held";
    for (const el of document.querySelectorAll("[data-requires-control]")) {
      el.toggleAttribute("inert", blocked);
    }
    renderBanner();
    renderPromptHint();
  }

  function renderBanner() {
    if (holderEl !== null) holderEl.textContent = holderText();
    // 取得は空いているとき（保持者がいるなら奪取）、解放は保持中だけ
    if (acquireButton) acquireButton.hidden = state === "held" || state === "viewer";
    if (releaseButton) releaseButton.hidden = state !== "held";
    if (takeoverButton) takeoverButton.hidden = state !== "viewer";
  }

  function holderText() {
    if (state === "held") return "操作権: あなた";
    if (state === "viewer") return `操作権: ${holderName()}（閲覧のみ）`;
    if (state === "free") return "操作権: 空き";
    return "操作権: 不明";
  }

  // プロンプト本文は閲覧者にも見せる（何を待っているか分からないより良い）。
  // 保持者が居ないまま応答待ちになったら、取得を促す（誰も応答できない詰みの回避）。
  function renderPromptHint() {
    if (promptHint === null) return;
    if (state === "held") {
      promptHint.textContent = "";
    } else if (state === "viewer") {
      promptHint.textContent = `〈${holderName()}〉の応答待ち`;
    } else if (state === "free") {
      promptHint.textContent = "操作権が空いています。取得して応答してください";
    } else {
      promptHint.textContent = "操作権の状態を確認中です";
    }
  }

  async function refresh() {
    try {
      applySnapshot(await api("GET", "/api/state"));
    } catch {
      // 状態が読めない間も塞いだままにする（fail-closed）。トーストは出さない
      // （backend 不通のページで毎回鳴る）
      state = "unknown";
      render();
    }
  }

  async function post(url, successMessage) {
    try {
      applySnapshot(await api("POST", url));
      if (successMessage !== null) toast(successMessage);
    } catch (err) {
      // 423 のときは api() が onDenied を呼んで状態を直している
      toast(err.message, false);
    }
  }

  function readNameCookie() {
    const prefix = `${NAME_COOKIE}=`;
    const found = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(prefix));
    return found ? decodeURIComponent(found.slice(prefix.length)) : "";
  }

  function writeNameCookie(name) {
    // 表示名は latin-1 のヘッダに生では載らないので encodeURIComponent で書く
    // （ProxyApp は quote 済みの値としてそのまま backend へ渡す）
    document.cookie =
      `${NAME_COOKIE}=${encodeURIComponent(name)};` +
      ` Path=/; Max-Age=${NAME_MAX_AGE}; SameSite=Lax`;
  }

  // 先に fail-closed を適用してからイベントを配線する（配線側で何かあっても塞がった状態は残る）
  render();

  if (nameInput) {
    nameInput.value = readNameCookie();
    nameInput.addEventListener("change", () => {
      writeNameCookie(nameInput.value.trim());
      // 保持者の表示名は backend が持っているので更新を通知する。閲覧者の名乗りは
      // 取得時にヘッダで届くので送らない
      if (state === "held") post("/api/control/name", null);
    });
  }
  if (acquireButton) {
    acquireButton.addEventListener("click", () =>
      post("/api/control/acquire", "操作権を取得しました")
    );
  }
  if (releaseButton) {
    releaseButton.addEventListener("click", () =>
      post("/api/control/release", "操作権を解放しました")
    );
  }
  if (takeoverButton) {
    takeoverButton.addEventListener("click", () =>
      post("/api/control/takeover", "操作権を奪取しました")
    );
  }

  window.webui.control = {
    // app.js が 423 のときだけ呼ぶ
    onDenied,
    // job_console.js が WS の control_changed を中継する（you は前回値を使う）
    applyControl: (control) => applySnapshot({ control }),
    // WS 再接続時の取り直し（切断中の control_changed を取りこぼしている）
    refresh,
    state: () => state,
  };

  refresh();
})();
