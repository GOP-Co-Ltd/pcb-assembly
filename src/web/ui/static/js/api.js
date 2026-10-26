"use strict";

// HTTP の唯一の入口と、保存待ちの唯一の置き場。base.html が最初に読み込み、window.webui に公開する。
// 他のモジュールは fetch を直接呼ばず、宛先に応じて次の関数を使う
// （tests/web/ui/test_layout.py が静的に検査する）。
// - 表示中の機体の backend（"/api/..." / "/artifacts/..."）: api() / downloadApi()
// - frontend 自身（/api/machines, /api/self-update/**, /api/update-notice）: frontendApi() / frontendJson()
// - WS・<img>・リンクなどブラウザが直接開く backend の URL: withBase() で機体 prefix を付ける
//
// 入力の即保存はすべて pendingSaves を通す。ジョブを始める前に job_console.js が flush し、
// 保存が終わっていない（古い設定のまま走る）状態でジョブを始めない。

// ---- machine prefix ----

// サーバが組んだ機体 prefix（"/m/<machine_id>"）。マシン非依存のページでは空。
// backend 相対のパス（"/api/..." / "/artifacts/..."）を機体宛てに直す処理は
// ここ 1 箇所だけにする（各所の URL literal は backend 相対のまま残す）。
const BASE = document.body.dataset.machineBase ?? "";

// base を渡すと表示中の機体ではなくその機体宛てにする（マシン非依存の機体一覧用）
function withBase(path, base = BASE) {
  return `${base}${path}`;
}

// ---- HTTP ----

async function fetchApi(method, url, body) {
  const options = { method };
  if (body instanceof FormData) {
    // multipart は fetch が boundary 付き Content-Type を自動設定する
    options.body = body;
  } else if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const res = await fetch(withBase(url), options);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const err = new Error(data.detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.data = data;
    // 423 = 操作権を持っていない。UI 全体の状態を即座に閲覧モードへ切り替える
    if (res.status === 423) window.webui.control?.onDenied(data);
    throw err;
  }
  return res;
}

async function api(method, url, body) {
  const res = await fetchApi(method, url, body);
  return res.json().catch(() => ({}));
}

async function downloadApi(method, url, body) {
  const res = await fetchApi(method, url, body);
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const quoted = disposition.match(/filename="([^"]+)"/i)?.[1];
  const plain = disposition.match(/filename=([^;\s]+)/i)?.[1];
  const filename = encoded
    ? decodeURIComponent(encoded)
    : quoted ?? plain ?? "download";
  const objectUrl = URL.createObjectURL(await res.blob());
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
  return filename;
}

// frontend 自身のエンドポイント（/api/machines, /api/self-update/**, /api/update-notice）を呼ぶ。
// machine prefix は付けない（付けると backend へ中継されて 404 になる）。
async function frontendApi(method, url, body) {
  const options = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(url, options);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const err = new Error(data.detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return res.json().catch(() => ({}));
}

async function frontendJson(url) {
  return frontendApi("GET", url);
}

// ---- 保存待ち ----

// 入力の遅延保存（schedule）と進行中の保存（run）をページ全体で 1 か所にまとめる。
// - schedule(key, save, delay): 同じ key の未送信の保存を置き換え、delay 後に save() を呼ぶ
// - run(label, save): 保存を実行して完了まで追跡する。失敗は「label: 理由」でトーストに出す
// - flush(): 未送信の保存をすぐ送り、進行中の保存がすべて終わるまで待つ。最初の失敗を返す（無ければ null）
function createPendingSaves() {
  const scheduled = new Map();
  const running = new Set();

  function cancel(key) {
    clearTimeout(scheduled.get(key)?.timer);
    scheduled.delete(key);
  }

  function schedule(key, save, delay) {
    cancel(key);
    const flush = () => {
      cancel(key);
      save();
    };
    scheduled.set(key, { flush, timer: setTimeout(flush, delay) });
  }

  function run(label, save) {
    const result = Promise.resolve().then(save).then(
      () => null,
      (error) => {
        const failure = new Error(`${label}: ${error.message}`);
        window.webui.toast(failure.message, false);
        return failure;
      }
    );
    running.add(result);
    result.then(() => running.delete(result));
    return result;
  }

  async function flush() {
    let failure = null;
    while (scheduled.size || running.size) {
      const pending = new Set(running);
      for (const entry of [...scheduled.values()]) entry.flush();
      for (const result of running) pending.add(result);
      for (const error of await Promise.all(pending)) failure ??= error;
    }
    return failure;
  }

  return { cancel, schedule, run, flush };
}

window.webui = {
  api,
  downloadApi,
  frontendApi,
  frontendJson,
  withBase,
  pendingSaves: createPendingSaves(),
};
