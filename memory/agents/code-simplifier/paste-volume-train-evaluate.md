# paste_volume train / evaluate（Phase 2 + Phase 3）の仕上げ

起点 `2b9a63f`。3 巡分のレビューの nit 処理、実装者の確認事項 2 件、ドキュメント同期、
docformatter 由来の日本語崩れ（本 MR が触った file 分）。

**学習の振る舞いは変えていない。** model / loss / optimizer / split / batch / seed の
どれにも触っていない。step 8 の実走（MAE 0.01708 / R^2 0.9160、commit `4af725b`）を
再実走する必要は無い。

## 簡素化した内部実装

- `PasteVolumeTrainingConfig.validate` の `math.isfinite` を落とした（1 巡目 N1）。
    NaN も inf も `0 < x < 1` だけで落ちる。`import math` も不要になった
- `UncertaintyCalibration` と `CALIBRATION_DOCUMENT` を `train.py` から
    `experiment.py` へ移した（3 巡目 N4）。`CALIBRATION_FILE_NAME` が既に
    `experiment.py` にあり、書くのは学習・読むのは評価なので、封筒を持つ module が
    両者の共通の下流になる。`evaluate.py` が `train.py`（Trainer / CheckpointStore を
    引く module）を import しなくなった。**`train.py` は再 export を残すので
    `ml.paste_volume.train.UncertaintyCalibration` は従来どおり使える**
- `_train_measured_mean(data)` を `run_training` で 1 度だけ計算し、`_run_tags` と
    `_run_params` へ渡す形にした（3 巡目 N1）。`_mean_bias_deviation` の引数も
    `data` から `train_measured_mean` へ変えた（private）
- `EvaluationRequest.validate` が空の `data.roots` を拒むようにし、`evaluate_fold` の
    上書き判定を `if roots:` から `if roots is not None:` へ変えた（3 巡目 N5）。
    `data.roots=[]` が黙って無視されて config 側の roots が使われる形を潰した

## 公開 IF 維持の確認

- 変えた関数シグネチャは private（`_run_tags` / `_run_params` / `_mean_bias_deviation`）のみ
- `UncertaintyCalibration` / `CALIBRATION_DOCUMENT` は定義位置だけが動き、
    `ml.paste_volume.train` からの import と `__all__` は維持（`test_train.py` の
    既存 import が無修正で通ることで確認）
- `make ml-docker-check` が全緑（下記）

## 対応した nit

### 1 巡目（step 0/1）

| nit | 対応 |
| --- | --- |
| N1 冗長な `isfinite` | 直した |
| N4 `PasteVolumeTrainingConfig` の field 並べ替え | **直さない**（下記） |
| N5 `TestRoleLabels` が test file 内の literal 比較 | **直さない**（下記） |
| N6 `resolve_session` の前頭一致が `sha256:` 込み | docstring へ書いた |
| N7 test helper の `**overrides: object` | **直さない**（既存の書き方の踏襲。型を締めると helper 側に overload が要る） |

### 2 巡目（step 2/3）

| nit | 対応 |
| --- | --- |
| N1 `zero_target_count` の将来衝突 | **直さない**（本数 26 の assert が機構として守っている） |
| N2 padding pixel を凍結しない理由の出典 | 仕様書が言っていることと実装者の解釈を docstring で分けた |
| N3 「residual stage 3」→「最終 stage」の読み替え | `apply_fine_tune_freeze` の docstring へ 1 行入れた |
| N4 `validate_gaussian_inputs` がどこからも呼ばれない | **本 MR の範囲外**（`ml` コア側。下記「別タスク」） |
| N5 `test_task.py` が 1,183 行 | **直さない**（src が 1 module なので tests ミラー規約と衝突する） |
| N6 `_as_float_mapping` の複製 | **直さない**（計画書 R6 で許容済み） |
| N7 「約 150 万 parameter」の直接 assert 無し | **直さない**（395,048 の完全一致が含意する） |

### 3 巡目（step 4/5/6）

