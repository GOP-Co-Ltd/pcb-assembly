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

---

# 3 巡目レビュー（`e8ecfb3` 追加後）

## verdict: approve

M3 / S18 / S9 / S17 / S19 / S21 いずれも適切に対応されている。残る指摘は
should-fix と nit だけで、merge を止めるものは無い。

## 確認したこと

### M3（自己検査の 1 段問題）— 解消

起点を `dataset/recorder.py` へ替え、「直接は届かない」「推移的には届く」の 2 本立てに
なった。`recorder.py` の直接 import に `pcbasm.hal` が無いことを確認済み
（`config` / `pasting.applicator` / `dataset.*` / `utils` / `vision.*` のみ）。

再帰（`pending.append`）を落とす変異を再現して実測:

```
recurse=True : selfcheck2hop=True
recurse=False: selfcheck2hop=False   → killed
```

### S18（shape の厳密一致）— 解消

`mask.any(dim=1).sum()` を `preprocessed_shape.height` と厳密比較する形になった。
回転が入っても成立することを確認: `_valid_pixel_mask` は target 矩形を回した mask で、
正方形を任意角度で回すと縦方向の広がりが `26.5·(|sinθ|+|cosθ|) ≥ 26.5` になるため、
canvas の上端行・下端行・左右端列に必ず有効画素が残る。よって「有効画素を持つ行数」
＝前処理後の高さが常に成立する。padding 由来の丸めが挟まらないので 1 px のずれも殺せる。

### S9 / S17 / S21 — 解消

`config.global_seed` は完全に消え、`collator.global_seed` が唯一の出典
（`plan_pixel_budget_batches` の `seed` もそこから取る）。`batch-plan` は文字列種なので
整数種の augmentation / placement とは衝突しない。docs 2 行も更新済み。

## should-fix（今回の変更から新たに見つかったもの）

### S22. 推移的 import 検査が相対 import を追わない

確信度：**高**（実測）。深刻度：中。

`_imported_modules` は `isinstance(node, ast.ImportFrom) and node.level == 0` で
**相対 import を捨てている**。`src/` には相対 import が 61 本あり、とくに
`src/pcbasm/vision/__init__.py` は 5 module すべてを相対 import で re-export している。

paste_volume の起点集合から実測した到達 module 数:

```
相対 import を無視（現状）: 67
相対 import を解決        : 85
見落とし: pcbasm.vision.calibration / copper / detection / overlay、
          pcbasm.geometry.path / polygon / polyline / routing / sampling / transform ほか
```

`paste_volume` は `metadata.py` 経由で `pcbasm.vision` に到達する（ancestor 規則で
`vision/__init__.py` が走る扱いになる）。**そこから先の 4 module が完全に不可視**なので、
`pcbasm/vision/detection.py` などが `pcbasm.hal` を引いた瞬間に、検査は緑のまま
コンテナだけが壊れる。M3 と同じ「guard が黙って過小になる」形。

`docker/README.md` が「その到達範囲は `test_architecture.py` が推移的に検証する」と
書いてしまったので、記述と実態が食い違っている。

直し方は `_imported_modules` に `node.level > 0` の解決を足すだけ（file の package path
から `level-1` 段さかのぼって `node.module` を連結する）。

### S23. ancestor 展開に観測点が無い

確信度：高（実測）。深刻度：低。

`for depth in range(1, len(parts) + 1)` を「完全修飾名だけ」に縮める変異を当てても、
3 本の architecture test は全部緑のまま（`ancestors=False` で `selfcheck2hop=True`、
本体検査の offender も空）。docstring はこの規則を「ブロッカーは `vision/__init__` が
走る形だった」と根拠づけているので、その根拠が観測されていない。

S22 を直すと `vision/__init__.py` の中身を追えるようになるので、
「`pcbasm.vision.image` を import した起点から `pcbasm.vision.detection` へ到達する」
を見る観測点が自然に置ける。

