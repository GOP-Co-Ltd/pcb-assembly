# pasting 再構成 MR5（dataset + toolhead offset）レビュー

対象: worktree `.claude/worktrees/pasting-mr5`、`git diff 28d1351..HEAD`（ff909b4, 771c683, 9a44e9d）。
計画書 `memory/agents/implementation-planner/pasting-mr5-dataset-toolhead.md`、実装ノート
`memory/agents/plan-implementer/pasting-mr5-dataset-toolhead.md` と突き合わせた。
orchestrator 承認済みの逸脱（`record_post -> str | None`、`ProbedPoint.point: ToolheadOffsetPoint`、
docs の旧モジュール名、`resolved` のキー順）は指摘対象外。

## verdict: approve

must-fix なし。should-fix は動作に影響しない範囲で、code-simplifier で対応可能。

## 確認した不変性（問題なし）

- **metadata.json 形状**: 旧 `paste_dataset.py` の DTO と新 `dataset/metadata.py` のフィールド名・型・ネストが一致。
  `resolved` は `PasteParams` の 8 キー（`paste_height` の `"auto"` literal 保持）、`execution` は
  `DispenseSummary` の 6 キー（`applied_mode` の `"mixed"` / `None` 受理）。fixture
  `data/testing/schemas/paste_dataset_metadata_v1.json` と `test_recorder.py` のキー path 集合比較でピン済み
- **toolhead offset 診断 JSON**: `ToolheadOffsetDiagnostics.to_dict` のキーと順序
  （`requested_point_count` / `minimum_valid_point_count` / `successful_point_count` / `failures[]{index, board_position{x,y}, reason, image}`）、
  失敗 PNG 名 `toolhead_offset_failure_NN.png`、ファイル名 `toolhead_offset_diagnostics.json` / `toolhead_offset.json` が旧 web 実装と同一。
  失敗ごとの JSON 書き直しと終了時の再書き込みのタイミングも同一
- **ジョブ手順**: dataset（confirm 2 回 → 塗布前撮影 → retract → purge → 塗布 → 塗布後撮影 → 質量 prompt → finalize → ZIP）、
  toolhead（直径検証 → 配置 → 高さ計測 → loading → 塗布 → 円検出）の prompt 順・progress 文言と分母・
  checkpoint 位置・ログ文言・`JobResult.summary` / `ApplyPayload` / RuntimeError 文言は旧実装と一致。
  検出リトライ定数（5 frame / 3 attempts / 0.5 s）と最小サンプル数 5 の扱いも同じ（`measure -> None` を web が旧 RuntimeError 文言に変換）
- **web 薄化**: `src/web/api/jobs/pasting/{dataset,toolhead_offset}.py` に幾何・検証式・DTO 詰め替え（旧 `_dataset_execution` / `_dataset_resolved` / Metadata 組立 / probe・deposit・measure）は残っていない。
  `pcbasm` から `web.*` の import なし
- **import 契約**: `pasting/__init__.py` は re-export なし。`tests/test_package.py::TestPastingImportLight` 緑
- **旧名の残存**: `paste_dataset` / `crop_pad_image` / `DatasetResolvedPaste` / `DatasetExecution` / `PadImageCrop` /
  `validate_dataset_image_margins` / `from_dict` は src / tests に残っていない（docs は承認済み残課題）
- **成果物汚染**: 変更ファイルに `</content>` 等の混入なし
- **テスト配置**: src 1 ファイル ↔ test 1 ファイル。`class TestXxx` 集約、private 非参照、fake は `tests/helpers` の `FakeKlipper` / `FakeCamera` のみ、3rd-party モックなし

## must-fix

なし。

## should-fix

1. **`validate_view` が本番経路から呼ばれていない**
   - 対象: `src/pcbasm/pasting/dataset/metadata.py:43`（定義）、`src/pcbasm/pasting/dataset/writer.py:106`（`write_capture`）、`src/pcbasm/pasting/dataset/recorder.py:78`（`__init__`）
   - 問題: 旧 `DatasetView.__attrs_post_init__` は「view 番号 ≥ 0・offset 有限」を構築時に保証していた。新実装はそれを `validate_view` に外出ししたが、
     writer / recorder / web のどこも呼ばない。`DatasetView(number=-1)` がそのまま `write_capture` に届くと `000001.-1.png` と負の `number` が metadata に書かれる
   - 根拠: `grep -rn validate_view src` の呼び出しはゼロ（tests のみ）。現状 web は `DatasetView(number=0)` しか作らないため実害はないが、invariant の抜けと dead code の両方に当たる。
     `PasteDatasetRecorder.__init__` で `views` を一括検証し ValueError（invariant）にするのが最小
   - 確信度: 高（未使用は確実）／深刻度: 低

