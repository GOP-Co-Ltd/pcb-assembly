# paste_volume train / evaluate step 0 + step 1 レビュー

対象: `git diff d74f96f..fb65491 -- src tests`（`347acfe` step 0 / `fb65491` step 1）。
`3590bc6` 以降と、同じ作業ツリーで並行実装中の step 2/3 は対象外。

レビューは repo を書き換えずに行った。変異は `.review-mutants/`（src と tests/ml の複製）を
`PYTHONPATH` で差し替えて実施し、終了後に削除済み。作業ツリーは触っていない。

## verdict: request-changes

## must-fix

### M1. view-dropout の材料検査が退化した観測点で、材料の衝突を検出できない

- 対象: `tests/ml/test_seed_roles.py:129-150`（`test_view_dropout_uses_its_labelled_material`
  / `..._does_not_use_the_unlabelled_material`）、`AVAILABLE_VIEW_COUNT = 5`
- 問題: `GLOBAL_SEED=7, EPOCH=3, SAMPLE_ID` の材料は `randint(1,5)` が **5** を引く。
  `tuple(sorted(sample(range(5), 5)))` は乱数列に関係なく必ず `(0,1,2,3,4)` になるので、
  この観測点は材料を区別しない。実測: 任意材料 2000 件のうち **21%** が同じ `(0,1,2,3,4)` を返す。
- 根拠（測定）: `src/ml/data/batch.py:102` のラベルを `view-dropout` → `augmentation` に
  変えて **augmentation と材料を完全に衝突させても**、`test_seed_roles.py` は全緑のまま
  （落ちたのは無関係な `tests/ml/data/test_batch.py::TestViewDropout::
  test_each_sample_gets_its_own_subset_of_views` 1 件だけ）。
  この file が「4 用途の材料は互いに一致しない」と主張している当の衝突を、この file 自身は
  検出できない。
- 直し方: `count < AVAILABLE_VIEW_COUNT` になる定数を選ぶ（実測: 同じ SAMPLE_ID で
  `EPOCH=0` → count 1 / `(0,)`、`EPOCH=1` → count 1 / `(2,)`、`AVAILABLE_VIEW_COUNT=8`,
  `EPOCH=3` → count 2 / `(1,2)`）。count 自体も観測点に含めるとさらに強い。
- 確信度: 高（測定済み）。深刻度: 高（このリポジトリが 5 回踏んだ「検出力の低下で黙って緑」の型）

### M2. 閾値 28 の根拠に書いた実測値 32 が再現しない（実測 31）

- 対象: `tests/ml/paste_volume/test_batch.py:539-541` のコメント、
  `memory/agents/plan-implementer/paste-volume-train-evaluate-step01.md` の実測表
- 問題: コメントは「60 epoch で 32 通り出る（実測）」と書いているが、`fb65491` の code で
  実測すると **31**（欠けるのは `(band 5, offset 3)` の 1 通り）。3 回連続で 31、
  repo の src をそのまま使っても 31 で決定的。
- 根拠（測定）: epoch 数を変えた probe で
  60 → **31**、120 → 32、200 → 32、400 → 32、1000 → 32。
  材料を衝突させた場合（`_PLACEMENT_ROLE="augmentation"`）は 60/120/200/400/1000 いずれも **20**
  （bands 0-3 が 1 位置に固定、bands 4-7 が 4 位置）。
- 影響: 「理論上限 8x4=32 にちょうど到達している」という観測点の主張が成立していない。
  60 epoch では上限へ届いていないので、`>= 28` の上側の余裕は 4 ではなく 3。
- 直し方（提案）: epoch を 120 へ上げて `len(pairs) == 32` の完全一致で固定する
  （計画書 R1 の「閾値を下げず epoch を増やす」に沿う。上限に張り付くので `>=` より強い）。
  据え置くならコメントと申し送りの 32 を 31 に直す。
- 確信度: 高（測定済み）。深刻度: 中（現状のテストは衝突を検出できるが、根拠の記録が誤り）

### M3. session 次元で既存 split.json を再利用すると、held_out_session の食い違いを黙って通す

- 対象: `src/ml/paste_volume/task.py:_resolve_split`（既存 manifest を読む経路）
- 問題: 同じ `split_manifest_path` に対して `held_out_session` を変えて build すると、
  古い fold の manifest が **検証を通って再利用される**。session group は 1 group が 1 split に
  収まっているので `SplitManifest.validate` は何も言わない。