### S24. `session_sample_count` は entries から導出できる（S17 の書き方）

確信度：中（設計の好み）。深刻度：低。

現状の仮値方式そのものは安全に閉じている。`_entry_for_cell` は private で `build` しか
呼ばず、`entries.extend(generator)` は `usable` を再代入する前に同じイテレーション内で
消費し切る。0 が漏れれば `1.0 / 0` で loud に落ちる。

ただし `session_sample_count` は `Counter(entry.session_fingerprint for entry in entries)`
から導出できる**非正規化フィールド**で、仮値の往復はそれを entry に持たせたことの帰結。
より素直なのは次のどちらか。

- entry から落とし、`PasteVolumeSampleIndex` に
  `session_sample_counts: Mapping[str, int]`（または `sample_weight_for(sample_id)`）を
  置く。`collate` はそこを引く
- entry に残すなら、`attrs.evolve` の generator を list 内包表記にする
  （遅延 generator が可変 local を読む形は、将来 `usable` の寿命を変えたとき壊れる）

## 質問への回答

1. **M3 の直し方は妥当。** ただし自己検査の穴は 2 つ残る（S22 の相対 import、
   S23 の ancestor 展開）。S22 は「弱い自己検査」ではなく走査本体の穴で、
   こちらのほうが実害が大きい
2. **S17 の仮値方式は安全だが、より素直な形はある**（S24）
3. **S19 は次 MR 送りで良い。** 判断の根拠:
   - `ml` の種を変えると `tests/ml` の期待値（乱数列に依存するもの）が動きうる。
     本 MR のスコープ（data + task）を越える
   - docstring で罠が明示され、`_derived_seed` の呼び出し側は現状 1 つだけ
   - ただし**期限は「次 MR で Trainer を初めて回す前」**。checkpoint を 1 つでも
     作ると augmentation 系列の変更が過去 run との比較を壊す。次 MR の計画へ
     この順序制約を書いておくこと
4. **approve できる。** 未対応（`entry_for` の線形探索、テストの `type: ignore` 6 件、
   docformatter の折り返し空白）が merge を止めないという 2 巡目の判断は変わらない。
   S22 だけは 3 行程度で直り、かつ `docker/README.md` の記述と食い違うので、
   本 MR に入れるか直後の follow-up にするかを選べる状態にしておくのが良い

## 積み残し（merge 後で良い）

- S22 / S23（architecture test の走査）— S22 は早めが望ましい
- S24（`session_sample_count` の持ち方）
- S14 `entry_for` の線形探索
- S15 テストの `type: ignore` 6 件（`tests/ml` は 0 件）
- 関数内 import 3 件（`test_task.py:180` / `:236` / `:497`）
- `test_batch.py::test_the_planned_shape_matches_what_collate_produces` が
  1 巡目のままの緩い版（`< stride`）。task 側で厳密に見るようになったので害は無いが、
  docstring の「一致する」と assert の強度が合っていない
- docformatter の折り返しで日本語文中に残る空白 20 箇所

## 検証結果（3 巡目）

- make format: pass
- make type: pass
- make test-no-hardware: 未実行（実機側。ユーザー担当）
- `make ml-docker-check`: **pass**（1458 passed / 1 skipped / exit 0）

step 0a の共有ドメイン変更（`applicator.py` / `dataset/metadata.py` /
`dataset/pending.py` / `tests/pcbasm/pasting/dataset/test_metadata.py`）について、
実機側 `make test-no-hardware` の結果確認だけが残る。

______________________________________________________________________

# 4 巡目レビュー（`918b760..HEAD`。ドメイン層を `ml` へ移した作り替え）

対象 4 commit: `25bfb04` / `f0896b6` / `a8c5b73` / `f8e2491`。

## verdict: request-changes

must-fix 2 件。うち M4 は依頼の重点 A（構造契約の検出力）への答えで、**ごく普通の
import 文 1 行で契約 3 を無効化できる**。実測で確認し、修正方向の妥当性も実測した。

