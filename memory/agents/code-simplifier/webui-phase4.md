# code-simplifier: WebUI Phase 4（posctrl ジョブ + 表示責務分離）

対象: 903c1dc..9baf8a0 の src 範囲（公開 IF は
`memory/agents/implementation-planner/webui-phase4.md` の確定シグネチャを維持）。

## 実施した簡素化

### `src/webui/jobs/posctrl.py`（私的ヘルパのみ。公開 IF 不変）

1. **`_calibrated_board(ctx, camera)` を抽出**: board_tour / orthogonality_test が
   重複していた「pcb_path assert + progress("セットアップ") +
   `setup_board_calibration(...)`」ブロック（各 ~12 行）を 1 箇所に集約。
2. **`_move_to` に `speed` パラメータ追加**（既定 `Speed.absolute(30)`、不変）:
   補正巡回ループのインライン gcode 移動ブロックを `_move_to(result, target,
   speed=Speed.rate(0.5))` に置換。
3. **`_pad_renderer(result, session, projector, pads, position)` を抽出**:
   照合失敗時と補正巡回の 2 箇所にあった `PadResultRenderer` 構築
   （pads → roi_polygons=copper_polygon / paste_polygons=polygon の対応付け）を
   1 箇所に集約。`pad_align` ローカル変数も不要化。

### `src/pcbasm/posctrl/tour.py`

4. **`_move_to(result, machine_pt)` を抽出**: `display_at_point` /
   `interactive_display_at_point` が重複していた移動 + wait_for_done ブロックを
   集約。両関数の公開シグネチャは不変。

## 変更なしと判断した点

- `setup.py` / `pad.py` / `alignment.py` / `render.py` / `session.py`：
  既に線形・最小。`from_calibration` のラッパーは公開 IF（テストがピン）のため温存
- `context.py` / `manager.py` / `preview.py` の Phase 4 追加分：簡素化余地なし
- `PadResultRenderer.render` の全面 addWeighted → fill 限定演算化は
  数値挙動（丸め）が変わり得るため見送り（制約どおり）
- `_stream_labeled_frames` / `_stream_pad_result` の統合は lambda 間接化で
  かえって読みにくくなるため見送り
- テンプレート / JS（job_form.html 抽出済み等）：重複なし

## 検証

- `uv run pyright src/` → 0 errors
- `uv run pytest tests -m "not hardware" -q` → **928 passed**（維持）
- `uv run pre-commit run --files <変更 2 ファイル>` → 全フックパス
  （docformatter の自動整形を取り込み済み）

コミットはユーザー判断（未コミット、working tree に変更あり）。