- 根拠（測定、合成 3 session）: `held_out_session="session-0"` で split.json を作った後に
  `held_out_session="session-2"` で build すると `reason=None` で成功し、
  `test` は session-0、**要求した session-2 は train に入る**。理由も警告も出ない。
- なぜ深刻か: `_leave_one_session_out_manifest` の docstring 自身が「fallback すると、
  session をまたいだ汎化を測っているつもりの run が黙って別のものを測る」と書いている、
  その事故が別経路で成立している。step 5 は `run_directory/split.json` を使う設計で、
  `PasteVolumeExperimentConfig.run_directory` の既定は `Path("runs")`（fold 間で共有され得る）。
  fold を取り違えた run は report まで気づけない（`SplitManifest` は次元も held-out も持たない）。
- 直し方: session 次元のときだけ、読み込んだ manifest の test split が
  `resolve_session(held_out)` の session の sample 集合と一致することを確かめ、
  違えば理由を返す（3 行）。
- 確信度: 高（測定済み）。深刻度: 高

### M4. private を直接 import してテストしている

- 対象: `tests/ml/test_seed_roles.py:20-27`（`ml.data.image._derived_seed`、
  `ml.paste_volume.batch._placement_seed`）
- 根拠: `memory/feedback_no_private_test.md`（ユーザー確立の規約）「private 関数・メソッド・
  属性 (`_` prefix) を直接テストしてはいけない」「private を import している既存テストを
  見つけたら指摘・修正対象」「pyright の `reportPrivateUsage` warning が出る import は
  テストでも避ける」。skill `refactor-conventions` / `testing-strategy` も同じ規約を指す。
- 補足: `_derived_seed` は期待値の計算に使っているだけなので、同 file が view-dropout /
  batch-plan で既にやっているのと同じく `int.from_bytes(sha256(material).digest()[:8], "big")` を
  test 内で組み直せば import は消える。`_placement_seed` は private 関数そのものを SUT に
  しており、こちらが規約上の本体。配置の観測点は `collate` の `valid_pixel_mask`
  （`test_batch.py::TestPadding` が既に使っている公開経路）で取れる。
- 先例: `tests/pcbasm/hal/test_audio.py` に同じ形の ignore が 3 箇所ある（規約より前の資産か）。
- 確信度: 規約の存在は高 / 分類（must か should か）は中。深刻度: 中

## should-fix

### S1. 「5 番目の用途」がラベル無しで入っても全緑（規則が機械検証されていない）

- 根拠（測定）: `AugmentationRange.parameters_for` の scale だけを
  `random.Random(_derived_seed(f"{global_seed}:{epoch}:{sample_id}"))` から引く変異
  （＝ docstring が「例外なく」禁じたラベル無し材料の新規追加）を入れても
  `tests/ml` は **333 passed で全緑**。
- `test_seed_roles.py` は 4 用途を手で列挙した表なので、新しい用途は原理的に見えない。
  step 2/3 のレーンが今まさに乱数を使う code を足しているタイミングでもある。
- 提案: `test_architecture.py` と同じ手口で `src/ml` を走査し、
  `_derived_seed(` / `random.Random(f"` の材料に既知の役割ラベルが入っていることを機械検証する
  （計画書「自己検査」節 4 と同じ考え方。走査器は 1 つに保つ）。
- 確信度: 高（測定済み）

### S2. 計画外に足した判断 3 件が、いずれも無検査（変異しても全緑）

| 変異 | 結果 |
| --- | --- |
| `SplitManifest(seed=fold.seed)` → `seed=config.split_seed`（申し送り 6） | **333 passed 全緑** |
| `resolve_session` の空文字ガードを削除（申し送り 5） | **333 passed 全緑** |
| `validate` の `validation_ratio` 範囲検査を削除（申し送り 3） | **333 passed 全緑** |

- `fold.seed` を入れた理由（5 fold の manifest を seed 欄で区別する）を固定する assert が無い。
- 空文字ガードは、外すと `""` が全 session に前頭一致して「複数の session に一致します: ''」
  という別の理由になるため、`test_reports_an_empty_selector`（reason が None でないことしか
  見ていない）は素通りする。理由文まで assert すればガードが固定できる。