移動そのもの（観点 B）と `docker/ml/`（観点 C）は問題を見つけていない。

## must-fix

### M4. `from <package> import <submodule>` 形が走査から丸ごと抜ける

`tests/ml/test_architecture.py:167-187`（`_absolute_imports`）。
確信度：**高**（コンテナで実測）。深刻度：**高**。

`ast.ImportFrom` から `node.module` しか記録せず、`node.names` の alias を submodule
候補として足していない。結果、`from pkg import submodule` 形の import が
「pkg を読んだ」としか記録されず、submodule 自身が走査されない。

実測（`src/ml/paste_volume/_probe_tmp.py` に 1 行入れて `tests/ml/test_architecture.py` を実行）:

```
from pcbasm.pasting.dataset import capture    -> 22 passed （見逃し）
from pcbasm.pasting.dataset import recorder   -> 22 passed （見逃し）
import pcbasm.pasting.dataset.recorder        -> 2 failed  （検出）
from pcbasm.pasting.dataset.recorder import X -> 2 failed  （検出）
```

`capture` は `pcbasm.hal` / pcbnew / picamera2 へ、`recorder` は `applicator` 経由で
`pcbasm.hal` / picamera2 へ届く。どちらもコンテナでは import した瞬間に collect が落ちる。

同じ穴は `tests/ml` 側にもある。`tests/ml/_probe_tmp.py` に
`from tests import helpers` を入れても **22 passed**
（`test_no_test_module_imports_the_device_test_helper` が素通り）。

これが効く理由:

- `pcbasm/pasting/dataset/__init__.py` は「このパッケージは re-export を持たない。
    利用側は必要なサブモジュールを直接 import する」と書いており、`from pkg import
    submodule` 形は自然に選ばれる書き方
- 許可リストは package 単位なので、`pcbasm.pasting.dataset` は契約 2 も通る。
    つまり**契約 2 と契約 3 の両方を同時にすり抜ける**
- module docstring は「CI は picamera2 のある Raspberry Pi で走るので、実行時に落ちる
    ことにも頼れない」と書いている。その主張がこの形では成立しない

修正方向（実測済み・こちらでは適用していない）: `ImportFrom` で `origin` に加えて
`f"{origin}.{alias.name}"` も `imported` へ入れる。

```
修正案のみ          -> 22 passed（baseline 緑のまま）
修正案 + capture    -> 3 failed（pcbasm.hal / pcbnew / picamera2）
修正案 + tests import helpers -> 4 failed
```

過剰に記録される `pkg.ClassName` は `_module_file` が解決できないので走査は伸びず、
`_is_domain_data` も許可 package の prefix 一致なので誤検出しない。

### M5. `src/ml/__init__.py` の docstring が、本 MR が撤回した不変条件を宣言したまま

`src/ml/__init__.py:3-5`。確信度：高。深刻度：中（ドキュメントのみ）。

```
PyTorch による実験・評価・最適化・export の共通部分を提供する。装置制御ドメイン
(:mod:`pcbasm` / :mod:`web`) を一切参照せず、依存の向きは常にドメイン側から
``ml`` への一方向とする。
```

AGENTS.md・CLAUDE.md・仕様書 §7・`src/pcbasm/pasting/README.md`・`docker/ml/README.md`
はすべて書き替わったのに、方針の一次出典であるはずの package docstring だけが
旧方針（一方向・pcbasm 不参照）を主張している。`ml.paste_volume` が
`pcbasm.pasting.dataset` / `pcbasm.geometry` を参照する現状と正面から食い違う。

## should-fix

以下 S25〜S30 は依頼の重点 A への答え。**変異 14 通りを追加で当て、11 通りが生存した。**
生存のうち意味のあるものを挙げる（driver は
`scratchpad/rev4/mut2.py` / `violate.py` / `violate2.py`）。

