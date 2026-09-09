# paste_volume ドメイン層（data + task）レビュー

MR !214 / `feature/2026-09-09/paste-volume-data-task`。対象は `git diff origin/main...HEAD`。

## verdict: request-changes

must-fix 2 件。うち M1 は「削ったコードが本当に不要か」（依頼の観点 4）への答えで、
削除そのものが観測されない欠陥を作っている。

## must-fix

### M1. placement seed が augmentation seed と完全一致する（`batch.py:219`）

確信度：**高**（数値で再現確認済み）。深刻度：高（エラーにならず統計的に壊れる）。

```python
digest = sha256_bytes(f"{global_seed}:{epoch}:{sample_id}".encode())
return int(digest[:16], 16)
```

`AugmentationRange.parameters_for` は
`_derived_seed(f"{global_seed}:{epoch}:{sample_id}")`
= `int.from_bytes(sha256(同じ文字列).digest()[:8], "big")` を使う
（`src/ml/data/image.py:165` / `:475`）。`sha256_bytes` は素の hexdigest なので
`int(hexdigest[:16], 16) == int.from_bytes(digest[:8], "big")`。**両者は同じ整数**。

- 検証: 任意の `(global_seed, epoch, sample_id)` で 100% 一致
- 結果として `random.Random(seed)` が同じ初期状態から回るので、回転角と配置位置が
  独立でなくなる。実測（sample 20000、spare=3）:
  **rotation < 180° の sample では `top == floor(rotation / 45°)` が厳密に成立**。
  (rotation octant, top) の 32 通りのうち 20 通りしか出現しない
- 原因は `_placement_seed` から `split` を落としたこと。削除前は
  `f"{global_seed}:{split}:{epoch}:{sample_id}"`（commit 49749f1）で衝突していなかった。
  「split を区別しても観測できる違いが生まれない」は正しいが、**区別を消した結果
  augmentation の seed と材料が byte 一致した**という副作用が見落とされている
- 直し方: `ViewDropout` と同じく材料へ役割 label を入れる
  （`f"{global_seed}:{epoch}:placement:{sample_id}"`）
- 観測点が無い理由: `TestPadding` は配置を切り出すためにわざと
  `AugmentationRange(rotation_enabled=False, ...)` を使うので、回転との相関を
  構造的に見られない。回転を有効にしたまま「同じ rotation octant の中で 4 通りの
  offset が出る」を見る観測点が要る

### M2. index の `constraints` と collator の `constraints` が一致する保証が無い

確信度：高。深刻度：中〜高。

`PasteVolumeSampleIndex.from_roots(..., constraints=A)` と
`PasteVolumeCollator(constraints=B)` は独立に渡せる。`PasteVolumeTrainingData.build`
は `collator.constraints` しか見ず、index がどの constraints で拒否判定したかを
知らない（`task.py:95-97`）。

- 設計の柱は「拒否は index build で済ませる」（計画書「sample の拒否は index build で
  済ませる」）。A ≠ B だとこの前提が無条件に崩れる
- A が緩い場合: 通した sample が `materialize` で `ValueError`（大きい声で落ちる）
- A が厳しい場合: 余分な cell が `rejections` へ落ち、**学習母集団が黙って減る**
- `smallest_source_size` は constraints に依存しないので、`validate_augmentation` は
  この食い違いを検出しない
- 直し方: index が使った `constraints` を `PasteVolumeSampleIndex` へ持たせ、
  `PasteVolumeTrainingData.build` で `collator.constraints` との一致を検証する
  （または `build` が index の生成まで引き受ける）
- 観測点も無い。既定値どうしがたまたま一致しているため全テストが緑

## should-fix

### S1. `src/pcbasm/pasting/paste_volume/__init__.py` の docstring が古い

確信度：高。「予約 namespace」「現時点では中身を持たない」と書いてあるが、本 MR で
5 module が入った。`src/pcbasm/pasting/README.md` は更新済みなので、package docstring
だけ取り残されている。

### S2. `build` docstring の「空 split は作られない」は load 経路に当てはまらない