- `validation_ratio` の範囲検査にはテストが 1 件も無い（`grep validation_ratio tests/ml` は
  helper 経由の生成のみ）。公開 `validate()` の分岐を無検査で足したことになる。
- 確信度: 高（測定済み）

### S3. LOSO の fold 構成（val 1 / train 3）を CI で固定するテストが無い

- 合成 session のテストは 3 session だけ（`_index(tmp_path, sessions=3)`）で、そこでの構成は
  test 1 / val 1 / **train 1**。実際の主張である「held-out 1 / validation 1 / train 3」を
  見ているのは `skip_if_no_real_sessions` の opt-in テスト 1 件のみで、実データの無い環境では
  丸ごと skip する。
- session 数に対する挙動（実測）:

  | n | 結果 |
  | ---: | --- |
  | 1 | `available=False`「値が 2 種類未満」 |
  | 2 | `available=False`「train/validation 用の group が 2 個未満」 |
  | 3 | (test,val,train) = (1,1,1)、val 実効比 0.500 |
  | 4 | (1,1,2)、0.333 |
  | 5 | (1,1,3)、0.250 |
  | 6 | (1,1,4)、0.200 |
  | 10 | (1,1,8)、0.111 |
  | 13 | (1,**2**,10)、0.167 |
  | 20 | (1,3,16)、0.158 |

  `validation_ratio=0.15` は n<=12 では効かず（`max(1, ...)` の床）、**n=13 で初めて val が 2 に
  増える**。session が 6 本に増えたときは opt-in テストの `assert len(values) == 5` が
  「6 == 5」で落ちるので気づけるが、それは実データのある環境だけで、しかも理由が構成変化だと
  読み取りにくい。
- 提案: 合成 5 session で `(1,1,3)` を固定するテストを 1 件足す（opt-in に依存させない）。
- 確信度: 高（測定済み）

### S4. `SplitManifest.seed` に 64 bit の派生 seed を入れた副作用

- `fold.seed = _derived_seed(f"{seed}:session:{value}")` は最大 2^64-1。`ml.training.random_state.
  seed_everything` は `np.random.seed(seed)` を呼ぶので、この値をそのまま run seed へ流すと
  `ValueError: Seed must be between 0 and 2**32 - 1`（container で確認）。現状 `manifest.seed` を
  読む code は無いので今は壊れないが、step 5 で「split の seed を run seed に使う」と書かれると
  落ちる。
- 併せて `seed` 欄の意味が「利用者が設定した split_seed」から「fold 派生値」へ変わったのに、
  manifest の schema も docstring も変わっていない（`SplitManifest.seed` は `ml.data.split` の
  公開型）。
- 確信度: 中（副作用は測定済み、実害はまだ無い）

### S5. `_built_split` に渡した `groups` が session 経路で使われず、同じ計算を 2 回する

- `_resolve_split` が `index.sample_groups(dimension=config.split_dimension)` を作って
  `_built_split(..., groups=groups)` へ渡すが、session 経路は使わず
  `_leave_one_session_out_manifest` の中で `sample_groups(dimension="session")` を作り直す。
  引数が cell 経路専用になっていて、「同じ次元の groups を使う」という
  `_resolve_split` の docstring の主張が code 上で保証されていない（次元をずらす変異が
  1 箇所では効かない形）。
- 併せて `task.py:_sample_ids_of` は `ml/data/split.py:_sample_ids_in` の再実装。
  ドメイン層が `SplitManifest` を直接組む選択（計画どおり）の副産物だが、
  同じ処理が 2 箇所にある。
- 確信度: 高（読み取り）

### S6. `PasteVolumeTrainingData.build` の docstring が session 経路を説明していない

- 「split が空でないことは確かめ直さない。新規に作る場合は `SplitManifest.build` が
  `require_test=True` のとき各 split へ最低 1 group を割り当て…」という根拠は cell 経路のみ。
  session 経路は `SplitManifest.build` を通らない（`LeaveOneGroupOutPlan` が
  held-out>=1 / validation>=1 / train>=1 を保証する、が正しい根拠）。不変条件の根拠を
  書いた docstring が、2 経路のうち 1 経路しか説明していない。
- 確信度: 高

### S7. 契約外の `split_dimension` が `validate()` を通り、`sample_groups` が None を返す

