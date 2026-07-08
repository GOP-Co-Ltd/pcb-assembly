# webui リファクタ Group D（フロントエンド）

計画: `/home/gop/.claude/plans/fable-webui-pcbasm-webui-flake-webui-starry-quasar.md` の
Group D 節（D-1〜D-5）。4 コミットで実装（FE-1 共有基盤 / FE-2 pad_editor 分割 /
FE-3 thin-wrapper 是正 / FE-4 テンプレート整理。FE-5 = CSS 分割しないは判断のみ）。

## 計画外の判断ログ

1. **FE-3c の e2e テストは実機区分（`@mark_hardware`）に振り替えた**。
   計画は「不正値送信でログに理由が出る e2e を 1 本追加」だったが、
   `_run_loading_loop` へ到達するには loading ジョブが実 Moonraker へ接続し
   `PasteApplicator.__enter__`（AirPump ON + Stepper Enable）を実行する必要がある。
   e2e（make test-e2e）は kurousagi 設定で実 Moonraker に届く環境があり、
   自動テストのたびに実機のエアポンプが作動するのは不可。fake Klipper も無い
   （HAL ABC でないためモック禁止対象）。代替:
   - unit: `TestParseLoadingCommand` で InvalidLoadingCommand の区分・理由文言をピン
   - 実機: `TestPastingHardware.test_loading_invalid_value_logs_reason_and_continues`
     （不正値 → 理由ログ・押出なし・ループ継続。ユーザー実行）
2. **own_summary.fields の順序用に `UI_FIELD_ORDER` を pasting_view.py に新設**。
   `PASTE_OVERRIDE_FIELDS` は paste_height が ul_per_mm2 に先行し、JS
   `pad_editor/model.js` の FIELDS（UI 列順）と並びが異なるため流用できない。
   `test_own_summary_field_order_pins_js_fields_contract` で順序と集合を二重ピン。
3. **parse_loading_command は「値欠落」も InvalidLoadingCommand 扱いに変更**
   （match パターンを type のみで捕捉し、値は `command.get()` で検証）。
   従来は amount 欠落 → None →「未知のコマンドです: 'extrude'」という誤解ログだった。
4. **FE-4 の唯一の DOM 差分**: param_field マクロ統一により、job_form 経由ページでも
   runtime_editable=True の number 入力に `data-runtime-editable="true"` が付く。
   該当は dev の job_demo（live_value）のみで、dispense_runtime_params.js は
   dispense-calibration-form ゲートがあるため挙動影響なし。
5. FE-1 で `debounce()` 化した `scheduleFetch`（loading_controls.js）は
   const 化により hoist されないため、`bindMassCalibration()` 呼び出しより
   前方（computed 宣言直後）に定義を置いた（TDZ 回避）。
6. preview.js の pagehide から debounce タイマーの clearTimeout を削除
   （`connect()` が `stopped` でガード済みのため挙動同値）。

## 他 implementer への IF 変更通知

- `webui.jobs.pasting.parse_loading_command` の戻り値が
  `LoadingAction | InvalidLoadingCommand | None` に変更（呼び出し 2 箇所は追従済み）。
- `HierNodeInfo` に `own_summary: OwnSummary` を追加（契約は追加のみ・非破壊）。
- `window.webui` に svgEl / debounce / createBackoff / formatPosition、
  `window.webui.jobs` に isActive / commandReady / sendCommandOrToast を追加。
- 静的アセット: `js/pad_editor.js` → `js/pad_editor/index.js`（+ 新規 table.js）。

## 既知の制約・残課題

- 実機確認（ユーザー）: loading 画面での不正値送信 → ジョブコンソールに理由が
  出ること（トーストは出なくなった）。`make test`（hardware 含む）の
  `test_loading_invalid_value_logs_reason_and_continues` も未実行。
- ブラウザ体感確認（ユーザー）: pad editor（テーブル編集・バッジ・順路/塗布パス
  描画）、質量キャリブ適用ボタン、ジョグパッド SVG。

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（1481 passed）
- make test-e2e: pass（41 passed。コミット 2/3/4 後に各実行）
- make test（hardware）: 実行しない（実機はユーザー確認）
- `grep -rn '</content>' src tests`: クリーン（各コミット前に確認）
