# docs-keeper: 吐出量キャリブレーション刷新

入力: ブランチ `feature/20260624/dispense-calibration`、コミット ee9ebde（刷新）/
431fb6b（内部簡素化）。前段メモ `memory/agents/plan-implementer/dispense-calibration-{algo,base,webui}.md`、
`memory/agents/code-simplifier/dispense-calibration-{pcbasm,webui}.md`。

## 変更要旨（事実）

- 新ジョブ `dispense_calibration`（webui pasting タブ）に一本化。銅板へ線を引き
  ①rotations_per_ul（+連動 dispense_accel）→ ②max_dispense_rate → ③max_fill_speed を
  メニュー駆動で確定。検証ループ付き。
- 旧 `flow_calibration` ジョブ / loading 質量キャリブ表 /
  `GET /api/pasting/loading/calibration` を廃止。
- `config.PasteDispenser.fill_speed` → `max_fill_speed`（装置一律の連続塗布速度上限）。
  pad ごとの速度 override（settings の fill_speed）は廃止。
  `effective_fill_speed = min(max_fill_speed, max_dispense_rate/q)` は旧
  `fill_speed_actual` と数学的に同値。
- 新規 `pcbasm/pasting/dispense_calibration.py`（LineLayout / DispenseRateCalibration /
  FillSpeedSweep / RotationsPerUlRound / slot_area / dispense_rate_schedule /
  fill_speed_schedule）。
- `FillSequence.rate_cap`、`applicator.draw_line`、`FlowCalibrationSet.rescaled_dispense_accel`。

## 修正したドキュメント

`src/pcbasm/pasting/README.md` のみ。

- モジュール機能一覧の「流量キャリブレーション・ローディング」→
  「吐出量キャリブレーション・ローディング」。旧 `flow_calibration`（流量＝質量計測）
  ジョブ廃止と銅板線引き式キャリブへの刷新に合わせた最小修正。ローディング機能
  （`interactive_loading`）自体は残存（廃止は loading 画面の質量キャリブ表のみ）なので維持。

検証: `pre-commit run --files src/pcbasm/pasting/README.md` 全 Passed（mdformat / codespell 含む）。

## 変更不要と判断した箇所

- `README.md`（プロジェクト）: 今回の変更に関する記述なし。`docs/webui/specification.md`
  への参照リンクは存在するが `docs/` 自体が未配置の既存の壊れたリンクで、今回の変更とは
  無関係。作業範囲外として触らず。
- `AGENTS.md` / `configs/README.md` / 他モジュール README: 該当言及なし。
- `src/pcbasm/pasting/dispense_calibration.py`: module/クラス docstring は刷新後の事実を
  正確に記述済み。修正不要。
- `src/pcbasm/pasting/calibration.py`（`FlowCalibration` / `MassFlowCalibration` /
  `FlowCalibrationSet`）: クラスは①の rotations_per_ul 算出用に内部存続。docstring の
  「流量キャリブレーション」は数学モデルの説明で、廃止された旧ジョブ/旧 endpoint への
  言及ではないため矛盾なし。`rescaled_dispense_accel` の docstring も新規追加分として整合。
- `applicator.py` / `fill_sequence.py` / `config.py`: docstring は既に `max_fill_speed`
  へ移行済みで実態と一致。`vision/README.md` の「カメラキャリブレーション」は無関係。
- CLAUDE.md / `.claude/` / `memory/`（規約類）/ `.claude/plans/`: ユーザー管轄のため不変。
