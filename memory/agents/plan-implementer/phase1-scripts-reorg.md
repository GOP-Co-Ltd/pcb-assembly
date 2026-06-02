# Phase 1 実装ノート (plan-implementer / src 専任)

ブランチ: `refactor/20260527/phase1-scripts-reorg`
スコープ: `src/` のみ。`tests/` には一切触れていない。
コミット: **未実施**（Claude main が統合検証後にまとめてコミットする方針）。

## 1. scripts サブディレクトリ化（git mv 15 本）

計画書「ファイル移動の完全対応表」の通り実行。全 15 ファイルが git の rename として検出済み。

- `mkdir -p src/scripts/{posctrl,pasting,pnp,dev}`
- posctrl/ ← board_tour, camera_calibration, camera_preview, circle_detection_demo, orthogonality_test
- pasting/ ← paste_solder, paste_flow_calibration, paste_loading, pasting_toolhead_offset, pasting_height_plane
- dev/ ← extract_pcb, fill_path_simulate, generate_grid_pcb, klipper_demo, stage_demo
- pnp/ ← 空（mkdir のみ。git は空ディレクトリを追跡しないため commit 対象に出現しない。計画想定内）

検証:
- `ls src/scripts/*.py` → 直下に py 残存なし（OK）
- `git status --short` → 15 件すべて `R`（rename）で検出
- `__init__.py` は置いていない（namespace package のまま）

namespace package 解決の実証:
- `uv run python -m scripts.dev.generate_grid_pcb --help` → 正常に usage 出力（exit 0）。
  → spec-test-author が tests 側を `scripts.dev.generate_grid_pcb` に書き換えれば import 解決する裏付け。

## 2. paste_solder.py 分解（src/scripts/pasting/paste_solder.py）

`main()` を以下に分解。同一ファイル内で完結（パッケージ抽出はしない＝YAGNI / 計画指定通り）。

- `@attrs.frozen class Env` — machine / klipper / stage / board_transform / toolhead_offset /
  height_measurer / paste_dispenser を保持。
- `_setup_environment(args) -> tuple[Env, BoardCalibrationResult]`
  — 現 main L68〜109（machine_session の外の初期化）を移設。`top_coppers`/`top_pads` 生成のため
    `result`(BoardCalibrationResult) も返す（計画書の推奨案 (a)）。
- `_measure_height(env, top_coppers: list[Copper]) -> HeightPlane`
  — 現 L114〜119。`"\n=== Height plane計測 ==="` print を含む。
    `Compose([env.board_transform, env.toolhead_offset])`。
- `_load_and_apply(env, height_plane, top_pads: list[Pad], args) -> None`
  — 現 L121〜164。nearest ソート → PasteApplicator → 任意ローディング →
    `"\n=== リトラクション ==="` → `"\n=== パッド塗布 (...) ==="`。
    `Compose([env.board_transform, env.toolhead_offset, height_plane])`。
- `main()` — argparse（引数定義・デフォルト・help 文言は無変更）→ `_setup_environment` →
  top_coppers/top_pads 生成 → `machine_session(env.klipper)` 内で try/except(KeyboardInterrupt) →
  `"\n=== 緊急停止 ===" / env.klipper.emergency_stop()`。

### 挙動等価チェック（すべて維持）
- print 文言・出力順・改行をそのまま写経。
- `machine_session` の境界（初期化＝外、計測・塗布＝内、KeyboardInterrupt＝内 try）を一致。
- `Compose([...])` の合成順を変えていない（measure: [board, toolhead] / 塗布: [board, toolhead, height]）。
- `WINDOW_NAME = "Paste Solder"` 維持。`setup_logging(logging.INFO)` は main 冒頭で維持。
- argparse: `--machine/-m`(pd_china_frame) `--pcb-file/-p`(required) `--tolerance/-t`(0.1)
  `--amount`(0.1) `--interactive-loading/-l` すべて同一。
- `--help` 出力が原版と一致することを `uv run python src/scripts/pasting/paste_solder.py --help` で確認。

### 確定した型 import 元（実シンボルを Read で確認）
- `Machine`, `get_machine_config` ← `pcb_assembly.config`
- `Klipper`, `XYZStage`, `PasteDispenser`, `Probe` ← `pcb_assembly.hal`
- `Transform`, `Compose`, `HeightPlane`, `Move`, `sort_by_nearest` ← `pcb_assembly.geometry`
  （HeightPlane は geometry の公開シンボル。height.py も geometry から import している）
- `HeightPlaneMeasurer` ← `pcb_assembly.control.adjust`
- `Copper`, `Pad`, `Layer` ← `pcb_assembly.pcb`
- `BoardCalibrationResult`, `machine_session`, `setup_board_calibration` ← `pcb_assembly.control.setup`
- `ProbeExecutor` ← `pcb_assembly.control.probe`
- `PasteApplicator`, `interactive_loading` ← `pcb_assembly.control.pasting`
- `gcode` ← `pcb_assembly`、`setup_logging` ← `pcb_assembly.utils`、`attrs`（新規追加）

型整合の根拠: `PcbFile.copper -> CopperList(UserList[Copper])`, `.pads -> PadList(UserList[Pad])`
なので `[c for c in result.pcb.copper if ...]` は `list[Copper]`、pads は `list[Pad]` に推論され、
`_measure_height` / `_load_and_apply` のシグネチャと整合（pyright エラー 0）。

## 3. パッケージ構造（pcb_assembly/）
一切変更していない。

## 計画外の判断
- なし。計画書通り。`top_coppers`/`top_pads` は推奨案 (a)（main に生成を残す）を採用。
- docformatter が paste_solder.py の docstring 先頭語を自動キャピタライズ＆リフロー
  （"height_measurer" → "Height_measurer" 等）した。これは format フックの標準挙動で
  runtime 挙動に影響なし。format は再実行で安定（全 Passed）。
- IF 変更通知: なし（公開 IF＝スクリプトの起動方法・argparse は不変。spec-test-author への
  影響は generate_grid_pcb の import パスが `scripts.dev.generate_grid_pcb` になる点のみで、
  これは計画書で既に spec-test-author の担当として明記済み）。

## 検証結果
- `make format` → Passed（docformatter の自動修正後、再実行で全 Passed）
- `make type` → **0 errors**, 2 warnings。
  - warning 2 件は `tests/pcb_assembly/control/pasting/test_fill_path.py` の reportPrivateUsage で
    **本作業と無関係・未変更ファイル**（git status でも未変更）。src 側エラーなし。
- `make test-no-hardware` は計画指示により **実行していない**（test_generate_grid_pcb のパス追従を
  spec-test-author が並行作業中。統合検証は Claude main が両者完了後に実施）。

## 注意点（引き継ぎ）
- `tests/scripts/test_generate_grid_pcb.py` は spec-test-author が `scripts.dev.generate_grid_pcb` へ
  書き換える必要がある（import 10 箇所 + sys.modules キー 1 箇所）。これが未完だと
  `make test-no-hardware` が collection error になる。
- `pnp/` は git に出現しない（空ディレクトリ）。異常ではない。