確信度：高。深刻度：低。`task.py:86-89` の根拠は `SplitManifest.build` の性質だが、
`_resolve_split` は `split_manifest_path` が存在すれば `SplitManifest.load` の結果を
使う。`SplitManifest.validate`（`ml/data/split.py:148`）は fingerprint・重複・過不足・
group またぎを見るが、**各 split が非空かは見ない**。`require_test=False` で作った
manifest を渡せば test split が空のまま通る。docstring は無条件の主張になっている。

### S3. `dataset_fingerprint` が `constraints` を含まない

確信度：高。深刻度：中。M2 と同根。`_dataset_fingerprint` は
`{schema_version, session fingerprints}` だけ。しかし entries を決めるもう 1 つの入力は
`constraints`（拒否判定）。異なる constraints で作った index が同じ fingerprint を
名乗るので、`SplitManifest.load` の fingerprint 照合と、Trainer の
`dataset_fingerprint` 照合（resume）が、実際には違う sample 集合を同一視する。
split manifest 側は `validate` の過不足検査が救うが、checkpoint 側は救われない。

### S4. `dataset.py:49` の docstring が保証していない検出を主張している

確信度：高。深刻度：低。「破れていれば decode が例外を投げる」は path 実在にしか
当てはまらない。画像が別寸法へ差し替わっても `decode_rgb_image` は成功し、
`preprocess` も成功し、padding が batch 内 max へ合わせるので**黙って**
`plan_epoch` の計画とずれる。まさに本 MR が繰り返し警戒している壊れ方。

### S5. `manifest.validate(...)` の呼び出しに観測点が無い

確信度：高（call-site 変異の生存）。深刻度：中。`task.py:218-221` を丸ごと落としても
全テストが緑になる。`test_rejects_a_manifest_built_for_another_dataset` は
`SplitManifest.load` の fingerprint 照合で先に落ちるため、`validate` を通っていない。
S3 のとおり「同じ fingerprint で sample 集合が違う」は実際に作れるので、この呼び出しは
load-bearing。

### S6. `sorted(cell.views, key=view.number)` に観測点が無い

確信度：高。深刻度：低。`index.py:230`。合成 fixture（`helpers._view_geometry`）が
0..n の昇順で書くので、`sorted` を外しても
`test_orders_views_by_number_and_resolves_absolute_paths` は緑のまま。view を降順で
書く fixture が要る。

### S7. shape 一致テストが stride 幅の不一致を見逃す

確信度：高。深刻度：中（観測点の強度）。
`test_batch.py::test_the_planned_shape_matches_what_collate_produces` と
`test_task.py::test_the_planned_shape_bounds_the_materialized_batch` は
`padded >= planned` かつ `padded - planned < stride` しか見ない。padding は 8 の倍数へ
切り上げるので、**計画 shape が実 shape と最大 7 px ずれても緑**。依頼の観点 1 の本命が
この強度で守られている。`stride=1` の `ImageConstraints` で厳密一致を見るか、
`PreprocessedMultiViewSample.preprocess` の出力 shape と直接突き合わせるべき。

### S8. `test_architecture.py` は直接 import しか見ない

確信度：高。深刻度：中。docstring は「装置 HAL へ届く import が 1 本でも入ると collect
できなくなる。規約ではなくテストで固定する」と主張するが、走査するのは
`paste_volume` ツリー自身の import 文だけ。**本 MR のブロッカーだった経路**
（`metadata.py` → `applicator.py` → `pcbasm.hal` → picamera2）は、`metadata.py` 側が
再び hal を引いても検出できない。実際に守っているのはコンテナでの collect であって
このテストではない。docstring を実態に合わせるか、subprocess で
`import pcbasm.pasting.paste_volume.session` を通す形にする。

### S9. `global_seed` が 2 か所に独立に存在する

確信度：中（意図的な分離の可能性あり）。深刻度：低。
`PasteVolumeCollator.global_seed`（augmentation / view dropout / placement）と
`PasteVolumeTrainingConfig.global_seed`（batch plan の並べ替え）。同名・同既定値で、
一致を強制も記録もしない。片方だけ変えると「seed を変えた」つもりが半分しか効かない。
分離が意図なら docstring で役割を書き分ける、そうでなければ 1 つにまとめる。

