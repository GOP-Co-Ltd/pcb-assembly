# paste-solder-integration (Phase 5)

計画書: `~/.claude/plans/claude-webui-1-pad-extract-eager-pine.md` Phase 5
ブランチ: feature/20260615/paste-solder-integration

## やったこと

paste_solder ジョブを「基板ごとの pad 有効/無効 + 階層 override 塗布設定」で
塗布するよう統合した。Phase 1〜4 の公開 IF（build_pad_hierarchy /
resolve_pad_settings / base_override_from_config / PasteSettingsModel /
ResolvedPaste / PasteApplicator.apply の per-pad 引数 / BoardSettingsStore）を
そのまま利用。

### 変更ファイル

- `src/webui/jobs/context.py`
  JobContext.__init__ に keyword（デフォルト付き）`machine_name=""` /
  `source_pcb=None` / `board_store=None` を追加し、同名 public プロパティを追加。
  `from webui.board_settings import BoardSettingsStore` を追加（循環なし）。
- `src/webui/jobs/manager.py`
  __init__ で `self._board_store = BoardSettingsStore(settings.data_dir)`。
  start の JobContext 生成に machine_name / source_pcb（selected_pcb.as_posix()）
  / board_store を配線。
- `src/webui/jobs/pasting.py`
  `_resolve_paste_model(ctx)`（フォールバック付きモデル取得）と
  `_is_pad_enabled(pad, resolved)`（階層除外 pad は後方互換で有効扱い）を新設。
  `_run_paste_solder` を改修（下記フロー）。
- `tests/webui/jobs/test_context.py`
  TestBoardSettingsWiring を追加（新プロパティが manager から渡る／未選択で
  source_pcb=None）。

### _run_paste_solder 新フロー

1. setup → PasteSession.from_calibration
2. `build_pad_hierarchy(session.pcb.components, session.pcb.pads)`（全 pad）
3. `_resolve_paste_model(ctx)` → `resolve_pad_settings` で resolved を得る
4. 有効 top pad のみ抽出（resolved 不在 or .enabled True）。無効件数を log。
5. 銅箔照合は有効 pad を 1 つ以上持つ部品 group のみに絞る
6. 補正適用して `(polygon, ResolvedPaste|None)` ペア化 → sort_by_nearest
   （key は pair[0].centroid）
7. 塗布ループは r=None なら applicator.apply([poly])、r ありなら 7 項目を渡す
8. summary: 「塗布 有効 N / 全 M pads（無効 K 件スキップ・押出合計…）」

## 判断ログ・計画逸脱

- spec-test-author は本タスク（paste-solder-integration）には engage して
  いない（spec-test-author dir に該当メモ無し）ため、tests を本 agent が記述。
- `_resolve_paste_model` / `_is_pad_enabled` は private ヘルパ。testing-strategy
  に従い private を直接テストしない。pad フィルタ/解決ロジックは Phase 1〜3 の
  unit（resolve_pad_settings / build_pad_hierarchy / BoardSettingsStore）で
  カバー済み。本 agent では「JobContext 新プロパティの manager 配線」を公開
  挙動として検証するに留めた。
- 無効 pad 除外・resolved 設定の apply 引数反映は実機ジョブ（カメラ + Klipper、
  @mark_hardware / WebUI 手動 E2E）に委ねる。fake HAL での全フロー再現は既存
  paste_solder テストにも seam が無く、過剰モックを避けた（計画書の許容範囲）。
- 後方互換: board_store/source_pcb 未配線 or 設定ファイル不在時は machine.toml
  デフォルトで base_enabled=True の全 pad 有効 = 現行等価。
- IF 変更通知: なし（JobContext は keyword default 追加のみ、既存呼び出し無風）。

## 検証結果

- make format: パス
- make type: パス（0 errors）
- uv run pytest tests/webui -q -m "not hardware": 405 passed / 18 deselected
- uv run pytest tests/pcbasm -q -m "not hardware": 681 passed / 16 deselected
