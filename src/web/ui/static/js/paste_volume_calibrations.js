"use strict";

// 収集ジョブの volume_calibration 入力を、保存済み校正の <select> に差し替える。
// 選択肢のラベルと詳細行は /api/pasting/paste-volume/calibrations が組み立て済みの
// 文字列をそのまま流す（ここで連結・整形しない）。
//
// 一覧が取れなかったときはテキスト入力のまま残す。差し替えてから失敗すると、
// 選択肢の無い <select> だけが残って名前を入れる手段が消える。
(() => {
  const { api } = window.webui;

  const input = document.getElementById("param-volume_calibration");
  if (!input) return;

  // persisted_params で復元された前回の選択。差し替えで捨てない
  const saved = input.value;

  function option(value, text) {
    const element = document.createElement("option");
    element.value = value;
    element.textContent = text;
    return element;
  }

  function build(entries) {
    const select = document.createElement("select");
    select.id = input.id;
    select.name = input.name;
    select.dataset.paramType = "str";
    select.dataset.paramOptional = "true";
    select.replaceChildren(option("", "検証しない"));
    for (const entry of entries) {
      select.append(option(entry.name, entry.option_label));
    }
    if (saved && !entries.some((entry) => entry.name === saved)) {
      // 保存済みの校正が消えている。黙って「検証しない」へ落とさず、選択を見せる
      select.append(option(saved, `${saved}（見つかりません）`));
    }
    select.value = saved;
    return select;
  }

  async function load() {
    let data;
    try {
      data = await api("GET", "/api/pasting/paste-volume/calibrations");
    } catch (error) {
      const note = document.createElement("p");
      note.className = "job-param-help";
      note.dataset.testid = "volume-calibration-details";
      note.textContent = error.message;
      input.after(note);
      return;
    }
    const entries = data.calibrations ?? [];
    const select = build(entries);
    const details = document.createElement("p");
    details.className = "job-param-help";
    details.dataset.testid = "volume-calibration-details";

    const byName = new Map(entries.map((entry) => [entry.name, entry]));
    const describe = () => {
      details.textContent = byName.get(select.value)?.details ?? "";
    };
    select.addEventListener("change", describe);
    describe();

    input.replaceWith(select);
    select.after(details);
  }

  load();
})();