### S10. `measured_volume_ul` / `pixel_per_mm` の有限性を誰も見ていない

確信度：高（`json.loads` は既定で `NaN` / `Infinity` を受理する）。深刻度：低〜中。
`_validate_cells`（`session.py:148`）は `pixel_per_mm <= 0` を弾くが `NaN <= 0` は False
なので素通りする。`parse_metadata` も DTO の `validate()` を呼ばない。結果として
`conditioning = log(nan)`、`target = nan` が batch に載る。
`PasteVolumeBatch.validate()` を消したので受け皿も無い（削除自体は妥当だが、
消した検査のうち「値が有限か」だけは自己検算ではなくデータ検証だった）。
`pcbasm.utils.is_finite_number` で index/session 側に置くのが筋。

### S11. 到達不能な `# pragma: no cover` 分岐（`index.py:142-144`）

確信度：高。`_entry_for_cell` の契約上 `entry is None` なら `reason` は非 None。
型的にも `f"...{reason}"` は None で通るので narrowing のためでもない。
AGENTS.md 開発原則 2（起こり得ないシナリオ向けの処理を増やさない）に反する。
リポジトリ内の `pragma: no cover` は他に 1 件（`TYPE_CHECKING` ガード）だけ。

### S12. `_validate_image_sizes` の `del stack`（`index.py:305-310`）

確信度：高。深刻度：低。`for stack, view in zip(stacks, views, strict=True)` の
`stack` を使わず `del` で黙らせている。`stacks` は `views` から作るので長さは常に一致する。
`for view in views` で足りる。

### S13. Makefile の対象拡大にドキュメントが追随していない

確信度：高。`ML_TEST_PATHS` / `ML_TYPE_PATHS` に paste_volume を足したのに、
次が「`tests/ml` だけ」と書いたまま。

- `AGENTS.md:83`（`make test-ml`）、`AGENTS.md:106`、`AGENTS.md:118`
- `CLAUDE.md:16`
- `docker/README.md:69`、`:70`、`:87`（pyright の対象を明示的に列挙している）

### S14. `PasteVolumeSampleIndex.entry_for` が線形探索

確信度：高。深刻度：低（現状 331 entry）。`plan_epoch` は split の全 sample について
`entry_for` を呼ぶので epoch あたり O(n^2)。`@attrs.frozen` なら
`functools.cached_property` の dict で潰せる。

### S15. テストの `type: ignore` 6 件

確信度：高。深刻度：低。`**overrides: object` のヘルパーが原因。
リポジトリ全体のテスト 23 件のうち 6 件が本 MR、`tests/ml` は 0 件。
ヘルパーを keyword 引数で明示すれば消える。

### S16. 拒否スクリーニングは 1 通りの組み合わせしか見ていない（観点 3 への答え）

確信度：高（構造上の事実）。深刻度：低（実データでは踏まない）。
index build は「全 view × `NO_AUGMENTATION`」でだけ `preprocess` を通す。学習時は
(a) view の部分集合、(b) 回転で狭まった有効領域、の上で標準化する。どちらも分散を
下げる方向に働くので、build を通った sample が `materialize` で
「分散が小さすぎます」に落ちうる。落ちれば `ValueError` なので黙って壊れはしないが、
「build 時に隔離済み」という不変条件は厳密には成り立っていない。docstring と
計画書の主張をこの粒度へ落とすか、screening を最小 view 数でも通すかのどちらか。

なお `ImageConstraints.validate_augmentation` の保証も、`_constraint_scale < 1`
（`maximum_pixels` に当たる大きい画像）が混ざると成立しない。53 px では
`limit ≈ 9.66` なので現状は安全。

### S17. `session_sample_count` が拒否した cell を含む

確信度：高。深刻度：低。`index.py:139` の `sample_count=len(session.cells)` は
rejection を差し引く前の値。拒否が出た session では weight の総和が 1 未満になり、
「各収集 session が同程度の寄与を持つ」（計画書）という狙いから少しずれる。
実データは拒否 0 なので現状は無害。