### S25. 許可リストの package 単位化が収集側の module を通す

確信度：**高**（実測）。深刻度：中。

`DOMAIN_DATA_PACKAGES` に `pcbasm.pasting.dataset` を package ごと入れたので、
その配下の**収集側ロジック**まで許可される。`ml.paste_volume` へ 1 行入れて実測:

```
from pcbasm.pasting.dataset.writer import PasteDatasetWriter -> 22 passed（見逃し）
from pcbasm.pasting.dataset import plan                      -> 22 passed（見逃し）
```

- `writer.py` は「Dataset session directory への lossless PNG 書き込みと atomic 確定」。
    データ構造ではなく収集側の I/O
- `plan.py` は「銅板上のセル格子・吐出量スイープ・撮影 view の計画」。HAL 非依存の純ロジック
    だが、収集手順の計画であってデータ構造ではない
- `capture.py` / `recorder.py` は装置 HAL へ届くので契約 3 が拾う（ただし M4 の形なら
    そちらもすり抜ける）

module docstring は「収集 schema と純粋な値オブジェクトは可、制御ロジックは不可」と
書いているので、記述と検査の粒度が合っていない。package 単位を維持するなら
docstring を「`pcbasm.pasting.dataset` は package ごと許可する（収集側 I/O も通る）」へ
落とすか、`metadata` / `pending` だけを module 単位で挙げるかのどちらか。

### S26. `ml` コアが `ml.paste_volume` を import できてしまう（層の向きが固定されていない）

確信度：**高**（実測）。深刻度：中。

`src/ml/_probe_tmp.py` に `from ml.paste_volume.session import PasteVolumeSession` を
入れても **22 passed**。`_is_domain_package` は先頭 segment しか見ないので `ml.*` は
素通りし、`ml.paste_volume` 自体は装置 HAL へ届かないので契約 3 も無反応。

契約 1 の狙いは「再利用価値があるのはこの層なので、強度を落とさない」。コアが
ドメイン層を 1 本引いた時点で、コアは推移的に `pcbasm` へ依存する。**contract 1 の
実効的な意味が消える**ので、「コアは `ml.paste_volume` を import しない」を
`TestCoreDomainIndependence` へ 1 本足すのが筋。

### S27. `_module_file` の package 解決（`__init__.py`）に観測点が無い

確信度：**高**（実測）。深刻度：中。

`_module_file` から `anchor / relative / "__init__.py"` の候補を落とす変異を当てても
**22 passed**。この変異で到達 module は 155 → 138 へ減り、
`pcbasm.vision.{calibration,copper,detection,overlay}` と
`pcbasm.geometry.{path,polygon,polyline,routing,sampling,transform}` が丸ごと見えなくなる。
3 巡目の S22（相対 import）で塞いだのと**同じ穴が、別の 1 行で開き直せる**。

理由は自己検査の起点の選び方（M3 と同型）:

- `test_the_scan_resolves_relative_imports` は `pcbasm/vision/__init__.py` を **file として
    直接** 起点に渡すので、`_module_file` を通らない
- `test_the_scan_counts_a_package_as_reached` は `"pcbasm.vision" in reachable` しか見ない。
    祖先展開は file が見つからなくても**名前だけ** `seen` へ入れるので、常に真

修正は 1 行で足りることを実測した。`test_the_scan_counts_a_package_as_reached` へ
`assert "pcbasm.vision.detection" in reachable` を足すと:

```
補強のみ    -> 22 passed
補強 + 変異 -> 1 failed（test_the_scan_counts_a_package_as_reached）
```

### S28. 到達検査の起点集合に観測点が無い

確信度：高（実測）。深刻度：中。

`test_nothing_reaches_a_device_only_module` の `entries` を削る変異が全部生きる。

```
tests/ml を落とす        -> 22 passed
src/ml を落とす          -> 22 passed
DATASET_SCHEMA_FILE だけ -> 22 passed
```

