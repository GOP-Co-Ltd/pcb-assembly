# webui リファクタ Group A: flaky 根絶

ブランチ: `fix/20260707/webui-flaky`（main から分岐）。4 コミット構成。

## 実施内容

1. `fix(webui)` 28ffa3d: WS subscribe を accept 前へ / lifespan に
   preview.request_shutdown / FrameHub.stop(timeout=5.0) /
   test-fixture klipper host を 127.0.0.1 に
2. `test(webui)` 341cddb: PreviewService に clock 注入（override 期限計算・判定のみ）、
   TTL 系 3 テストを _ManualClock.advance() で決定化
3. `test(e2e)` 98ada49: Chromium を session スコープ `_browser` に分離（browser_page は
   per-test context）、hover 退避を pad-editor-toolbar に置換、_VIEWPORTS を
   desktop+mobile に削減、browser マーカー自動付与、pytest-timeout 導入
   （make test-e2e のみ --timeout=180）、無効な asyncio_default_fixture_loop_scope 削除
4. `test(e2e)` 28e4e2b: height 手動入力テストの UI 再描画レース修正（下記）

## 計画外の判断ログ

- **コミット 4（計画外だが受け入れ条件由来）**: e2e 3 連続検証の 1 回目で
  `test_dispense_mode_and_height_controls_persist_after_reload` が
  `height_input.fill` の 30s timeout で fail。原因は既存のテストレース:
  `_wait_for_node_override_value`（サーバー API ポーリング）は JS の
  patchNode 応答 → reloadConfig → renderTable 完了と順序保証がなく、
  "auto" 保存の全再描画が "manual" 選択の後に到着すると input が hidden な
  新要素へ差し替わる。再描画後にのみ存在する
  `pad-own-override-marker[data-field="paste_height"]` の出現待ちを挿入して決定化
  （src は無変更、テストのみ）。単体 3 連続 + フルスイート 3 連続で検証済み
- 細部の確定:
  - hover 退避先は設計メモの例示どおり `_testid("pad-editor-toolbar")` を採用
    （`partials/pad_editor.html:9` に実在、tree 行・SVG ビューアと重ならないことを確認）
  - `configs/test-fixture/machine.toml` の host 行に IPv6 フォールバック回避の
    理由コメントを 1 行追加（「周辺コメントも整合させる」指示の範囲内）
  - browser マーカー付与は `item.path.is_relative_to(e2e_dir)` チェックの内側に
    置いた（browser_page fixture は e2e 専用のため実質同義だが安全側）

## 既知の制約・残課題

- `make test`（hardware 含む）と実機・ブラウザ体感確認はユーザー領分（未実行）
- Group B（`refactor/20260707/pcbasm-paste-helpers`）は本ブランチに積む

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（1443 passed）
- make test-e2e × 3 連続: 全 3 回 41 passed（各 41s。session ブラウザ化前は ~70s+）
  - run1: exit=0 41 passed in 39.11s
  - run2: exit=0 41 passed in 38.75s
  - run3: exit=0 41 passed in 39.00s
- browser マーカー分離: `-m "e2e and browser"` 24 本 / `-m "e2e and not browser"` 17 本
- `grep -rn '</content>' src tests configs`: 混入なし
