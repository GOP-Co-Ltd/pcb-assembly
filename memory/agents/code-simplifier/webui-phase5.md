# WebUI Phase 5（pasting ジョブ + 仕上げ）の簡素化

対象: 5da6d1f^..db9e663 の src 範囲。plan-implementer ノート
`memory/agents/plan-implementer/webui-phase5.md` を前提に実施。

## 簡素化した内部実装

- `src/webui/jobs/pasting.py`
  - `parse_loading_command`: extrude / suck の重複 2 分岐を
    `case {"type": "extrude" | "suck" as kind, ...}` に統合（`_positive_amount`
    呼び出しの重複削除）
  - `_setup_calibration(ctx, camera, tolerance)` を新設し、
    `progress("セットアップ")` + `setup_board_calibration(machine=…, pcb_file_path=…,
    camera=…, frame_sink=ctx.frame)` の定型 3 箇所（paste_solder / height_plane /
    toolhead_offset）を集約。assert（requires_pcb）もヘルパ内に移し、
    paste_solder / toolhead_offset の関数頭の assert を削除
  - `_dispenser_rig` が `PasteApplicator.from_config` まで行い
    `(klipper, stage, applicator)` を返すよう変更。`_run_loading` /
    `_run_flow_calibration` の from_config 重複呼び出しを削除
- `src/webui/routers/pages.py`: pasting コンテキストの連続 `context.update` 3 回を
  1 回の update + 条件付き 1 キー代入に統合
- `src/webui/static/js/loading_controls.js`: ボタンの `getElementById` 二重取得
  （配列構築用 + addEventListener 用）を `[id, type]` テーブル 1 ループに統合。
  `job !== null && job !== undefined` → `job != null`

## 変更を見送った部分・理由

- `jobs/manager.py` のログブリッジ（attach/detach、worker スレッド判定）:
  タスク制約どおり並行性まわりは不変。contextmanager 化も検討したが見送り
- `_run_toolhead_offset` の手書きリトラクション gcode を `applicator.retract()` に
  置換しようとして**取り消した**。`PasteApplicator._retraction_accel` は
  `factor * rate^2 / retraction` で、ジョブ／script の手書き
  `dispense_accel * retract_accel_factor` とは加速度が異なる（gcode が変わる）。
  scripts と同一挙動を維持するため現状の手書きを温存
- `machine_commands.py` の jog 分岐（軸ごとの条件式）: `**{axis: distance}` 化は
  pyright の型検査（`relax: bool` への代入可能性）を通らないため現状維持
- `height_render.py` / `sampling.py` / `applicator.from_config` /
  `job_console.js`: すでに最小構成と判断し変更なし
- `register_pasting_jobs` の 6 連 `catalog.register`: 宣言的データであり
  ループ化はかえって読みにくいため現状維持

## 公開IF維持の確認

- 変更はすべて私的ヘルパ（`_` prefix）・JS 内部・テンプレートコンテキスト構築のみ。
  計画書の確定シグネチャ（`LOADING_STAGE` / `parse_loading_command` /
  `register_pasting_jobs` / `Extrude` / `Finish` / `handle_machine_command` 等）不変
- テスト 1012 passed（非 hardware、34 deselected）で公開契約のピンを全通過

## 検証結果

- pre-commit（変更 3 ファイル）: pass（docformatter の自動修正 1 件を取り込み済み）
- uv run pyright src/: pass（0 errors）
- uv run pytest tests -m "not hardware" -q: pass（1012 passed）
- git commit は未実行（タスク制約）