`_python_files` が空でないことは `TestLayerPartition` が押さえているが、**その結果を
検査本体が使っていること**は誰も見ていない。起点集合を module 定数（例 `SCAN_ENTRIES`）へ
出し、`ML_SOURCE_ROOT/"paste_volume"/"session.py"` と
`ML_TEST_ROOT/"paste_volume"/"helpers.py"` が含まれることを別テストで見るのが安い。

### S29. `DEVICE_ONLY_MODULES` を空にすると赤くならない

確信度：高（実測）。深刻度：中。

`DEVICE_ONLY_MODULES = ()` にすると parametrize が空になり、pytest は既定
（`empty_parameter_set_mark = skip`）で **skip** にする。結果は
`19 passed, 1 skipped` で exit 0。契約 3 が丸ごと消えても CI は緑。

`DOMAIN_LAYER` / `DOMAIN_PACKAGES` / `DOMAIN_DATA_PACKAGES` は空にすると死ぬよう
押さえてあるので、`DEVICE_ONLY_MODULES` だけが取り残されている。
`assert DEVICE_ONLY_MODULES != ()` を足すか、`empty_parameter_set_mark = fail_at_collect`
を pytest 設定へ入れる。

### S30. `ast.walk` と `ast.Import` の扱いに観測点が無い

確信度：高（実測）。深刻度：低〜中。

- `ast.walk(...)` を `ast.parse(...).body`（module 直下のみ）へ縮める変異 -> **22 passed**
- `ast.Import`（`import X` 形）を無視する変異 -> **22 passed**

いま実際の違反を入れれば両方とも検出できる（関数内 `import pcbnew` も
`import pcbnew` も検出を確認済み）。つまり機能はあるが、それを固定する観測点が無い。
`_absolute_imports` を tmp file に対する小さな単体テストで押さえると、M4 の修正と
まとめて 1 か所に収まる。

### S31. `tests/conftest.py` が collect 契約の対象外

確信度：高（実測）。深刻度：中。

契約 3 は「学習コンテナで collect できること」を主張するが、pytest が `tests/ml` を
collect するとき必ず読む `tests/conftest.py` が起点集合に入っていない。実測すると
`tests/conftest.py` からは `pcbasm.hal` / pcbnew / picamera2 へ届く。

いま安全なのは、それらの import が**fixture 本体の中に置いてある**からで、これは
`tests/conftest.py` の docstring に書かれた手運用の約束にすぎない。module 冒頭へ
1 本上げた瞬間に `make ml-docker-check` が collect できなくなり、Pi の CI は緑のまま。

注意：`tests/conftest.py` をそのまま起点へ足すと（`ast.walk` が関数内 import も拾うので）
検査は落ちる。**module 直下の import だけ**を見る別検査が要る。

### S32. `test_the_scan_crosses_from_the_tests_into_the_sources` の 2 番目の assert が弁別しない

確信度：高（実測）。深刻度：低。

`assert "pcbasm.pasting.dataset.metadata" in _reachable_modules([entry])` は、
`test_session.py` が同時に import している `ml.paste_volume.session` 経由でも成立する。
実測で「テスト側からしか届かない `pcbasm` module」は **0 件**だった
（`helpers.py` の `pcbasm` import は `src/ml/paste_volume` 側の到達集合に含まれる）。

つまりこのテストの主張を実際に支えているのは 3 番目の
`_module_file("tests.ml.paste_volume.helpers") is not None` だけ。docstring が言う
「テスト側の起点から `src/` 側の module へたどれること」を見たいなら、tests 側からしか
届かない module を作るか、assert を 3 番目へ寄せて docstring を合わせる。

### S33. `src/ml/paste_volume/__init__.py` の 2 段落目が移動後に意味を失っている

確信度：高。深刻度：低。

```
torch を import してよいが、``pcbasm.pasting.__init__`` からは re-export しない
（``import pcbasm.pasting`` が torch を引き込まない契約を ``tests/test_package.py`` が固定）。
```

