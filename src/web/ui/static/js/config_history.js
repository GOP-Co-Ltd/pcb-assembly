"use strict";

// 設定履歴ページ。世代の一覧を描き、選んだ世代を復元する。
// 時刻と変更の文字列はサーバーが組んだものをそのまま出す。

(() => {
  const { toast, api } = window.webui;
  const list = document.getElementById("config-history-list");
  const empty = document.getElementById("config-history-empty");
  if (list === null || empty === null) return;

  function render(entries) {
    empty.hidden = entries.length > 0;
    list.replaceChildren(
      ...entries.map((entry) => {
        const item = document.createElement("li");
        const time = document.createElement("strong");
        time.textContent = entry.time_label;
        const changes = document.createElement("ul");
        changes.className = "config-history-changes";
        changes.replaceChildren(
          ...entry.changes.map((change) => {
            const line = document.createElement("li");
            line.textContent = change;
            return line;
          })
        );
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "この変更の前に戻す";
        button.addEventListener("click", () => restore(entry, button));
        item.append(time, " ", button, changes);
        return item;
      })
    );
  }

  async function restore(entry, button) {
    if (!window.confirm(`${entry.time_label} の変更より前の内容に戻しますか？`)) return;
    button.disabled = true;
    try {
      const data = await api(
        "POST",
        `/api/backup/history/${encodeURIComponent(entry.name)}/restore`
      );
      render(data.entries);
      toast(`${entry.time_label} の変更より前の内容に戻しました`);
    } catch (err) {
      toast(err.message, false);
      button.disabled = false;
    }
  }

  async function load() {
    try {
      render((await api("GET", "/api/backup/history")).entries);
    } catch (err) {
      toast(err.message, false);
    }
  }

  load();
})();
