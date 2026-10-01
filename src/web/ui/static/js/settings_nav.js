"use strict";

// 設定ページのセクション切替と絞り込み。表示の切り替えだけを行い、保存は settings.js が行う。

(() => {
  const nav = document.querySelector(".settings-nav");
  const filter = document.getElementById("settings-filter");
  if (!nav || !filter) return;

  const items = Array.from(nav.querySelectorAll(".settings-nav-item"));
  const panels = Array.from(document.querySelectorAll(".settings-section"));
  const groups = Array.from(document.querySelectorAll(".settings-group"));
  const rows = Array.from(document.querySelectorAll("tr[data-search]"));
  const emptyNotice = document.getElementById("settings-filter-empty");

  // 絞り込み中はどのセクションも選択表示にならないので、選択は DOM ではなくこの変数で覚える
  let activeSlug = items[0]?.dataset.section;

  function selectSection(slug) {
    if (items.some((item) => item.dataset.section === slug)) activeSlug = slug;
    for (const item of items) {
      // 空文字の aria-current は "false" 扱いになるので、付けるときは "true"
      if (item.dataset.section === activeSlug) {
        item.setAttribute("aria-current", "true");
      } else {
        item.removeAttribute("aria-current");
      }
    }
    for (const panel of panels) panel.hidden = panel.dataset.section !== activeSlug;
  }

  function applyFilter(query) {
    for (const row of rows) row.hidden = !row.dataset.search.includes(query);
    for (const group of groups) {
      group.hidden = group.querySelector("tr[data-search]:not([hidden])") === null;
    }
    let matched = 0;
    for (const panel of panels) {
      const hit = panel.querySelector(".settings-group:not([hidden])") !== null;
      panel.hidden = !hit;
      if (hit) matched += 1;
    }
    for (const item of items) {
      const panel = panels.find((el) => el.dataset.section === item.dataset.section);
      item.classList.toggle("is-empty", panel === undefined || panel.hidden);
      item.removeAttribute("aria-current");
    }
    if (emptyNotice) emptyNotice.hidden = matched > 0;
  }

  function clearFilter() {
    for (const row of rows) row.hidden = false;
    for (const group of groups) group.hidden = false;
    for (const item of items) item.classList.remove("is-empty");
    if (emptyNotice) emptyNotice.hidden = true;
  }

  function showSection(slug) {
    // 絞り込み中にセクションを選んだら、そのセクションの項目をすべて表示する
    filter.value = "";
    clearFilter();
    selectSection(slug);
  }

  for (const item of items) {
    item.addEventListener("click", () => {
      showSection(item.dataset.section);
      location.hash = activeSlug;
    });
  }

  filter.addEventListener("input", () => {
    const query = filter.value.trim().toLowerCase();
    if (query === "") {
      clearFilter();
      selectSection(activeSlug);
      return;
    }
    applyFilter(query);
  });

  // 戻る / 進むでもセクションが切り替わるようにする（選択は hash に載る）
  window.addEventListener("hashchange", () => showSection(location.hash.slice(1)));

  selectSection(location.hash.slice(1));
})();
