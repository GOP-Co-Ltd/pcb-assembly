# docs-keeper: 吐出量キャリブ リファクタ（実行中編集・中止→メニュー・タール/退避Zオフセット）

入力ブランチ: `refactor/20260625/dispense-calibration-tab`。
前段メモ: `memory/agents/plan-implementer/dispense-calibration-refactor{,-ui}.md`、
`memory/agents/code-simplifier/dispense-calibration-refactor.md`、
`memory/agents/spec-test-author/dispense-calibration-refactor.md`。

## 結論

**docstring / ドキュメントの修正は不要**だった。確認した公開面はすべて現状コードと整合
していた（実装側の各コミットで docstring が同時更新済み）。古い記述（ライブビュー化で
陳腐化したはずの文言）の混入も無し。

## 整合確認した公開面（いずれも正しい・修正なし）

- `src/webui/jobs/catalog.py`
  - `ParamSpec.runtime_editable`: Attributes に「実行中に値を変更できるか（True で patch
    受理）」と記載済み（L29）。
  - `JobDefinition.runtime_params`: 1 行サマリ済み（L72）。
  - `JobCatalog.validate_runtime_params`: patch セマンティクス（default 充填しない）と
    例外条件（固定/未知キー・型不一致・負の removal_z_offset）を明記済み（L143-152）。
  - `validate_params` の「default 充填済み」は **start 時用**の正しい説明で、
    runtime 側と用途が別。混同なし。
- `src/webui/jobs/context.py`
  - `JobBridge.live_params`（Protocol, L51）/ `JobContext.params`（L102-108）:
    「ライブストアから都度読み直す＝毎アクセスで実行中編集を反映」と明記済み。
    旧 `self._params` は実装から撤去され、`__init__` のコメントも live_params 委譲を説明
    する内容に更新済み（L90-92）。「開始時に 1 回」「default 充填済み」のような陳腐化
    文言は params 周りには **残っていない**。
  - `machine` プロパティの「ジョブ開始時に 1 回ロード済み」（L117）は **正しい**。
    machine は start() で 1 回ロードし保持する不変値で、params のようなライブ再読込では
    ない。陳腐化ではないので維持。
- `src/webui/jobs/manager.py`
  - `update_current_params`（L554-567, out-of-band/lock/persist 分岐）、
    `_JobRuntime.live_params`/`update_params`（L283/288）、`JobRecord.update_params`
    （L221）いずれも docstring 済み・正確。
- `src/webui/routers/jobs.py`
  - `PUT /jobs/current/params`（L139-153）: リクエストモデル docstring と「400: 不正」を
    記載済み。
- `src/webui/jobs/pasting.py`
  - `_CalibrationCancelled`（L640）/ `_prompt_confirm`（L657）/ `_prompt_mass`（L674）/
    `_removal_z`（L1184）、および ①②③（`_calibrate_*`）の docstring はタール確認・
    退避 Z＝max(z_min, z_max−removal_z_offset)・中止→メニュー・実行中変更可を正確に反映。
    private（`_` prefix）だが既存スタイルに合わせ簡潔な 1〜数行で維持されており、過剰
    でも陳腐でもない。追記・削除なし。

## ドキュメント追記が不要と判断した箇所

- プロジェクト/モジュール README・docs: ジョブの WS/REST API 一覧や「パラメータ機構」を
  説明する章は **どこにも存在しない**。`docs/` ディレクトリ無し、`src/webui/README.md`
  無し。`PUT /jobs/current/params` / `runtime_editable` を載せる既存の該当章が無いため、
  新規に章を起こさない（方針どおり「既存に該当章があるときのみ追記」）。
- `src/pcbasm/pasting/README.md`: pcbasm 層の機能一覧で、WS/REST や runtime_editable には
  触れていない。今回の WebUI 層リファクタの影響を受けず、前回 docs-keeper 修正
  （「吐出量キャリブレーション・ローディング」）のまま整合。修正なし。
- CLAUDE.md / `memory/` / `.claude/`: 方針どおりメインが別途管理。触らず。

## 検証

- `make format`: pass（docformatter / mdformat / codespell 含む全 Passed）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（1401 passed, 70 deselected）

ドキュメント差分が無いため検証は「既存が壊れていないこと」の確認のみ（diff ゼロ）。