`paste_volume` はもう `pcbasm.pasting` の下に無いので、この文は自明に真で、かつ
読み手を旧配置へ誘導する。あわせて `tests/test_package.py` は `tests.helpers`
（pcbnew / picamera2）を import するため学習コンテナでは走らない、という点も
いまは書かれていない。

### S34. 要件書 `docs/image-based-dispense-calibration.md` が旧方針のまま

確信度：高。深刻度：中。

ML 実装計画は「[画像ベース吐出量キャリブレーション要件](image-based-dispense-calibration.md)
に定義された…次段階」と自ら親文書として参照しているのに、その親が新方針と正面から矛盾する。

- `:743-744` 「データセット、画像前処理、モデル、学習、評価、推論、教師体積の配分、
    `rotations_per_ul` の補正計算は `src/pcbasm/` に集約する」
- `:749-750` 「`src/pcbasm/` 内の Python module として実装する」
- `:755-762` `python -m pcbasm.cli.paste_volume ...` × 8 行（計画側は
    `ml.cli.paste_volume` へ書き替え済み）

観点 D の「方針と実装が食い違っている記述」はここに残っている。

### S35. 仕様書 §7 の `ml.cli.paste_volume` は構造契約では「コア」に分類される

確信度：中（将来分の設計。深刻度：低）。

`docs/image-based-dispense-calibration-ml-plan.md:995` が `ml.cli.paste_volume` を
置くと決めているが、`_core_files` は `DOMAIN_LAYER not in path.parts` で判定するので:

- `src/ml/cli/paste_volume.py`（file 形）: parts は `paste_volume.py` なので**コア扱い**。
    塗布ドメイン専用の CLI が「再利用価値のあるコア」に入り、契約 1 で `pcbasm` 参照を
    禁じられる
- `src/ml/cli/paste_volume/`（directory 形）: コアからも `_domain_layer_files` からも
    外れ、`test_the_two_sides_partition_the_tree` が落ちる（こちらは loud なので健全）

同じ意図の配置が形によって扱いが変わる。`ml.paste_volume.cli` に寄せるか、
`DOMAIN_LAYER` の判定を「`ml.paste_volume` 配下」に限定して仕様書側を直すか。

## nit

- `tests/ml/__init__.py` の docstring が「コア ML 基盤 (`src/ml/`) のテスト.」のまま。
    いまは `tests/ml/paste_volume/` にドメイン層のテストが同居する
- `tests/ml/helpers.py:3` 「`ml` は装置ドメインを知らない ML 基盤なので」。結論
    （`tests.helpers` に依存させない）は有効だが、前提はもう成り立たない
- `DATASET_SCHEMA_FILE` を起点へ足すのは冗長。`ml.paste_volume` が
    `pcbasm.pasting.dataset.metadata` を import しているので、`_module_file` 経由で
    必ず走査される。落としても検査結果は変わらない（実測）
- `_core_files` が**絶対 path**の `parts` を見るので、`/…/paste_volume/pcb-assembly/`
    のような場所へ checkout するとコア側が空になる。`root` からの相対で見れば済む
- 変異「`_python_files` が `__init__.py` を除く」「tests 側のコア検査の対象を空にする」
    「ドメイン層検査の parametrize から `ML_TEST_ROOT` を外す」もいずれも生存。
    どれも「assert を消せる」類なので優先度は低い
- `docs/image-based-dispense-calibration-ml-plan.md:998` の `pcbasm.pasting.paste_volume`
    は Phase 5 の予定なので残して正しいが、本 MR で `src/pcbasm/pasting/paste_volume/`
    は消えた。予約 namespace が無くなったことは §7 のどこにも書かれていない
- `tests/ml/paste_volume/__init__.py` だけ docstring が無い（`tests/ml/__init__.py` は持つ）

## 観点 B（移動の完全性）への回答

