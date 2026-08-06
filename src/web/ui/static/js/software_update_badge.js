"use strict";

(() => {
  const badge = document.getElementById("software-update-badge");
  if (!badge) return;

  async function refresh() {
    try {
      const local = await window.webui.frontendJson("/api/software-update");
      let machine = null;
      if (document.body.dataset.machineBase) {
        machine = await window.webui.api("GET", "/api/software-update");
      }
      const status = machine?.can_apply || machine?.branch_change ? machine : local;
      badge.hidden = !(status.can_apply || status.branch_change);
      badge.textContent = status.message;
      badge.title = status.message;
    } catch (_error) {
      // badge は補助通知。poll 失敗で他の操作を妨げない。
    }
  }

  refresh();
  window.setInterval(refresh, 30000);
})();