| nit | 対応 |
| --- | --- |
| N1 `_train_measured_mean` が 2 回走る | 1 度にした |
| N2 `run_training` と `main()` の二重 `validate()` | **直さない**（下記） |
| N3 `cli._validation_lines` の docstring | 実際の出力に合わせた（理由は出さない） |
| N4 `evaluate` → `train` の import | `UncertaintyCalibration` を `experiment.py` へ移して解消 |
| N5 `data.roots=[]` が黙って無視される | `validate` で拒むようにした |
| N6 `run_kind` の語彙が仕様書 §4 と食い違う | 仕様書 §4 を実装値へそろえた |
| N7 preset x trainer profile が 1 組だけ | 全 4 preset x 全 2 profile の走査にした（+7 test） |
| N8 `assert not (PROJECT_ROOT / "mlruns").exists()` が CWD 依存 | subprocess の cwd を workspace 配下へ隔離した |
| N9 `collect_predictions` が `eval()` を戻さない | **直さない**（下記。docstring に明記した） |
| N10 `_repository_root()` の `parents[3]` 決め打ち | **直さない**（`GitProvenance.capture` が理由を返すので害が無い） |
| N11 budget 拒否より前に `run_directory` を mkdir | **直さない**（下記） |

### 直さなかった判断の理由

- **1 巡目 N4（field 並べ替え）**: 位置引数呼び出しは 0 箇所で、並びは split 次元まわりの
    field をまとめた意図的なもの。危険を機構的に消すには `kw_only=True` にするしかなく、
    それは公開コンストラクタの変更になる
- **1 巡目 N5（`TestRoleLabels`）**: 検出力を足すには src 側へ役割ラベルの一覧を公開する
    必要がある。テスト都合で公開 IF を増やす形になるので採らない。`TestMaterialsInUse`
    側（M1 で強化済み）が実質の検出器という reviewer の見立てに従う
- **3 巡目 N2（二重 `validate()`）**: `main()` 側は「記録先を開く前に argv の不備を
    報告する」ため、`run_training` 側は entrypoint を経由しない呼び出し元
    （`search._trial_value` は経由する、が公開 API としては要る）のため。`validate()` は
    副作用が無いので 2 度通してよい。**docstring の「同じ不整合へ検出器を 2 つ置かない」が
    Trainer 送出の話であることを明記して矛盾を解いた**
- **3 巡目 N9（`eval()` を戻さない）**: try/finally を足すとコードが増えるだけで、
    呼び出し元は学習後の calibration と評価の 2 つだけ。どちらもそのあと学習を続けない。
    **側効果を docstring に明記する**ほうが「明示 > 暗黙」に沿う
- **3 巡目 N11（mkdir の位置）**: budget 検査より前に mkdir を消すには
    `PasteVolumeTrainingData.build`（split.json を run directory へ書く）を model 構築の
    後ろへ動かす必要がある。**`seed_everything` と split 生成の順序が入れ替わるので、
    学習経路に触る変更になる。制約により実装しない**

## 実装者の確認事項 2 件

### `run_training` の戻り値の意味が 2 通り

**契約はそのまま、docstring を契約表に書き直した。** 3 通りの組み合わせ
（`(None, 理由)` / `(outcome, 理由)` / `(outcome, None)`）を列挙し、2 つ目を
`(None, 理由)` へ畳めない理由（`Trainer` が run の開始と終了を持つので、run が
終わってから MLflow へ書き足せない）を書いた。

結果オブジェクトへ変えるのが最も読みやすいが、`run_training` は公開 API なので
戻り値の型を変えると公開 IF が変わる。**呼び出し側（`train.main` /
`search._trial_value`）の振る舞いは 1 行も変えていない。**

### 終了コードの追記

3 か所へ書いた。

- `src/ml/paste_volume/train.py` の module docstring（0 / 1 / 2 の表）
- `docs/image-based-dispense-calibration-ml-plan.md` §7 の新設節
    「LOSO 5-foldの実走手順」（実走コマンド + 終了コードの表）
- `main()` の docstring（元からあったので維持）

## 同期したドキュメント

- **`AGENTS.md`**: `src/ml/` の 1 行説明へ「探索」と、ドメイン層が持つもの
    （model / task / conf / train / evaluate / search / 運用 CLI）を足した