- 測定: `PasteVolumeTrainingConfig(split_dimension="machine")` は `validate() -> None`（合格）、
  `index.sample_groups(dimension="machine")` は `match` を素通りして **None** を返し、
  `build` は `TypeError: cannot unpack non-iterable NoneType` で落ちる（理由文字列にならない）。
- 追加した `SPLIT_DIMENSIONS` は定義と `__all__` だけで**どこからも使われていない**。
  `validate()` で `split_dimension in SPLIT_DIMENSIONS` を見れば、定数も生き、
  「例外ではなく理由を返す」規約にも合う。
- 参考（step 4 の残課題 1 への回答）: `make_strict_converter()` は PEP 695 の
  `type SplitDimension = Literal[...]` を解決でき、`"cell"` は通り `"machine"` は
  `ClassValidationError` で落ちることを実測した。TOML 経由の入力は cattrs が止めるので、
  実害の口は argv/Python から直に組む経路に限られる。
- 確信度: 高（測定済み）／深刻度は低〜中

### S8. docformatter が日本語を崩した行が新規 docstring に 5 箇所

`memory/MEMORY.md` の「docformatter が日本語を化けさせる／整形結果を受け入れる前に diff を
見る」に該当。文中に空白が入っている:

- `tests/ml/paste_volume/test_index.py`「前頭辞を 渡して複数件の側だけを踏む」
- `tests/ml/paste_volume/test_task.py`「model が session を 言い当てて」
- 同「session をまたいだ汎化はこの次元でしか 測れない」
- 同「実データの本数でしか 「5 fold」は確かめられない」
- 同「どの session を外した のか」
- 併せて `_cell_config` docstring の「選ぶので、 cell 単位 split」も同型
- 確信度: 高（`pre-commit` は pass する＝docformatter の安定出力なので、手で直して
  `# noqa` 相当の回避が要るかは要確認）

### S9. 理由文を見ていない異常系テストが 2 件

- `test_reports_a_dataset_with_too_few_sessions`: `reason is not None` だけ。別の理由
  （例えば held_out 未指定）で落ちても緑になる。他の異常系テストは理由の部分一致を見ている。
- `test_reports_an_empty_selector`: 同上（S2 参照）。
- 確信度: 高

## nit

- N1. `validate` の `not math.isfinite(self.validation_ratio) or not (0 < ... < 1)` は前半が冗長。
  NaN も inf も後半だけで落ちる（`SplitRatios.validate` は `value < 0` と組むので前半が要る、
  という違い）。
- N2. `_derived_seed` の書き換えた docstring: 「`plan_pixel_budget_batches` の `batch-plan` が
  4 用途の全てで、`tests/ml/test_seed_roles.py` が…」は文が壊れている。内容も、
  `batch-plan` の材料に `sample_id` は入らず、`view-dropout` は batch 全体の id 列を使い、
  どちらも `_derived_seed` を通らない（`random.Random(材料文字列)`）ので、
  「`(global_seed, epoch, sample_id)` から種を作る 4 用途」という括りは不正確。
- N3. 同じ不正確さが `tests/ml/test_seed_roles.py` の
  「batch-plan だけ sample を跨いだ並べ替えなので sample_id を含まない」と
  `SAMPLE_SCOPED_ROLES` に view-dropout を入れている点にもある（view-dropout も batch 単位で、
  1 sample の batch のときだけ表と一致する）。
- N4. `PasteVolumeTrainingConfig` の field 並べ替えで `split_seed` の位置が 1 番目から 4 番目へ
  移った。全 field に既定値があるため位置引数呼び出しは黙って意味が変わる（現状 0 箇所）。
- N5. `TestRoleLabels` の 2 件はどちらも test file 内の literal 同士の比較で、src へ 1 段も
  届かない（`_distinct_material_count` の変異は自己検査が捕まえるので対にはなっている）。
  実質の検出力は `TestMaterialsInUse` 側にあり、その一角が M1 で抜けている。
- N6. `resolve_session` の前頭一致は `"sha256:"` 込みの fingerprint に対して行うので、
  `sample_id` の先頭 12 桁（`sha256:` を除いた部分）では当たらない。申し送りには書いてあるが
  docstring には無い。CLI を作る step 5 で踏みやすい。
