"use strict";

// 全ページ共通の JS。base.html が最初に読み込み、共通関数を window.webui に公開する。
// 他のモジュールは fetch を直接呼ばず、宛先に応じて次の関数を使う
// （tests/web/ui/test_layout.py が静的に検査する）。
// - 表示中の機体の backend（"/api/..." / "/artifacts/..."）: api() / downloadApi()
// - frontend 自身（/api/machines, /api/self-update/**, /api/update-notice）: frontendApi() / frontendJson()
// - WS・<img>・リンクなどブラウザが直接開く backend の URL: withBase() で機体 prefix を付ける

// ---- machine prefix ----

// サーバが組んだ機体 prefix（"/m/<machine_id>"）。マシン非依存のページでは空。
// backend 相対のパス（"/api/..." / "/artifacts/..."）を機体宛てに直す処理は
// ここ 1 箇所だけにする（各所の URL literal は backend 相対のまま残す）。
const BASE = document.body.dataset.machineBase ?? "";

// base を渡すと表示中の機体ではなくその機体宛てにする（マシン非依存の一括管理ページ用）
function withBase(path, base = BASE) {
  return `${base}${path}`;
}

// ---- shared helpers ----

// level: true=通常 / false=エラー / "warning"=警告
function toast(message, level = true) {
  const container = document.getElementById("toasts");
  if (!container) return;
  const el = document.createElement("div");
  el.className =
    level === "warning" ? "toast warning" : level ? "toast" : "toast error";
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

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
// fetch() の呼び出しをこのファイルに限定するための入口でもある（tests/web/ui/test_layout.py）。
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

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    el.setAttribute(key, value);
  }
  return el;
}

// 単一タイマーの debounce（キー別にまとめたい場合は各所の Map 実装を使う）
function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// 指数バックオフの遅延生成器。next() は現在の遅延を返して次の遅延を 2 倍にする。
// reset() で初期値に戻す
function createBackoff(baseMs, maxMs) {
  let delay = baseMs;
  return {
    next() {
      const current = delay;
      delay = Math.min(delay * 2, maxMs);
      return current;
    },
    reset() {
      delay = baseMs;
    },
  };
}

function formatPosition(p) {
  return `X${p.x.toFixed(3)} Y${p.y.toFixed(3)} Z${p.z.toFixed(3)}`;
}

// expose for other scripts
window.webui = {
  toast,
  api,
  downloadApi,
  frontendApi,
  frontendJson,
  svgEl,
  debounce,
  createBackoff,
  formatPosition,
  withBase,
};

// ---- topbar safety controls ----

async function postTopbarCommand(button, url, successMessage, failurePrefix) {
  button.disabled = true;
  try {
    // 何を再起動したかはサーバが文言まで組んで返す（組み立てを JS に複製しない）。
    // 文言を返さないコマンド（緊急停止）では呼び出し側の既定文言を使う
    const result = await api("POST", url);
    toast(result.message || successMessage);
    if (result.warning) toast(result.warning, "warning");
  } catch (err) {
    toast(`${failurePrefix}: ${err.message}`, false);
  } finally {
    button.disabled = false;
  }
}

function bindTopbarCommand(buttonId, url, successMessage, failurePrefix) {
  const button = document.getElementById(buttonId);
  if (!button) return;
  button.addEventListener("click", () => {
    // 確認の要否と文言はテンプレートが決める（付いていないボタンは即実行）
    const confirmation = button.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) return;
    postTopbarCommand(button, url, successMessage, failurePrefix);
  });
}

bindTopbarCommand(
  "estop",
  "/api/emergency-stop",
  "緊急停止を送信しました",
  "緊急停止失敗"
);
bindTopbarCommand(
  "firmware-restart",
  "/api/firmware-restart",
  "ファームウェア再起動を送信しました",
  "ファームウェア再起動失敗"
);
