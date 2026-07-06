# ジョブ実行中パラメータ編集 基盤（コミット 1: feat(webui)）

承認済み計画: `/home/gop/.claude/plans/fluffy-sleeping-truffle.md`
担当: plan-implementer（src/ のみ）。tests/ は並列 spec-test-author 担当。

## 実装済み公開 IF（spec-test-author と契約共有済み・シグネチャ厳守で実装）

- `ParamSpec.runtime_editable: bool = False`（catalog.py、frozen の末尾フィールド）。
- `JobDefinition.runtime_params -> tuple[str, ...]`（`@property`、runtime_editable な名前の tuple）。
- `JobCatalog.validate_runtime_params(definition, values) -> dict[str, ParamValue]`。
- `JobBridge.live_params(self) -> Mapping[str, ParamValue]`（context.py Protocol）。
- `_JobRuntime.live_params()` / `_JobRuntime.update_params(updates)`（manager.py）。
- `JobRecord.update_params(updates)`（manager.py）。
- `JobManager.update_current_params(values, *, persist=False) -> dict[str, ParamValue]`。
- `PUT /api/jobs/current/params`（body: `{values: dict, persist: bool}` → `{"params": <validated>}`、ValueError→400）。

## 計画外の判断ログ

- **negative removal_z_offset チェックの置き場**: `_coerce_param` を 2 段に分割した
  （`_coerce_type` が従来の型変換、`_coerce_param` が型変換 + removal_z_offset の `>= 0` ガード）。
  これにより `validate_params`（起動時）・`validate_runtime_params`（実行中）・`filter_persisted_defaults`
  の全経路が同じ負値拒否を共有する（フォークなし）。`spec.name == "removal_z_offset"` 限定の
  最小実装で、汎用の下限機構は入れていない（計画指示どおり。コメントで理由明記）。
  - メッセージ: 「removal_z_offset: マイナスにできません（与えられた値: ...）」。
  - 注意: この時点では `dispense_calibration` の ParamSpec に removal_z_offset はまだ無い（コミット 2 で追加）。
    catalog の仕組みだけ用意した。spec-test-author がテストするなら、テスト用にローカル定義した
    `ParamSpec(name="removal_z_offset", value_type="float", runtime_editable=True)` を使うとよい。

- **validate_runtime_params のエラーメッセージ区別**:
  - runtime_editable でない既存パラメータ → 「実行中に変更できないパラメータです: <key>」。
  - 定義に存在しないキー → 「未知のパラメータです: <key>」。
  - 型不一致 → `_coerce_param` の既存メッセージ（「<name>: <type> 型の値が必要です…」）。
  - 空 patch（`values={}`）は空 dict を返す no-op（200 相当）。default 充填はしない。

- **JobContext の `self._params` 撤去**: `params` プロパティを `self._bridge.live_params()` 委譲に変更したため、
  `__init__` の `self._params = dict(params)` は orphan になった。自分の変更で生じた dead なので削除し、
  `params` 引数は `del params` で受けるだけ残した（シグネチャは不変。manager がブリッジのライブストア
  初期値として使う）。`JobContext.params` は毎アクセスでブリッジから読み直す。

- **JobRecord.params のロック追加**: 既存 `params` read プロパティはロック無しだった。
  実行中編集（`update_params`）と並行読みが起きるため、read プロパティを `with self._lock: return dict(...)`
  に変更（他の read プロパティと同じ作法・コピー返却で torn read 防止）。

- **ライブストアの初期値**: `_JobRuntime.__init__` で `self._params = dict(record.params)` から初期化。
  `start()` が `JobContext(runtime, params=params, ...)` を渡すが、JobContext は params を保持せず
  runtime（= bridge）の `live_params()` を読むので、runtime のストアが単一の真実。

## スレッド安全性 / デッドロック回避

- `update_current_params` は `self._lock`（JobManager）を **snapshot 取得のみ**で使い、
  即座に解放してから `runtime.update_params` / `record.update_params` / `state.save_job_param_defaults`
  / `runtime.publish_status` を呼ぶ。`self._lock` を保持したまま runtime/record の各ロックに入らない
  （既存 `submit_command` / `request_abort` と同じ順序）。ロックのネストは発生しない。
- `live_params` / `update_params`（runtime）は `_params_lock` 下で dict コピー / update。内部 dict を露出しない。

## persist の state 取得方法

- `JobManager` は既に `self._state: AppState` を保持。`start()` と同じく
  `self._state.save_job_param_defaults(name, merged)` を使う。
  merged = `{**self._state.job_param_defaults(record.name), **persisted}`
  （router の `post_job_param_defaults` と同じマージパターン）。`persisted` は
  `definition.persisted_params` で絞った validated subset。空なら save しない。

## 触っていないもの（計画どおり）

- `request_abort` / `runtime.abort()` / `JobAborted` / コマンドキュー（`next_command`/`submit_command`）。
- `validate_params` / `filter_persisted_defaults` のロジック本体（coerce ヘルパ共有のみ）。
- `dispense_calibration` の ParamSpec（board_width/height/tolerance 含む）＝コミット 2 担当。
- configs/ 配下。pasting.py のフロー（コミット 2-4 担当）。

## 他 implementer への IF 変更通知

- なし（並列実装者は spec-test-author のみ。IF はシグネチャレベルで事前確定どおり）。

## 検証結果（コミット 1 スコープ）

- make format: pass（ruff-format が長行 3 ファイルを自動整形、再実行で安定 pass）
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（1392 passed, 65 deselected）
  - 本機能の新規テストは spec-test-author 側。既存テストは全緑（破壊なし）。

## 既知の制約・残課題

- コミット 2 で `dispense_calibration` の params に `removal_z_offset`（float, runtime_editable=True,
  unit="mm", default=DISPENSE_CALIBRATION_DEFAULT_REMOVAL_Z_OFFSET=0.0）を追加し、共有値を
  runtime_editable 化する。負値拒否の足場（_coerce_param）は本コミットで完了済み。
- フロント（pages.py グループ再編・runtime-editable JS パネル）はコミット 2/5 担当。