## nit

- `paste_volume` の docstring 20 箇所で、docformatter の折り返しが日本語文中に空白を
  残している（例 `dataset.py:49` 「不変条件なので、 ここでは検査しない」、
  `test_index.py:69` 「1 cell から できる ID は」）。文字化けは無い
- `helpers.py` の `__all__` が非整列（`MEASURED_RATIO` が `PERIPHERAL_VIEW_COUNT` の後）
- 関数内 import 2 箇所（`test_task.py` の `ViewDropout` と `PASTE_VOLUME_DATASET_DIR`）
- `test_rejects_an_empty_batch` の `tmp_path` が未使用
- `sample_id` の 48 bit 前置き衝突が起きると、`sample_groups()` が dict なので
  entry が 1 件黙って消える（確率は無視できる。`plan_pixel_budget_batches` の重複検査には
  届かない）
- `INDEX_SCHEMA_VERSION` を `_dataset_fingerprint` から落としても観測できない
- `conditioning` は `floor` 前の nominal scale を使う。53 px × 0.5 で実効 0.4906 に対し
  0.5 を報告し、最大 1.9% ずれる（`ml` 側 `PreprocessedSample.scale` の意味論）
- `view.pre == view.post` を弾いていない（pre/post が同一ファイルの session を受理する）
- `PasteVolumeBatch` の `eq=False` に理由の記載が無い（`ml` 側の同型 class は書いている）
- `from_roots` の失敗理由が `directory.name` だけで、複数 root のとき曖昧
- `docs/image-based-dispense-calibration-ml-plan.md` の sample index フィールド一覧が
  `PasteVolumeSampleEntry` と未整合（`source_ids` / `paste_id` / `paste_lot` /
  `nozzle_diameter_mm` / plate 寸法 / `label.kind` を実装は持たない）。sample_id と
  loss weight の段落だけ更新されている
- `memory/agents/orchestrator/paste-volume-data-task.md` の「残タスク step 6」が古い
  （計画書は本 MR で更新済み）

## 良かった点（削らない判断の確認）

- `plan_epoch` と `collate` の shape 一致は、`ImageShape.preprocessed` を両側で
  共有し、`entry.source_*` を実 decode から取り、session 側で `pixel_rect` /
  `crop_size_px` との一致を検証する三段で構造的に閉じている（S7 は観測点の弱さの話で、
  構造そのものは妥当）
- `_bucket_key` が `view_count` を含むので、`_view_indices` の「batch 内で view 数が
  揃う」要求は plan 側から保証されている
- `training = (split == "train")` は `ml/training/loop.py:651,662` の実装と一致し、
  食い違いは `materialize` で `ValueError` になる（黙っては壊れない）
- `plan_epoch` の `if not shapes: return ()` 削除は安全。`plan_pixel_budget_batches`
  が空入力で `()` を返す
- `_resolved_image_paths` の `sorted()` 削除は正しい（`canonical_json(sort_keys=True)`
  が dict を正規化する）
- `PasteVolumeBatch.validate()` 削除は形の検算としては妥当（S10 の値検証だけ別問題）

## 検証結果

- make format: pass（`make ml-docker-check` の `pre-commit run -a`）
- make type: pass（`pyright src/ml tests/ml src/pcbasm/pasting/paste_volume tests/pcbasm/pasting/paste_volume scripts/ml_smoke.py`）
- make test-no-hardware: **未実行**（学習機側の環境。実機側の実行はユーザー担当）
- `make ml-docker-check`: **pass**（1443 passed / 1 skipped / exit 0）

計画の step 0a は「装置ドメインの共有コードを触るので、ユーザーが実機側で
`make test-no-hardware` を通すまで先へ進めない」としている。`applicator.py` /
`dataset/metadata.py` / `dataset/pending.py` / `tests/pcbasm/pasting/dataset/test_metadata.py`
を触っているので、実機側の実行結果を確認したい。

---

# 2 巡目レビュー（`f004a79` 追加後）

## verdict: request-changes（軽微。must-fix は観測点 1 件のみ）

