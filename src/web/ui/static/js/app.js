"use strict";

// 全ページ共通の JS。api.js の後に読み込み、共通の部品を window.webui に足す。
// HTTP と保存待ちは api.js、WS は ws.js が受け持つ。

// ---- shared helpers ----

// level: true=通常 / false=エラー / "warning"=警告
function toast(message, level = true) {
  const container = document.getElementById("toasts");
  if (!container) return;
  const el = document.createElement("div");
  const [className, testid] =
    level === "warning"
      ? ["toast warning", "toast-warning"]
      : level
        ? ["toast", "toast"]
        : ["toast error", "toast-error"];
  el.className = className;
  el.dataset.testid = testid;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 5000);
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

Object.assign(window.webui, { toast, svgEl, debounce, createBackoff });

// ---- topbar safety controls ----

async function postTopbarCommand(button, url, successMessage, failurePrefix) {
  button.disabled = true;
  try {
    // 何を再起動したかはサーバが文言まで組んで返す（組み立てを JS に複製しない）。
    // 文言を返さないコマンド（緊急停止）では呼び出し側の既定文言を使う
    const result = await window.webui.api("POST", url);
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
  "/api/machine/firmware-restart",
  "再起動（復旧）を送信しました",
  "再起動（復旧）失敗"
);
