"use strict";

// 校正名のテキスト入力を、保存済み校正の <select> に差し替える。
// 対象は 2 つ:
//   - 収集ジョブの volume_calibration パラメータ（#param-volume_calibration）
//   - はんだ塗布ページの流量キャリブレーション設定（[data-calibration-picker]）
// 選択肢のラベルと詳細行は /api/pasting/paste-volume/calibrations が組み立て済みの
// 文字列をそのまま流す（ここで連結・整形しない）。
//
// 一覧が取れなかったときはテキスト入力のまま残す。差し替えてから失敗すると、
// 選択肢の無い <select> だけが残って名前を入れる手段が消える。
(() => {
  const { api } = window.webui;

  function targets() {
    const found = [];
    const param = document.getElementById("param-volume_calibration");
    if (param) found.push({ input: param, empty: "検証しない", param: true });
    for (const input of document.querySelectorAll("[data-calibration-picker]")) {
      found.push({
        input,
        empty: input.dataset.calibrationEmpty || "設定しない",
        param: false,
      });
    }
    return found;
  }

  function option(value, text) {
    const element = document.createElement("option");
    element.value = value;
    element.textContent = text;
    return element;
  }

  function build(target, entries) {
    const { input, empty, param } = target;
    // 復元済みの前回の選択。差し替えで捨てない
    const saved = input.value;
    const select = document.createElement("select");
    select.id = input.id;
    select.name = input.name;
    if (param) {
      select.dataset.paramType = "str";
      select.dataset.paramOptional = "true";
    } else {
      select.dataset.type = input.dataset.type || "str";
      // 空の選択肢は「補正しない」を意味するので、空文字として保存させる
      select.dataset.allowEmpty = "true";
    }
    select.replaceChildren(option("", empty));
    for (const entry of entries) {
      select.append(option(entry.name, entry.option_label));
    }
    if (saved && !entries.some((entry) => entry.name === saved)) {
      // 保存済みの校正が消えている。黙って空へ落とさず、選択を見せる
      select.append(option(saved, `${saved}（見つかりません）`));
    }
    select.value = saved;
    return select;
  }

  function describeElement() {
    const details = document.createElement("p");
    details.className = "job-param-help";
    details.dataset.testid = "volume-calibration-details";
    return details;
  }

  function swap(target, entries) {
    const select = build(target, entries);
    const details = describeElement();
    const byName = new Map(entries.map((entry) => [entry.name, entry]));
    const describe = () => {
      details.textContent = byName.get(select.value)?.details ?? "";
      // 幅が足りず省略されたときのために全文をツールチップへ残す
      details.title = details.textContent;
    };
    select.addEventListener("change", describe);
    describe();
    target.input.replaceWith(select);
    select.after(details);
  }

  async function load() {
    const found = targets();
    if (found.length === 0) return;
    let data;
    try {
      data = await api("GET", "/api/pasting/paste-volume/calibrations");
    } catch (error) {
      for (const target of found) {
        const note = describeElement();
        note.textContent = error.message;
        target.input.after(note);
      }
      return;
    }
    const entries = data.calibrations ?? [];
    for (const target of found) swap(target, entries);
  }

  load();
})();