- **`docs/image-based-dispense-calibration-ml-plan.md`**
    - §4 の `run_kind` 一覧を実装値（`base-train` / `cell-split-train` / `fine-tune` /
        `hpo-trial`）へ。`evaluate` / `export` / `benchmark` は Phase 4 と明記
    - §7 module 一覧に `ml.paste_volume.experiment` と `ml.paste_volume.search` を追加。
        **`ml.paste_volume.release` は「Phase 4。v1 では未実装」と注記して残した**
    - §7 の argv 例を実装の形へ（`data.manifest=` → `data.roots=`、`trainer=` /
        `logger=` / `logger.artifact_location` / `run_directory` を追加）。
        `evaluate` が group 層を積まないことも明記
    - §7 の運用 CLI 一覧に実装済み / 未実装（`dataset merge` は composite manifest ごと
        未実装、export / optimize / benchmark は Phase 4、infer は Phase 5）を注記
    - §7 に「LOSO 5-foldの実走手順」を新設（ユーザーが実測したコマンド + 終了コード表）
    - Phase 2 / Phase 3 を「（完了）」にし、実測値と**この MR でやらなかったもの**
        （`ml.paste_volume.release` / `dataset merge` + composite manifest /
        MLflow tracking server の常駐 / `data`・`model` の group directory）を列挙
- **`docs/image-based-dispense-calibration.md`**（親要件書）: CLI 一覧が
    `ml.paste_volume.cli train / finetune / evaluate` を挙げていたが、これは ML 実装計画
    §7 の「学習 entrypoint と subcommand parser に同じ argv を処理させない」と実装の
    両方に反する。独立 module を直接起こす形へ直し、正典を §7 と明記
- **docstring**: `resolve_session`（`sha256:` 込みの前頭一致）、
    `apply_fine_tune_freeze`（stage の読み替えと出典の分離）、
    `cli._validation_lines`（理由は出さない）、`collect_predictions`（eval mode のまま返す）、
    `run_training`（戻り値の契約表と二重 validate の理由）、
    `train.py` module docstring（終了コード）、`_train`（cwd を隔離する理由）

## docformatter 由来の日本語崩れ

本 MR が触った file のうち、`tests/ml/paste_volume/test_batch.py`（4 箇所）と
`tests/ml/paste_volume/test_index.py`（8 箇所）の計 **12 箇所**を直した。段落ごとに
1 物理行へ畳み、日本語どうしの間に混入した **U+0020 だけ**を消した。整形前後で
**非 ASCII 文字の多重集合が一致すること**を機械確認済み（漢字化けなし）。
`make ml-docker-check` の docformatter を通しても再発しない。

**MR の範囲外に残っているもの（別タスク）**: `src/ml/paste_volume/dataset.py` 1、
`tests/ml/paste_volume/test_dataset.py` 4、`tests/ml/paste_volume/test_session.py` 1。
step 5/6 実装者が報告した「28 箇所 / 12 file」の残り。検出は次で足りる。

```bash
grep -rnP '[ぁ-んァ-ヴー一-龥、。][ ][ぁ-んァ-ヴー一-龥]' --include=*.py src tests
```

## 別タスクへの申し送り

- **2 巡目 N4**: `ml.model.loss.validate_gaussian_inputs` は repo 内のどの task からも
    呼ばれていない（`GaussianRegressionTask` も同様）。`ml` コアの死んだ検査で、
    本 MR の範囲外なので触っていない
- **step 4 実装者の申し送り**: `tests/ml/tuning/test_integration.py:_duplicated_defaults` は
    `type(field.default)` で辿るため、既定値を持たない必須 field 配下の既定値二重定義を
    素通りさせる。`tests/ml/paste_volume/test_conf.py` 側だけ annotation から辿る版へ
    強化済みで、**tuning 側は弱いまま**

## 検証結果

`make ml-docker-check`（GPU workstation の常駐 container、**CUDA 可視。
`CUDA_VISIBLE_DEVICES` は unset で `torch.cuda.is_available() == True` / device 2**）。

- pre-commit: 全 hook Passed（ruff / ruff-format / docformatter / mdformat / codespell 含む）
- pyright: **0 errors, 0 warnings**（pyright の 3 つ目のカウンタも 0）
- pytest `tests/ml -m "not hardware and not e2e"`: **1770 passed, 1 skipped**（106.81s）

起点 `2b9a63f` の実測は 1763 passed / 1 skipped（同じ CUDA 可視の条件）。
差の **+7 は 3 巡目 N7 の走査化**（1 組 → 4 preset x 2 profile = 8 件）だけで説明がつく。
