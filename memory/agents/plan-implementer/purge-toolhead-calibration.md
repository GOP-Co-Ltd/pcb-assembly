# plan-implementer メモ — purge-toolhead-calibration

2026-07-10 実装完了。計画書のシグネチャは全て遵守（IF変更通知なし）。

## 変更ファイル（src/ のみ）

- `src/pcbasm/pasting/toolhead_offset.py` — `locate_paste_blob()` / `validate_offset_correction()` 追加
- `src/pcbasm/pasting/__init__.py` — 上記 2 関数を export
- `src/pcbasm/pasting/applicator.py` — `PasteApplicator.set_transform()` 追加
- `src/webui/jobs/catalog.py` — `ParamSpec.persist: bool = False` 追加
- `src/webui/templates/partials/job_params.html` — `spec.persist` で全 value_type に `data-persist="1"` 出力
- `src/webui/static/js/job_console.js` — `[data-persist]` の localStorage 復元/保存（キー `jobParam:<job>:<name>`、checkbox は "1"/"0"、それ以外は value 文字列）。フォーム submit ループの直前に配置
- `src/webui/jobs/pasting.py` — 定数 3 つ追加・paste_solder に `calibrate_toolhead_offset` ParamSpec（default True, persist）・`_run_toolhead_offset` の検出ブロックを `locate_paste_blob` に置換（挙動不変）・`_calibrate_toolhead_offset_from_purge()` 新設・`_run_paste_solder` 配線 + summary/artifact 追加

## 計画外の判断（軽微）

1. **orphan import の削除**: 検出ブロック移設で `time` / `CircleDetector` / `OffsetObserver` / `XYPositionAdjustor` が pasting.py で未使用になったため削除（自分の変更で生じた orphan）。`Image` は他で使用のため残置。`Shift` / `BoardCalibrationResult` を追加 import
2. **JS の非 bool persist 対応**: 計画は bool 想定だが `data-persist` は全型共通出力のため、JS は `input.type === "checkbox"` 分岐で checkbox 以外は `value` をそのまま保存/復元する汎用実装にした（ドメインロジックなし）
3. **スキップ分岐の位置**: `initial_purge_point` が None（未照合で center フォールバックした場合は非 None）になるのは initial_purge が None のときのみなので、「チェック ON だがパージ無効」の log は `elif calibrate_offset:` で実装
4. **summary の較正行**: 1 行追記ではなく既存 summary 末尾に ` / オフセット較正 X=... Y=... mm` を連結（JobResult.summary は単一文字列のため）

## 検証結果

- `make format` パス（docformatter/codespell の自動修正後に再実行でグリーン）
- `make type` パス（0 errors）
- `make test-no-hardware` 1520 passed / 78 deselected。既存回帰ゼロ。spec-test-author が並行追加した新テスト（TestSetTransform / TestValidateOffsetCorrection / TestLocatePasteBlob 等）も全てパス
- `make test`（実機）は禁止のため未実行。実機通し確認はユーザー担当

## 実装ノート

- pasting → posctrl の import（OffsetObserver / XYPositionAdjustor）は循環なし（posctrl は pasting を import していない、計画書どおり）
- ガードは `validate_offset_correction` → RuntimeError を machine.toml 書き込み**前**に判定
- machine.toml 反映は既存 `_apply_to_machine_toml`（round 6 桁 + ctx.log）を流用
- ラン内反映は `applicator.set_transform(Compose([board_transform, Shift(offset), height_plane]))`
- 検出失敗の RuntimeError は捕捉せず伝播（ジョブエラー中止）
- `</content>` 混入は全編集ファイルで無しを確認
