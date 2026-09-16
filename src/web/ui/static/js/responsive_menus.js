/* 狭い画面では共通操作と機能一覧を閉じる。開閉は native details に任せる。 */
(() => {
  document.querySelectorAll("[data-responsive-menu]").forEach(menu => {
    const narrow = window.matchMedia(menu.dataset.responsiveMenu);
    const resize = () => { menu.open = !narrow.matches; };
    narrow.addEventListener("change", resize);
    resize();
  });
})();
