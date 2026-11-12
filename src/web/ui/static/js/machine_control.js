"use strict";

// マシン操作パネル（右サイドバー）。REST (/api/machine/command) への送信と、
// 展開中だけのステータスポーリングを行う。
// - XY は円形ジョグパッド（SVG）、Z は縦バー。距離はリングで切り替える
// - 座標欄は常に現在のマシン座標を表示し、入力確定（change）で絶対移動する
// - 対話ジョブ（accepts_commands）実行中は WS command 送信に切り替える
//   （送信先が REST か WS かの違いのみで、操作系は共通）

(() => {
  const { toast, api, svgEl } = window.webui;
  const sidebar = document.getElementById("mc-sidebar");
  const panel = document.getElementById("machine-control");
  if (!sidebar || !panel) return;
  const layout = sidebar.closest(".layout");

  const positionEl = document.getElementById("mc-position");
  const homedEl = document.getElementById("mc-homed");
  const absInputs = [...panel.querySelectorAll("[data-mc-abs]")];
  const COLLAPSE_KEY = "mc-sidebar-collapsed";

  function activeJob() {
    const job = window.webui.jobs ? window.webui.jobs.currentJob() : null;
    return window.webui.jobs && window.webui.jobs.isActive(job) ? job : null;
  }

  // 命令は REST と WS command で同じ形（web.contract の MachineCommandRequest）
  async function sendControl(command) {
    const job = activeJob();
    if (job) {
      // ジョブモード: 対話ジョブの command キューへ送る
      if (!job.accepts_commands) {
        toast("ジョブ実行中はマシン操作できません", false);
        return;
      }
      window.webui.jobs.sendCommandOrToast(command);
      return;
    }
    try {
      const status = await api("POST", "/api/machine/command", command);
      renderStatus(status);
      toast("操作完了");
    } catch (err) {
      toast(err.message, false);
    }
  }

  // 部品実装機だけ（テンプレート側で出さなければ null）。押すとコントローラ操作のジョブを始め、
  // 実行中にもう一度押すと中止して終える
  const controllerToggle = document.getElementById("mc-controller-toggle");

  // 対話を受けないジョブの実行中はパネルを disabled 表示にする
  // （テンプレート由来の disabled（フォーカス Z 未設定）は維持する）。
  // コントローラ操作のボタンは別扱い: そのジョブの間は押せ（終えるため）、ほかのジョブの間は押せない
  if (window.webui.jobs) {
    const originallyDisabled = new Set(
      [...panel.querySelectorAll("button, input")].filter((c) => c.disabled)
    );
    window.webui.jobs.onUpdate((job) => {
      const active = window.webui.jobs.isActive(job);
      const blocked = active && !job.accepts_commands;
      panel.classList.toggle("mc-blocked", blocked);
      for (const control of panel.querySelectorAll("button, input")) {
        if (control === controllerToggle) continue;
        control.disabled = blocked || originallyDisabled.has(control);
      }
      if (controllerToggle) renderController(job, active);
    });
  }

  function renderStatus(status) {
    if (!status.connected) {
      positionEl.textContent = "位置: 接続エラー";
      homedEl.textContent = status.error ? `(${status.error})` : "";
      return;
    }
    const p = status.position;
    positionEl.textContent = `位置: ${status.position_label}`;
    homedEl.textContent = `homed: ${status.homed_axes || "なし"}`;
    // 座標欄は現在位置を常時表示する（編集中の欄は上書きしない）
    for (const input of absInputs) {
      if (document.activeElement !== input) {
        input.value = p[input.dataset.mcAbs].toFixed(3);
      }
    }
  }

  async function pollStatus() {
    try {
      renderStatus(await api("GET", "/api/machine/status"));
    } catch (err) {
      positionEl.textContent = "位置: 取得失敗";
    }
  }

  // ---- 円形 XY ジョグパッド（SVG） ----

  const CENTER = 100;
  // 移動距離と扇形の半径範囲（内側から外側の順）
  const RINGS = [
    { dist: 0.1, r0: 22, r1: 46 },
    { dist: 1, r0: 48, r1: 72 },
    { dist: 10, r0: 74, r1: 98 },
  ];
  // 扇形の方向: 中心角（SVG 座標系。y は下向きが正なので上= -90°）
  // カメラ座標系に合わせ、画面下方向を Y+ とする（上=Y-、下=Y+）
  const SECTORS = [
    { axis: "x", sign: 1, mid: 0 },    // 右 = X+
    { axis: "y", sign: 1, mid: 90 },   // 下 = Y+（カメラ座標系: 下が正）
    { axis: "x", sign: -1, mid: 180 }, // 左 = X-
    { axis: "y", sign: -1, mid: 270 }, // 上 = Y-
  ];
  const HALF_SPAN = 40; // 扇形の片側角度（隙間 10°）

  function polar(r, deg) {
    const rad = (deg * Math.PI) / 180;
    return [CENTER + r * Math.cos(rad), CENTER + r * Math.sin(rad)];
  }

  function sectorPath(r0, r1, a0, a1) {
    const [x0, y0] = polar(r0, a0);
    const [x1, y1] = polar(r1, a0);
    const [x2, y2] = polar(r1, a1);
    const [x3, y3] = polar(r0, a1);
    return (
      `M${x0},${y0} L${x1},${y1} ` +
      `A${r1},${r1} 0 0 1 ${x2},${y2} L${x3},${y3} ` +
      `A${r0},${r0} 0 0 0 ${x0},${y0} Z`
    );
  }

  function buildJogPad(container) {
    const svg = svgEl("svg", { viewBox: "0 0 200 200", class: "mc-jogpad-svg" });
    for (const sector of SECTORS) {
      for (const ring of RINGS) {
        const distance = sector.sign * ring.dist;
        const group = svgEl("g", { class: "jog-seg", role: "button" });
        group.appendChild(
          svgEl("path", {
            d: sectorPath(ring.r0, ring.r1, sector.mid - HALF_SPAN, sector.mid + HALF_SPAN),
          })
        );
        const rMid = (ring.r0 + ring.r1) / 2;
        const [tx, ty] = polar(rMid, sector.mid);
        const label = svgEl("text", { x: tx, y: ty });
        label.textContent = `${distance > 0 ? "+" : ""}${distance}`;
        group.appendChild(label);
        const title = svgEl("title", {});
        title.textContent = `${sector.axis.toUpperCase()} ${distance > 0 ? "+" : ""}${distance} mm`;
        group.appendChild(title);
        group.addEventListener("click", () => {
          sendControl({ type: "jog", axis: sector.axis, distance });
        });
        svg.appendChild(group);
      }
    }
    // 中心 = 全軸ホーミング
    const home = svgEl("g", { class: "jog-center", role: "button" });
    home.appendChild(svgEl("circle", { cx: CENTER, cy: CENTER, r: 18 }));
    const homeLabel = svgEl("text", { x: CENTER, y: CENTER });
    homeLabel.textContent = "⌂";
    home.appendChild(homeLabel);
    const homeTitle = svgEl("title", {});
    homeTitle.textContent = "全軸ホーミング";
    home.appendChild(homeTitle);
    home.addEventListener("click", () => sendControl({ type: "home", axes: [] }));
    svg.appendChild(home);
    container.replaceChildren(svg);
  }

  buildJogPad(document.getElementById("mc-jogpad"));

  // ---- ボタン・入力のイベント ----

  // homing
  for (const button of panel.querySelectorAll("[data-mc-home]")) {
    button.addEventListener("click", () => {
      const axis = button.dataset.mcHome;
      sendControl({ type: "home", axes: axis ? [axis] : [] });
    });
  }

  // Z ジョグ（縦バー）
  for (const button of panel.querySelectorAll("[data-mc-jog]")) {
    button.addEventListener("click", () => {
      sendControl({
        type: "jog",
        axis: button.dataset.mcJog,
        distance: Number(button.dataset.mcDist),
      });
    });
  }

  // 絶対移動: 座標欄の入力確定（Enter / フォーカスアウト）でその軸だけ移動
  for (const input of absInputs) {
    input.addEventListener("change", () => {
      const text = input.value.trim();
      if (text === "") return;
      sendControl({ type: "move", [input.dataset.mcAbs]: Number(text) });
      input.blur();
    });
  }

  document.getElementById("mc-relax").addEventListener("click", () => {
    sendControl({ type: "relax" });
  });

  // 新ファームの機体ではテンプレート側でボタンを出さない（null ガード）
  const focusZButton = document.getElementById("mc-focus-z");
  if (focusZButton) {
    focusZButton.addEventListener("click", () => {
      sendControl({ type: "focus_z" });
    });
  }

  // paste マシン以外ではテンプレート側でボタンを出さない（null ガード）
  const moveToCapButton = document.getElementById("mc-move-to-cap");
  if (moveToCapButton) {
    moveToCapButton.addEventListener("click", () => {
      sendControl({ type: "move_to_cap" });
    });
  }

  // 部品実装機以外ではテンプレート側でボタンを出さない（null ガード）。
  // サーボは Klipper の外なので、命令（REST / 対話ジョブの WS）とは別の API へ送る
  const servoRelaxButton = document.getElementById("mc-servo-relax");
  if (servoRelaxButton) {
    servoRelaxButton.addEventListener("click", async () => {
      try {
        await api("POST", "/api/pnp/servo/relax");
        toast("サーボを脱力しました");
      } catch (err) {
        toast(err.message, false);
      }
    });
  }

  // ---- コントローラ操作（部品実装機だけ。テンプレート側でボタンを出さなければ何もしない） ----
  // ジョブのログを出すコンソールはこのボタンの近くに無いので、失敗だけはトーストで知らせる

  // このページで始めたか、実行中を見たコントローラ操作のジョブの id。終わったときに 1 回だけ失敗を知らせる
  // （ページを開いたときに届く、前に終わったジョブの失敗は知らせない）
  let controllerJobId = null;

  function renderController(job, active) {
    const running = active && job.name === controllerToggle.dataset.jobName;
    controllerToggle.disabled = active && !running;
    controllerToggle.setAttribute("aria-pressed", String(running));
    controllerToggle.textContent = running ? "コントローラ操作を終える" : "コントローラ操作";
    if (running) {
      controllerJobId = job.id;
    } else if (job?.id === controllerJobId) {
      controllerJobId = null;
      if (job.status === "failed") {
        toast(`${job.label}に失敗しました: ${job.error || "不明なエラー"}`, false);
      }
    }
  }

  if (controllerToggle && window.webui.jobs) {
    controllerToggle.addEventListener("click", async () => {
      const jobs = window.webui.jobs;
      const name = controllerToggle.dataset.jobName;
      const job = jobs.currentJob();
      if (jobs.isActive(job)) {
        if (job.name === name) await jobs.abort();
        return;
      }
      controllerToggle.disabled = true; // 開始の応答まで二重に押させない（状態は WS の job_status で戻る）
      try {
        const data = await api("POST", `/api/jobs/${name}`, { params: {} });
        // すぐ失敗したジョブは、POST の応答より先に終端の job_status だけが届くことがある。
        // 応答の id で追い、届き済みの状態でもう一度描いて失敗を見落とさない
        controllerJobId = data.job.id;
        const current = jobs.currentJob();
        renderController(current, jobs.isActive(current));
      } catch (err) {
        toast(err.message, false);
        controllerToggle.disabled = false;
      }
    });
  }

  // ---- ピンセット（部品実装機だけ。テンプレート側で欄を出さなければ何もしない） ----
  // 回転は相対角、開閉は開き具合（0=閉じ切り、1=開き切り）を送る。
  // スライダーと数値欄は backend が返す目標（target_fraction）を、「現在」は実測（open_fraction）を表示する

  const openSlider = document.getElementById("mc-open-slider");
  const openField = document.getElementById("mc-open");
  const openCurrent = document.getElementById("mc-open-current");

  for (const button of panel.querySelectorAll("[data-mc-rotate]")) {
    button.addEventListener("click", async () => {
      try {
        await api("POST", "/api/pnp/servo/rotate", {
          degrees: Number(button.dataset.mcRotate),
        });
      } catch (err) {
        toast(err.message, false);
      }
    });
  }

  // 開閉の指令は 1 本ずつ送る。送信中に来た値は最新の 1 つだけ保留し、応答が返ったら送る
  // （サーバーの応答の速さで自然に間引かれ、最後の値は必ず届く）
  let openSending = false;
  let openPending = null;
  let openRequests = 0; // 送信を始めた回数。送信をまたいだポーリングの結果を捨てるのに使う

  async function sendOpen(fraction) {
    if (openSending) {
      openPending = fraction;
      return;
    }
    openSending = true;
    let next = fraction;
    while (next !== null) {
      openRequests += 1;
      try {
        await api("POST", "/api/pnp/servo/open", { fraction: next });
      } catch (err) {
        toast(err.message, false);
      }
      next = openPending;
      openPending = null;
    }
    openSending = false;
  }

  function openBusy() {
    return openSending || openSlider.matches(":active");
  }

  if (openSlider && openField) {
    // 動かしている間の値（input）を送ってサーボを追従させる。離した位置は最後の input の値で、
    // 保留の仕組みで必ず届くので change は聞かない
    openSlider.addEventListener("input", () => {
      openField.value = Number(openSlider.value).toFixed(2); // 数値欄も動かしている値に揃える
      sendOpen(Number(openSlider.value));
    });
    openField.addEventListener("change", () => {
      const text = openField.value.trim();
      if (text === "") return;
      sendOpen(Number(text));
      openField.blur();
    });
  }

  async function pollTweezer() {
    if (!openSlider || !openField) return;
    const requests = openRequests;
    try {
      const status = await api("GET", "/api/pnp/servo/status");
      openCurrent.textContent = `現在 ${status.open_fraction.toFixed(2)}`;
      // 取得中に指令を送っていたら、その結果は古い目標なので欄に出さない
      if (requests !== openRequests || openBusy()) return;
      openSlider.value = String(status.target_fraction);
      if (document.activeElement !== openField) {
        openField.value = status.target_fraction.toFixed(2);
      }
    } catch (err) {
      // 役割の設定前などは表示できない。ポーリングのたびに通知しない
      openCurrent.textContent = "現在 ---";
      if (document.activeElement !== openField) openField.value = "";
    }
  }

  // ---- サイドバーの開閉とポーリング ----

  let timer = null;
  const toggle = document.getElementById("mc-toggle");

  function pollAll() {
    pollStatus();
    pollTweezer();
  }

  function startPolling() {
    if (timer !== null) return;
    pollAll();
    timer = setInterval(pollAll, 2000);
  }

  function stopPolling() {
    clearInterval(timer);
    timer = null;
  }

  // 折りたたみ中と背景タブでは Moonraker に問い合わせない（放置タブによる負荷を減らす）
  function updatePolling() {
    if (sidebar.classList.contains("collapsed") || document.hidden) stopPolling();
    else startPolling();
  }

  function applyCollapsed(collapsed) {
    sidebar.classList.toggle("collapsed", collapsed);
    if (layout) {
      layout.style.setProperty(
        "--mc-track-w",
        collapsed ? "2rem" : "var(--mc-w, 19rem)"
      );
    }
    // 右サイドバー: 展開中は "<"（左へ畳む）、折りたたみ中は ">"（右へ開く）
    toggle.textContent = collapsed ? ">" : "<";
    updatePolling();
  }

  toggle.addEventListener("click", () => {
    const collapsed = !sidebar.classList.contains("collapsed");
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "");
    applyCollapsed(collapsed);
  });

  applyCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
  document.addEventListener("visibilitychange", updatePolling);

  // ---- 左端ハンドルのドラッグで幅を変更 ----

  const WIDTH_KEY = "mc-sidebar-width";
  const MIN_WIDTH = 220;
  const MAX_WIDTH = 600;
  const resizeHandle = document.getElementById("mc-resize");

  function applyWidth(px) {
    const clamped = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(px)));
    if (layout) layout.style.setProperty("--mc-w", `${clamped}px`);
    return clamped;
  }

  const savedWidth = Number(localStorage.getItem(WIDTH_KEY));
  if (savedWidth) applyWidth(savedWidth);

  if (resizeHandle) {
    resizeHandle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = sidebar.getBoundingClientRect().width;
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";

      const onMove = (moveEvent) => {
        // 右サイドバー: 左へドラッグ（clientX 減少）で幅を広げる
        const width = applyWidth(startWidth + (startX - moveEvent.clientX));
        localStorage.setItem(WIDTH_KEY, String(width));
      };
      const onUp = () => {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
      };
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
    });
  }
})();
