"use strict";

// マシン選択ドロップダウン: 選択された遷移先へ移動し、一覧をサーバから取り直す。
// 表示名（label）・選択状態（current）はサーバが決めるので、ここでは option を
// 組み替えるだけ（表示規則を JS に複製しない）。
// タイマーポーリングはしない（ページロード後の 1 回と、タブが再表示された
// ときだけ取り直す。mDNS 探索は frontend の起動時から動いている）。

(() => {
  const select = document.getElementById("machine-select");
  if (!select) return;

  const currentId = select.dataset.machineId ?? "";
  const suffix = select.dataset.currentSuffix ?? "";
  const placeholder = select.querySelector("[data-placeholder]");

  select.addEventListener("change", () => {
    window.location.assign(select.value);
  });

  const render = (machines) => {
    // 未選択項目を残し、後続の option へサーバの current を反映する。
    placeholder.selected = true;
    select.replaceChildren(
      placeholder,
      ...machines.map((machine) => {
        const option = document.createElement("option");
        option.value = `/m/${machine.machine_id}/${suffix}`;
        option.textContent = machine.label;
        option.selected = machine.current;
        return option;
      }),
    );
  };

  const refresh = async () => {
    // frontend 自身のエンドポイント（machine prefix は付けない）
    const url = `/api/machines?current=${encodeURIComponent(currentId)}`;
    try {
      render((await window.webui.frontendJson(url)).machines);
    } catch {
      // 取り直しに失敗したら SSR 済みの option をそのまま残す
    }
  };

  refresh();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
})();
