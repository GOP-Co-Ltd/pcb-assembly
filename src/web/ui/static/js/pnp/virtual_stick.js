"use strict";

// 画面の仮想スティック（コントローラ操作のジョブのページ）。XY パッドと Z スライダーを押して倒している間、
// 倒し具合（-1〜1）を 20 Hz で WS command の stick として送り、離したら 0 を送る。
// - 向きはスティックの座標で送る: パッドは右 = +x・上 = +y、スライダーは上 = +z。
//   機体の向き（上 = Y−）への直しと、最高速度を掛けるのは backend（pcbasm.pnp.stick）がする
// - ここではピクセルを -1〜1 に直して送るだけで、デッドゾーンや速度は計算しない
// - 指を離したときのほか、pointercancel・lostpointercapture・タブが隠れたとき・ウィンドウがフォーカスを失ったときも離したことにする
// - 送れないとき（ジョブが走っていない・操作権が無い・WS が切れた）は送るのをやめる。
//   トーストは押した操作ごとに 1 回だけ出す（20 Hz で出さない）。送信が途切れても backend は期限を過ぎたら止める

(() => {
  const { toast, jobs } = window.webui;
  const root = document.getElementById("virtual-stick");
  if (!root || !jobs) return;

  const SEND_INTERVAL_MS = 50; // 20 Hz
  const jobName = root.dataset.jobName;
  const value = { x: 0, y: 0, z: 0 };
  // 押している操作子 → pointerId
  const held = new Map();
  let timer = null;

  // パッドは中心からの向きと距離（円の外は縁に収める）。上を +y にする
  function readPad(el, event) {
    const rect = el.getBoundingClientRect();
    const rx = rect.width / 2;
    const ry = rect.height / 2;
    let x = (event.clientX - rect.left - rx) / rx;
    let y = (rect.top + ry - event.clientY) / ry;
    const length = Math.hypot(x, y);
    if (length > 1) {
      x /= length;
      y /= length;
    }
    return { x, y };
  }

  // スライダーは中心からの縦の距離。上を +z にする
  function readZ(el, event) {
    const rect = el.getBoundingClientRect();
    const half = rect.height / 2;
    const z = (rect.top + half - event.clientY) / half;
    return { z: Math.max(-1, Math.min(1, z)) };
  }

  // knob はつまみの表示位置（-1〜1 の横と縦）
  const controls = [
    {
      el: document.getElementById("vs-pad"),
      read: readPad,
      neutral: { x: 0, y: 0 },
      knob: (v) => [v.x, v.y],
    },
    {
      el: document.getElementById("vs-z"),
      read: readZ,
      neutral: { z: 0 },
      knob: (v) => [0, v.z],
    },
  ].filter((control) => control.el !== null);

  function blockedReason() {
    if (!jobs.commandReady(jobs.currentJob(), { name: jobName })) {
      return "コントローラ操作のジョブを実行してから操作してください";
    }
    if (window.webui.control?.state() !== "held") {
      return "操作権が無いので送れません";
    }
    return null;
  }

  function send() {
    return jobs.sendCommand({ type: "stick", x: value.x, y: value.y, z: value.z });
  }

  function place(control, read) {
    Object.assign(value, read);
    const [kx, ky] = control.knob(value);
    control.el.style.setProperty("--vs-x", kx);
    control.el.style.setProperty("--vs-y", ky);
  }

  function center(control) {
    place(control, control.neutral);
  }

  function tick() {
    const reason = blockedReason();
    if (reason !== null || !send()) {
      halt(reason ?? "WebSocket 未接続のため送信できません");
    }
  }

  function stopTimer() {
    clearInterval(timer);
    timer = null;
  }

  // 送れなくなったら、押していた操作子をすべて戻して送るのをやめる（0 も送れないので、止めるのは backend の期限）
  function halt(message) {
    for (const control of held.keys()) center(control);
    held.clear();
    stopTimer();
    toast(message, false);
  }

  function release(control) {
    held.delete(control);
    center(control);
    if (blockedReason() === null) send();
    if (held.size === 0) stopTimer();
  }

  function releaseAll() {
    for (const control of [...held.keys()]) release(control);
  }

  for (const control of controls) {
    control.el.addEventListener("pointerdown", (event) => {
      if (held.has(control)) return;
      event.preventDefault();
      const reason = blockedReason();
      if (reason !== null) {
        toast(reason, false);
        return;
      }
      control.el.setPointerCapture(event.pointerId);
      held.set(control, event.pointerId);
      place(control, control.read(control.el, event));
      if (timer === null) {
        tick();
        if (held.size > 0) timer = setInterval(tick, SEND_INTERVAL_MS);
      }
    });
    control.el.addEventListener("pointermove", (event) => {
      if (held.get(control) === event.pointerId) {
        place(control, control.read(control.el, event));
      }
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
      control.el.addEventListener(type, (event) => {
        if (held.get(control) === event.pointerId) release(control);
      });
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) releaseAll();
  });
  window.addEventListener("blur", releaseAll);
  // ジョブが終わったら、押したままでも送るのをやめる
  jobs.onUpdate((job) => {
    if (!jobs.commandReady(job, { name: jobName })) releaseAll();
  });
})();
