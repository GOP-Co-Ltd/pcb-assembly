"use strict";

// 通知音ページ。設定の初期表示・音量の % 表示・テスト再生を行う。
// 保存は settings.js（form[data-machine-settings]）が行う。

(() => {
  const { toast, api } = window.webui;
  const deviceSelect = document.getElementById("audio-device");
  const volumeRange = document.getElementById("audio-volume");
  const volumeOutput = document.getElementById("audio-volume-value");
  const buttons = document.querySelectorAll("[data-sound]");
  if (deviceSelect === null || volumeRange === null || volumeOutput === null) return;

  function showVolume() {
    volumeOutput.textContent = `${Math.round(Number(volumeRange.value) * 100)}%`;
  }

  async function load() {
    try {
      const data = await api("GET", "/api/audio/settings");
      deviceSelect.replaceChildren(
        ...data.devices.map((device) => {
          const option = document.createElement("option");
          option.value = device.name;
          option.textContent = device.label;
          return option;
        })
      );
      deviceSelect.value = data.device;
      volumeRange.value = data.volume;
      showVolume();
    } catch (err) {
      toast(err.message, false);
    }
  }

  volumeRange.addEventListener("input", showVolume);

  for (const button of buttons) {
    button.addEventListener("click", async () => {
      for (const item of buttons) item.disabled = true;
      try {
        const result = await api("POST", "/api/audio/test", {
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

  load();
})();