- N7. `test_task.py` の `_cell_config` / `_session_config` は `**overrides: object` +
  `# type: ignore[arg-type]` なので、override の型は検査されない（既存の書き方の踏襲）。

## 良かった点（測定で確認）

- 「held-out が train / validation に現れない」の自己検査（`test_the_same_observation_finds_
  a_session_that_does_leak`）は効いている。`_leave_one_session_out_manifest` が held-out を
  train へ混ぜる変異で 4 件（実 session 込みで 6 件）落ちる。
- 次元切り替えの誤用は塞がっている。cell manifest を session 次元で読み直すテストは、
  読み直し側の次元を cell へ固定する変異で確実に落ちる。
- `sample_groups(*, dimension)` に既定値を持たせない設計は機能している。pyright は
  省略（`reportCallIssue`）・位置引数・未知の値（`reportArgumentType`）を全て error にする。
  repo 内に次元を渡していない呼び出しは 1 つも残っていない。
- 「最初の fold を常に使う」変異、「session group が cell key を返す」変異、
  「session manifest を cell groups から組む」変異はいずれも複数件で落ちる。

## 変異マトリクス

`.review-mutants` 上で実測。対象テストは `tests/ml/test_seed_roles.py` +
`tests/ml/paste_volume` + `tests/ml/data` の 333 件。

| # | 変異 | 落ちたテスト |
| --- | --- | --- |
| S1 | augmentation の役割ラベルを外す（step 0 以前） | 2 件 |
| S2 | augmentation のラベルを sample_id の後ろへ | 1 件 |
| S3 | augmentation のラベル名を `augment` へ | 1 件 |
| S4 | `_PLACEMENT_ROLE="augmentation"`（材料衝突） | 2 件（うち閾値テスト 20 >= 28） |
| S5 | placement のラベルを外す | 2 件 |
| S6 | view-dropout のラベルを外す | 2 件 |
| **S7** | **view-dropout のラベルを `augmentation` へ（材料衝突）** | **seed_roles は 0 件**（M1） |
| S8 | batch-plan のラベルを外す | 2 件 |
| S9 | session group が cell_key を返す | 9 件 |
| S10 | held-out group を train へ混ぜる | 4 件 |
| S11 | 読み直しの検証を常に cell groups で行う | 1 件 |
| S12 | `validation_count` の `max(1, ...)` を外す | 1 件 |
| S13 | 常に `plan.folds[0]` を使う | 2 件 |
| **S14** | **manifest の seed を config の split_seed に戻す** | **0 件**（S2） |
| **S15** | **`resolve_session` の空文字ガードを削除** | **0 件**（S2） |
| **S16** | **`validation_ratio` の範囲検査を削除** | **0 件**（S2） |
| S17 | cell 次元の held_out_session 検査を削除 | 1 件 |
| S18 | session manifest を cell groups から組む | 6 件 |
| **S20** | **5 番目の用途をラベル無し材料で足す** | **0 件**（S1） |
| T1 | `_distinct_material_count` を常に `len` へ | 1 件（自己検査が捕捉） |
| T2 | `_distinct_material_count` を常に 1 へ | 1 件 |
| T3 | `_expected_view_indices` が材料を無視 | 1 件（※ 差し替えた材料が偶然 count=1 だったため。count=5 の材料に変えると素通りする＝M1 と同根） |
| T4 | `_expected_batch_plan` が材料を無視 | 1 件 |
| T7 | MATERIALS 表を衝突させる | 2 件 |

## 検証結果（`fb65491` を凍結した複製に対して container 内で実行）

- format（`pre-commit run --files` 対象 8 file）: **pass**（再整形なし）
- 型検査（pyright、`src/ml` + `tests/ml` の 118 file）: **pass**（0 errors, 0 warnings）
- `tests/ml`（`-m "not hardware and not e2e"`）: **pass**（1502 passed, 1 skipped。
  実 5 session の opt-in テストも実行し緑）
- `make test` / `make run` / `pytest -m hardware` は実行していない
- 実行環境: 作業ツリーでは step 2/3 の別 agent が `src/ml/paste_volume/task.py` などを
  編集中だったため、レビュー対象の状態を `.review-mutants/`（`fb65491` 相当の複製）へ
  固定し、`PYTHONPATH` を差し替えて計測した。複製は削除済み、repo は無変更。
