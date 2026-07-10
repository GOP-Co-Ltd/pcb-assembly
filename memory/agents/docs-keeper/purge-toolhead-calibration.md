# docs-keeper: purge-toolhead-calibration

対象: feature/20260710/purge-toolhead-calibration の未コミット diff（パージ痕によるツールヘッドオフセット自動較正）。

## 変更したファイル

- `src/pcbasm/pasting/README.md` — 責務一覧に「ツールヘッドオフセットの計測（ペースト痕の円検出・補正量検証）」を 1 行追加。検出ロジック（`locate_paste_blob` / `validate_offset_correction`）が webui ジョブから pcbasm/pasting へ移ったため。

## 変更不要と判断したもの

- `configs/README.md` — machine.toml の例に `paste_dispenser` 系キーの記述がそもそも無く、今回新規設定キーも追加していない。追記は過剰。
- `src/webui/` — README が存在しない。新設はしない（要求外）。
- diff 内 docstring — `locate_paste_blob` / `validate_offset_correction` / `PasteApplicator.set_transform` / `ParamSpec.persist` / `_calibrate_toolhead_offset_from_purge` を実装と照合し不整合なし。`toolhead_offset.py` のモジュール docstring も implementer が更新済み。

## 検証

- `make format` 通過（結果は最終報告参照）
- 編集ファイル末尾の `</content>` 混入なしを確認
