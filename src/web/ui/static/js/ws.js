"use strict";

// backend の WS（/api/jobs/ws）の唯一のクライアント。切れたら指数バックオフで張り直す。
// 機体のページは job_console.js が 1 本、機体一覧は machines.js が行ごとに 1 本使う。
// 再接続のあいだのイベントは届かないので、受け手は onOpen で状態を取り直す。

(() => {
  const { createBackoff, withBase } = window.webui;

  // base を渡すと表示中の機体ではなくその機体へつなぐ（機体一覧の行用）
  function connectEvents({ base, onOpen, onEvent, onClose }) {
    const backoff = createBackoff(1000, 15000);
    let socket = null;

    function connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const connection = new WebSocket(`${proto}://${location.host}${withBase("/api/jobs/ws", base)}`);
      socket = connection;
      // 同期の HTTP 応答が届くまでに張り直していたら、古い応答で上書きしない
      const isCurrent = () =>
        socket === connection && connection.readyState === WebSocket.OPEN;
      connection.addEventListener("open", () => {
        backoff.reset();
        onOpen?.(isCurrent);
      });
      connection.addEventListener("message", (event) => {
        if (socket === connection) onEvent?.(JSON.parse(event.data));
      });
      connection.addEventListener("close", () => {
        if (socket !== connection) return;
        socket = null;
        onClose?.();
        setTimeout(connect, backoff.next());
      });
    }

    connect();
    return {
      // 開いていなければ送らずに false を返す
      send(message) {
        if (socket === null || socket.readyState !== WebSocket.OPEN) return false;
        socket.send(JSON.stringify(message));
        return true;
      },
    };
  }

  window.webui.connectEvents = connectEvents;
})();
