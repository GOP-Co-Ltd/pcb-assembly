# ペーストローディング 質量キャリブレーション エンドポイント — 簡素化レビュー

対象ブランチ: `feature/20260624/loading-calibration-endpoint`（変更は全て working tree、未コミット）
前段ノート: `memory/agents/plan-implementer/webui-loading-calibration.md`

## 結論: 変更なし（no-op）

diff は既に最小・最明瞭で、高確度の簡素化余地が無かったため**何も変更していない**。

## レビューした対象と判断

### Python（calibration.py / __init__.py / pasting.py / pages.py）
- `TrapezoidalRotationProfile` クラスと `MassFlowCalibration.from_trapezoidal_profile`
  の削除は dead code 除去で純粋に簡素化方向。
- 追加した `dispense_rate_for` / `dispense_accel_for` は `FlowCalibrationSet` の
  同名メソッドと同式・同 docstring を踏襲。重複に見えるが両クラスは独立した値
  オブジェクトで、共通基底を導入するのは早すぎる抽象化。現状維持が正しい。
- `get_loading_calibration` は early-return ガード + 4 値構築で読みやすい。
  クエリ名・レスポンスキーは tests/webui/routers/test_pasting.py が pin。触らない。
- `pages.py` は context 2 行追加のみ。

### JS（loading_controls.js）
最大の変更点。検討した簡素化候補と却下理由:
1. result key（`rotations_per_ul` 等）/ `computed` の短縮 key（`rpu` 等）/ 設定 key
   （`paste_dispenser.*`）の 3-way マッピングを 1 つの descriptor テーブルに統合する案 →
   それなりの重複だが、既にレビュー済みの動作するコードを大きく再構築する churn。
   JS には lint/format/単体テストの安全網が無く（`make` で走るのは E2E のみ・親が実行）、
   リスク対効果が悪い。CLAUDE 原則「外科的変更・早すぎる抽象化を入れない」に反する。見送り。
2. `value === null || value === undefined` → `value == null` は単なる style nit。見送り。
3. `updateApplyButtons` の apply-all 条件が 3 条件を再掲する点 → 明示的で読める。見送り。

plan-implementer ノート時点で既に「薄いクライアント化（計算を全てサーバ側へ移動）」
という簡素化が実装中に済んでおり、これは post-simplification なコード。

## 公開IF維持の確認
- 何も変更していないため公開 IF は自明に不変。
- HTML id 群（#lc-*）、エンドポイント `GET /api/pasting/loading/calibration`、
  クエリ名（mass_mg/rotations/rate/accel）、レスポンスキー
  （volume_ul/rotations_per_ul/max_dispense_rate/dispense_accel）、
  `MassFlowCalibration` の公開メソッド名はすべてそのまま。

## 検証結果（現状 working tree のまま）
- make format: pass
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（1239 passed, 63 deselected。新規 TestLoadingCalibration 5 件含む）
- E2E: 親が実行（このレビューでは未実行）
