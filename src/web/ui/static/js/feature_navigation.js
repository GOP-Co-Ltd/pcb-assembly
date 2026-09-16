/* 狭い画面では機能一覧を閉じて本文を先に見せる。開閉は native details に任せる。 */
(() => {
  const menu = document.querySelector(".feature-menu");
  if (!menu) return;
  const narrow = window.matchMedia("(max-width: 760px)");
  const resize = () => { menu.open = !narrow.matches; };
  narrow.addEventListener("change", resize);
  resize();
})();