M1 / M2 の実装は正しく直っている。S10 は**私の誤検出**で、退けた判断が正しい。
残る must-fix は、今回足した観測点のうち 1 件が狙った変異を殺せていないこと。

## must-fix

### M3. `test_the_transitive_scan_follows_more_than_one_hop` が 1 段しか検証していない

`tests/pcbasm/pasting/paste_volume/test_architecture.py`。確信度：**高**（実行で確認）。

起点にしている `src/pcbasm/pasting/applicator.py` は **27 行目で
`from pcbasm.hal import Klipper, PasteDispenser, Speed, XYZStage` と直接 import している**。
docstring の「2 段先の `pcbasm.hal` へ到達するはず」は事実に反し、実際は 1 段。

`_reachable_modules` から `pending.append(target)`（再帰）を落とす変異を当てて実測:

```
recurse=True : reaches pcbasm.hal -> True
recurse=False: reaches pcbasm.hal -> True
```

`test_nothing_reaches_the_device_hal_through_a_chain` は「到達しないこと」の assert
なので、走査を弱めるほど通りやすくなる。つまり**再帰を丸ごと落としても全テストが緑**で、
検査は元の直接 import 版へ黙って退化する。M1 の観測点でやったのと同じ失敗
（bug 下でも通る閾値）が、こちらでは残っている。

直し方: 起点を「hal へ 2 段以上でしか届かない module」にする。実測した候補:

- `src/pcbasm/pasting/dataset/recorder.py`（`applicator` 経由。**本 MR のブロッカーと
  同じ形**なので自己検査として最も筋が良い）
- `dataset/capture.py`、`flowcalib/procedure.py`、`toolhead_offset.py`

## should-fix

### S18. shape の厳密一致は依然として stride 幅に丸められている（S7 の積み残し）

確信度：高。深刻度：中。

- `test_task.py::test_the_planned_shape_bounds_the_materialized_batch` は
  `padded == _ceil_to(max(planned), stride)` になった。しかし `padded` は
  `_ceil_to(max(actual), stride)` なので、**planned と actual が同じ 8 px 窓に入る限り
  一致する**（例: planned 50 / actual 52 → どちらも 56）
- `test_batch.py::test_the_planned_shape_matches_what_collate_produces` は
  1 巡目のまま（`padded - planned < stride`）で、こちらは最大 7 px のずれを見逃す

厳密に見るなら次のどちらか。

- `ImageConstraints(stride=1)` の collator で `padded == planned` を見る
- 回転を切った augmentation（`rotation_enabled=False`、scale は振る）で
  `_valid_region` / `_mask_offset` から有効画素の bounding box を取り、
  `preprocessed_shape` と厳密比較する（回転が無ければ bbox == 前処理後 shape）

### S19. 乱数種の材料に役割ラベルを強制する仕組みが無い

確信度：中（設計提案）。深刻度：低。

今回の直しは呼び出し側の literal 1 つに依存している。`ml` 側の
`AugmentationRange.parameters_for` は**ラベル無しの `{global_seed}:{epoch}:{sample_id}`
を占有したまま**なので、次にドメイン層が種を足すとき同じ罠を踏める。

- `ml/data/image.py` に「この材料は augmentation が予約している」と明記する
- あるいは `ml` 側へ `derive_seed(role, ...)`（role 必須）を出し、
  `_placement_seed` と `parameters_for` を両方そこへ寄せる

`parameters_for` 側にラベルを足すと既存の augmentation 系列が変わる。**checkpoint を
1 つも作っていない今なら無料**で、学習を回した後は高くつく。

### S20. 未対応 should-fix のうち「最初の学習を回す前」に片付けたいもの

merge を止めるものは無い（確信度：高）。ただし次の 2 つは**値が変わる変更**なので、
checkpoint を作った後に直すと過去の run と比較できなくなる。

- **S9 `global_seed` の二重化**（collator と config）。統合すると batch plan と
  augmentation の両方の系列が動く
- **S17 `session_sample_count` が拒否済み cell を含む**。直すと loss weight が動く

