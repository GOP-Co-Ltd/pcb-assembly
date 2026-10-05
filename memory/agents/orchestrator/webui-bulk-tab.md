# WebUI 一括管理タブ

## 段階 1 計画

要求: WebUI に「一括管理」タブ。1 行 = マシン名 / 操作権の取得・解放 / 更新。
はんだペースト・PnP でセクション分け。ペーストのセクションでは行ごとに「塗布実行」
（その行のマシンで paste_solder を実行）。PnP は未実装表示。

解釈（自分で決めたもの）:
- 「更新」= ソフトウェア更新（/api/update/check → 更新ありなら確認して /api/update/run）。
  行ごとの状態再読込は定期ポーリングで行うので、ボタンにはしない
- セクション分けは frontend 登録（machines.toml / mDNS TXT）の machine_type で行う。
  backend へ問い合わせずに描けるので、1 台落ちていてもページが描ける。
  machine_type 不明の機体は「種別不明」セクション（該当がある場合のみ）に出す
  （黙って消えるのを避ける）
- 一括管理はマシン非依存ページ `/bulk`（`/update` と同じ扱い）。header に常に `/bulk` へのタブを出す
- 塗布実行は `POST /m/{id}/api/jobs/paste_solder {params:{}}`。paste_solder は
  persisted_params を持たないので、空 params = フォーム既定値と同じ
- 対象 PCB の取り違えを防ぐため、ペースト行に選択中 PCB を表示する（/api/state.pcb_file）
- 取得/解放のみ（奪取は出さない）。他端末が保持中なら保持者名を出し取得ボタンは無効
- 更新・塗布実行はその行の操作権を保持しているときだけ押せる

公開 IF:
- web.ui.layout: BULK_PATH, BULK_LABEL, BulkSection(title, machine_type, machines),
  bulk_sections(machines) -> tuple[BulkSection, ...]
- web.ui.pages: GET /bulk（/{tab} より前に登録）
- templates/bulk.html, static/js/bulk.js

テスト観点:
- bulk_sections: paste/pnp を常に出す、順序、種別不明は該当時のみ
- /bulk: 200、行・ボタン testid、PnP 未実装表示、backend 不通でも 200、header にタブ
- E2E: 行の取得 → 解放、PCB 未選択での塗布実行が backend の 400 を表示

## 段階 2〜3

- tests/web/ui/test_bulk.py（SSR / セクション分け）、tests/e2e/test_bulk_browser.py（取得・解放、塗布実行の 400）
- 計画外: tests/web/ui/test_pages.py の「内部リンクは全て machine prefix 付き」検査に
  BULK_PATH を例外追加。一括管理はマシン非依存ページで、prefix しないのが仕様のため
- format / type / test-no-hardware green、E2E（bulk + topbar）green
