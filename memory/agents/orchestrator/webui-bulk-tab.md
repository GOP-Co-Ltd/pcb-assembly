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

## 段階 4 レビュー（code-reviewer、request-changes → 対応）

- M1 対応: 行が WS を張らないと保持者の接続 0 本で 30 秒後に操作権が失効する。
  bulk.js で行ごとに /m/{id}/api/ws を張り続け、control_changed / state_changed と再接続で
  再取得する方式に変更（ポーリング廃止）。回帰 E2E は backend の control.connections >= 1 を確認
  （修正前の bulk.js で落ちることを確認済み）
- S2 対応: WS open で更新要約も取り直す（不通からの復帰・更新後の再起動から戻ったとき）
- S3 対応: 最初の応答まで SSR の「確認中…」を残す。不通時の PCB 欄は「---」
- nit 対応: /bulk は render_standalone を使う
- S1（更新の解釈）: 計画どおりソフトウェア更新のまま。PR とユーザー報告で明示する
- S4 却下: /bulk はマシン非依存で BASE が空であることがページの前提。app.js に入口を足すほどの重複ではない
- S5 却下: test_control_ui.py 等と同じく機能単位のファイルにする（test_pages.py は既に 1000 行超）
- S6 見送り: fake 環境で paste_solder を開始すると Klipper 不通で即失敗し、開始の確認として弱い
- nit 見送り: 「PCB未選択」文言重複、mDNS 追加の即時反映、persisted_params 将来対応