残り（S14 `entry_for` の線形探索、S15 `type: ignore`、docformatter の折り返し空白）は
純粋な整理で、いつ直しても影響が無い。

### S21. docs 同期の取りこぼし 2 行

確信度：高。深刻度：低。

- `docker/README.md:69` — `make ml-docker-test    # tests/ml を実行する`。この target も
  `ML_TEST_PATHS`（tests/ml + paste_volume）を回す
- `AGENTS.md:83` — `make test-ml`: `tests/ml` だけを実行`。同上

## nit（2 巡目で増えたもの）

- 関数内 import が 2 件から 3 件へ（`test_task.py:236` の `import attrs`。
  `test_index.py` は同じ commit で top-level へ入れているので不揃い）
- `test_the_planned_shape_bounds_the_materialized_batch` の docstring が
  「stride の中で一致する」のまま。assert は `==` になった
- `test_reports_a_number_that_is_not_finite` が他 module のメッセージ文字列
  「有限なfloatが必要です」を直接 pin している。`metadata.py` の文言変更で
  paste_volume のテストが落ちる

## 誤検出だったもの（取り下げ）

### S10（非有限の数値）— **私の誤り。対応方針が正しい。**

`_make_metadata_converter`（`src/pcbasm/pasting/dataset/metadata.py:286-295`）が
`converter.register_structure_hook(float, strict_float)` で **すべての `float` 注釈**へ
`math.isfinite` 検査を掛けている。`parse_metadata` は必ずこの converter を通るので、
`pixel_per_mm` も `measured_volume_ul` も NaN / inf の時点で
「有限なfloatが必要です」で落ちる。`_validate_cells` に検査を足しても到達不能だった。

追加検査を取り消して schema 層の性質をテストで固定した判断は正しい。

## 確認できたこと（質問への回答）

1. **M1 の直し方は十分。** 整数種を作る経路を全部数えた:
   `ml/data/image.py:165`（augmentation）、`batch.py:227`（placement）、
   `ml/data/split.py:247`（leave-one-group-out）、`ml/data/split.py:92`（split_seed の生値）。
   文字列種は `ml/data/batch.py:101`（`view-dropout`）と `:263`（`batch-plan`）で、
   `random.Random` の文字列 seed は sha512 経由なので整数種と衝突しない。
   材料の重複は無くなっている。残る懸念は S19（仕組みで防いでいない）だけ
2. **S10 は誤検出。** 上記
3. **`constraints` を fingerprint へ入れた互換性の問題は無い。**
   `dataset_fingerprint` は本 MR で初めて生まれる値で、既存の checkpoint も
   split manifest も存在しない。`attrs.asdict` の dict は `canonical_json` が
   キー順を正規化するので安定。`INDEX_SCHEMA_VERSION` も同じ dict に入っている
4. **新しい観測点 6 件のうち 5 件は狙った変異を殺す。** 1 件（M3）が殺さない
   - 配置と回転の独立性: **殺す**。bug 下では 32 組中 20 組しか到達不能なので、
     閾値 28 は原理的に届かない。3 sample × 60 epoch = 180 draw で
     期待到達 31.9 組、余裕もある
   - constraints の不一致 / fingerprint への反映 / index への保持: 殺す
   - view の並べ替え: 殺す（metadata を逆順にした fixture）
   - sample 単位 manifest の拒否: 殺す。`SplitManifest.load` の fingerprint 検査を
     通ってから `validate` だけが弾く経路になっている（S5 の穴は塞がった）
   - 推移的 import: **殺さない**（M3）
5. **未対応の should-fix に merge を止めるものは無い。** ただし S20 の 2 件は
   最初の学習を回す前に決着させたい

## 検証結果（2 巡目）

- make format: pass
- make type: pass
- make test-no-hardware: 未実行（実機側。ユーザー担当）
- `make ml-docker-check`: **pass**（1457 passed / 1 skipped / exit 0）

1 巡目と同じく、step 0a の共有ドメイン変更（`applicator.py` /
`dataset/metadata.py` / `dataset/pending.py`）について実機側 `make test-no-hardware` の
結果確認が残っている。
