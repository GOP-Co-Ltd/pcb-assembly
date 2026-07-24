"use strict";

(() => {
  const { toast, api } = window.webui;
  const buttons = document.querySelectorAll("[data-sound]");

  for (const button of buttons) {
    button.addEventListener("click", async () => {
      for (const item of buttons) item.disabled = true;
      try {
        const result = await api("POST", "/api/dev/audio/test", {
          sound: button.dataset.sound,
        });
        toast(`通知音を再生しました: ${result.device}`);
      } catch (err) {
        toast(err.message, false);
      } finally {
        for (const item of buttons) item.disabled = false;
      }
    });
  }
})();
