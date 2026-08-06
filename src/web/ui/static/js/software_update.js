"use strict";

(() => {
  function render(card, status) {
    card.querySelector("[data-update-message]").textContent = status.message;
    card.querySelector("[data-update-branch]").textContent = status.branch || "—";
    card.querySelector("[data-update-previous]").textContent = status.previous || "—";
    card.querySelector("[data-update-revision]").textContent = status.available || "—";
    card.querySelector("[data-update-phase]").textContent = status.phase;
    const blockers = card.querySelector("[data-update-blockers]");
    blockers.replaceChildren(
      ...status.blockers.map((message) => {
        const item = document.createElement("li");
        item.textContent = message;
        return item;
      })
    );
    const apply = card.querySelector("[data-update-apply]");
    apply.disabled = !(status.can_apply || status.branch_change);
    apply.dataset.branch = status.branch || "";
    apply.dataset.revision = status.available || "";
    apply.dataset.branchChange = String(status.branch_change);
  }

  async function request(card, suffix, body) {
    return window.webui.frontendApi(
      "POST",
      `${card.dataset.endpoint}/${suffix}`,
      body
    );
  }

  async function refresh(card) {
    render(card, await window.webui.frontendApi("GET", card.dataset.endpoint));
  }

  for (const card of document.querySelectorAll("[data-update-card]")) {
    card.querySelector("[data-update-check]").addEventListener("click", async () => {
      try {
        await request(card, "check");
        await refresh(card);
      } catch (error) {
        window.webui.toast(error.message, false);
      }
    });
    card.querySelector("[data-update-apply]").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      if (!window.confirm("このサービスを更新して再起動しますか？")) return;
      let branchConfirmation = null;
      if (button.dataset.branchChange === "true") {
        branchConfirmation = window.prompt(`branch名 '${button.dataset.branch}' を入力してください`);
      }
      try {
        await request(card, "apply", {
          expected_branch: button.dataset.branch,
          expected_revision: button.dataset.revision,
          confirmed: true,
          branch_confirmation: branchConfirmation,
        });
        await refresh(card);
      } catch (error) {
        window.webui.toast(error.message, false);
      }
    });
  }

  const cards = [...document.querySelectorAll("[data-update-card]")];
  window.setInterval(() => {
    for (const card of cards) refresh(card).catch(() => {});
  }, 30000);
})();