2. **`strict_applied_mode` が `AppliedDispenseMode` の literal 値を重複定義**
   - 対象: `src/pcbasm/pasting/dataset/metadata.py:296`
     ```python
     if value is None or value in ("dot", "line", "area", "mixed"):
     ```
   - 問題: `pcbasm/pasting/fill_path.py:41` の `AppliedDispenseMode = Literal["dot", "line", "area"]` と同じ値をハードコードしている。
     方式が増えたとき parse 側だけ取り残される
   - 根拠: 同ファイル内で `get_args` を既に使っており、`get_args(AppliedDispenseMode) + ("mixed",)` で導出できる
   - 確信度: 高／深刻度: 低

3. **`ToolheadOffsetProcedure.measure` の静定待ちが注入不能**
   - 対象: `src/pcbasm/pasting/toolhead_offset.py:44, 475`（`_MEASURE_SETTLE_TIME = 1.0` / `time.sleep`）
   - 問題: 同 MR の `DatasetCapturer` は `settle_time` を ctor 引数にしているのに、Procedure は module 定数固定。
     `test_measure_returns_failure_with_roi_image_when_no_circle` が実 sleep 1 s + リトライ待ち 1 s を消費する
   - 根拠: 実装ノートは「成功経路の unit テストを書かないため不要」と判断しているが、失敗経路のテストは既に存在し、`DatasetCapturer` との API 非対称も生じている
   - 確信度: 中（設計判断の範囲）／深刻度: 低

## nit

- `type PixelRect` が `vision/crop.py:16` と `pasting/dataset/metadata.py:28` に重複。metadata から crop を import すると cv2 が入るため、`pcbasm.vision.image` 等の軽い場所へ移すのが筋
- `tests/pcbasm/pasting/dataset/test_capture.py` と `tests/pcbasm/pasting/test_toolhead_offset.py` で `_g1_moves` / `_calibration_result`・`_board_result` がほぼ同一。`tests/helpers.py` へ寄せられる
- `src/pcbasm/pasting/dataset/writer.py:32` `PasteDatasetWriter.__init__(root, stem)` は public だが docstring で「直接構築は想定しない」。計画どおりではあるが、`_stem` 採番済み前提の公開 ctor は誤用余地がある
- `src/web/api/jobs/pasting/dataset.py:136-147` 旧実装は `initial_purge_ul` 検証を params 読み出しより前に行っていた。新実装は `ctx.params["paste_id"]` を先に読むため、param 欠落時の例外種別が KeyError になる（ParamSpec 必須なので実運用では起きない）
- `src/web/api/jobs/pasting/toolhead_offset.py:311` `_diagnostics` は純粋な DTO 構築。`ToolheadOffsetDiagnostics` 側の classmethod にすると web からさらに消せる
- `src/pcbasm/pasting/toolhead_offset.py:3, 349` docstring に不自然な半角スペース（「担い、 ここは」「ループと 進捗」）
- parse の厳格化: 旧 `DatasetResolvedPaste.dispense_mode: str` は任意文字列を受理していたが、新 `PasteParams.dispense_mode: DispenseMode` は literal 検査になる。旧 writer が書いた値は常に有効 literal なので既存ファイルの読込に影響なし（情報のみ）
- `tests/pcbasm/pasting/dataset/test_writer.py:280`, `test_recorder.py:45` の `_summary` / `_execution` が `applied_mode: str` + `# type: ignore[arg-type]`。引数型を `AppliedDispenseMode` にすれば ignore 不要
- `test_toolhead_offset.py::test_deposit_dispenses_at_surface_z_plus_paste_height` が fixture 設定値 `paste_height == "auto"` を assert している（テスト対象ではなく前提の確認。コメントか `assume` 相当にするのが自然）

## 検証結果

- make format: pass（実行後 `git status` clean）
- make type: pass（0 errors, 0 warnings）
- `uv run pytest -m "not hardware" tests/pcbasm/pasting/dataset tests/pcbasm/vision/test_crop.py tests/pcbasm/pasting/test_toolhead_offset.py tests/web/api/jobs/test_pasting.py tests/test_package.py`: 187 passed, 8 deselected
- `make test` / hardware は未実行（禁止）。実機確認はユーザー（`paste_dataset_collection` 1 基板・`toolhead_offset` 失敗画像 / 診断 JSON / Apply）