**旧 path の参照残りは無い。** `src/` `tests/` `Makefile` `AGENTS.md` `CLAUDE.md`
`docker/` `.gitlab-ci.yml` `.claude/settings.json` `.codex/rules/` を全走査して、
`pcbasm.pasting.paste_volume` / `tests/pcbasm/pasting/paste_volume` の残りは
`memory/agents/**` と仕様書 `:998`（Phase 5 の予定、意図的）だけ。
空 directory も残っていない。

**docstring の相互参照は 5 module すべて追随している**（`batch` / `dataset` / `index`
/ `session` / `task` の `:mod:` 参照を確認）。取り残しは `__init__.py` 側だけで、
M5（`src/ml/__init__.py`）と S33（`src/ml/paste_volume/__init__.py`）。

**`memory/agents/**` を更新しない判断は妥当。** 当時の事実の記録であり、
`code-reviewer` / `implementation-planner` / `plan-implementer` の過去ノートを
書き替えると「その時点で何を見て何を決めたか」が失われる。進行中の
`orchestrator/paste-volume-data-task.md` にだけ追記しているのも正しい切り分け。

## 観点 C（`docker/ml/` 移動）への回答

**見落としは見つからなかった。**

- `compose.yaml` の bind mount `../../:/workspace` は `docker/ml/` からの相対で正しい。
    `build.context: .` は compose file の directory 基準なので `Dockerfile` と同じ場所を指す
- `write-env.sh` は `SCRIPT_DIR` 基準で `.env` / `compose.credentials.yaml` を書くので、
    移動しても生成先が追随する。実際に `docker/ml/.env` と
    `docker/ml/compose.credentials.yaml` が存在し、コンテナが 4 時間稼働している
- `docker/ml/.gitignore` も一緒に移っており、生成物 2 つは引き続き無視される
- project 名 `pcb-assembly-ml` は固定なので named volume（`ml-venv` 等）は保持。
    README の「既定の project 名が `ml` になってしまう」への書き替えも正しい
- `.dockerignore` は元から無い。build context が `docker/ml/`（4 file）へ狭まったので
    むしろ改善
- `Makefile` の `DOCKER_COMPOSE` / `ml-docker-env` は両方追随済み
- `.claude/settings.json` と `.codex/rules/default.rules` の docker 許可は
    subcommand 単位で path を含まないので影響なし。`.gitlab-ci.yml` は `docker/` を
    参照しない
- README の相対リンク `../../docs/…` は正しい（`docker/ml/README.md` から）

## 観点 D（ドキュメント整合）への回答

`AGENTS.md` / `CLAUDE.md` / `src/pcbasm/pasting/README.md` / `docker/ml/README.md` /
仕様書 §7 は方針どおりに揃っている（`make test-ml` / `ml-docker-test` /
`ml-docker-check` の対象記述、型検査の範囲、リンク先まで確認）。

食い違いが残るのは 3 か所。**M5**（`src/ml/__init__.py`）、**S34**（要件書）、
**S33**（`ml/paste_volume/__init__.py`）。

## 検証結果（4 巡目）

- make format: **pass**（`pre-commit run -a`、24 hook Passed）
- make type: **pass**（`pyright src/ml tests/ml scripts/ml_smoke.py` -> 0 errors）
- make test-no-hardware: 未実行（実機側。ユーザー担当）
- `make ml-docker-check`: **pass**（1462 passed / 1 skipped / exit 0）

検証中に `src/ml/**` と `tests/ml/**` へ一時 probe file を置いたが、すべて削除済み。
`tests/ml/test_architecture.py` は sha256 照合で復元を確認した。作業ツリーは clean。

CI #323 の結果は未確認。装置ドメインの共有コード（`applicator.py` /
`dataset/metadata.py` / `dataset/pending.py`）は本ラウンドでは触れていないので、
実機側 `make test-no-hardware` の確認は 3 巡目までの残件のまま。
