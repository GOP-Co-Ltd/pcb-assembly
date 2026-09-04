# コア ML 基盤 MR3: 簡素化とドキュメント同期（2 巡目 approve 後の残件）

裁定: `memory/agents/orchestrator/ml-core-3-model-evaluation.md`「2 巡目レビューの裁定」
レビュー: `memory/agents/code-reviewer/ml-core-3-model-evaluation-round2.md`

## N2. 重み付き平均の重複を消した（should-fix）

`src/ml/evaluation/_aggregation.py` を新設し、public な `weighted_mean(values, weight)` を置いた。
`regression.py` の `_weighted_mean`（9 箇所から呼ばれていた）を削除して置換し、
`slices.py` の `_reliability_bin` は `torch.stack(...).sum(dim=1).div(...)` の
3 統計まとめ計算をやめて `weighted_mean` を 3 回呼ぶ形にした。

**pyright は 0 errors / 0 warnings で通った。** レビュアーの検証どおり、
`reportPrivateUsage` は private *記号* に対する警告であって、私有 module の public 記号は
対象外。`from ml.evaluation._aggregation import weighted_mean` は無警告。

公開 API は増えていない（`ml/evaluation/__init__.py` は re-export しない。
`_` 始まりなので `dir()` にも出ない）。

なぜ簡素化か: 同じ `sum(w*x)/sum(w)` の実装が 2 か所にあり、片方は 3 統計を
1 本の stack へ畳んだ読みにくい形だった。1 実装 1 定義になり、`slices.py` から
`torch.stack` / `.div` の細工が消えた。

## N1. docstring に前提条件を足した（コードは変えない）

`_weighted_percentile` の docstring に「重みの比が float64 の分解能内であること」
「実 weight は session 内 pad 数 × pad 内 view 数の逆数なので比は高々 1e4」を追記。
ガードは足していない（到達不能で死にコードになるため。AGENTS.md 原則 2）。

## N3. identity 比較のテストを足した（should-fix）

- `tests/ml/data/test_batch.py` `TestPadImageSamples::test_compares_by_identity`
    同一入力から 2 回作った `PaddedBatch` が `!=` であること、`set` サイズ 2 を固定
- `tests/ml/data/test_image.py` `TestPreprocessImageStack::test_compares_by_identity`
    `scale` が等しく画像が違う 2 つの `PreprocessedSample` が `!=` であることを固定。
    `scale` 一致を先に assert しているので、`field(eq=False)` の壊れた形へ戻すと必ず落ちる

## nit 4 件

1. `tests/ml/model/test_heads.py`: ほぼ恒真の `assert best_loss < first_loss` を削除。
    history が誤差だけになったので `list[tuple[float, float]]` → `list[float]` に単純化
2. `tests/ml/evaluation/test_compile_parity.py:362`: 「実測 86 倍」を、単独 process では
    2,000 倍超・dynamo が温まった process では 26〜70 倍と条件を書き分け、
    テストは桁ではなく大小関係だけを見る旨に改めた
3. 同 file: `_DriftingModel` / `_FirstCallParameterModel` を使う 2 テストに、
    期待値が「compiled_seconds は warm-up 後の 2 回目の pass」という
    `CompileParityResult` の契約に依存する旨を 1 行ずつ追記
4. `src/ml/evaluation/slices.py`: `DiagnosticSlice.reason` の `"machine=beta: "` prefix を削除。
    `dimension` / `value` と重複していたため。`DiagnosticSlice` の docstring に
    「reason は理由だけを持ち、どの slice かは繰り返さない」を明記。
    テスト `test_names_the_slice_in_the_reason` は
    `test_identifies_the_slice_by_its_structured_fields_not_the_reason` に置き換え

## 公開 IF の不変性

- `__all__` を実行時にダンプして 4 module とも変化なしを確認
- `git diff src/` の追加/削除行に public な `def` / `class` / `__all__` の変化なし
    （消えたのは `_weighted_mean` のみ）
- 唯一の観測可能な変化は `DiagnosticSlice.reason` の文言（上記 nit 4、裁定済み）

## 検証

- `make format` 2 回: 全 hook Passed、2 回目で tree 無変更
- `make type`: 0 errors, 0 warnings
- `make test-no-hardware`: **3127 passed** / 140 deselected / 116.45s（3125 から +2）
- 実機テストは不実行

## MR5 / MR6 への申し送り（追加）

`ml/evaluation/_aggregation.py` は私有 module。評価系で共有する集計を足すときはここへ。
`__init__.py` から re-export しないこと（公開 API が増える）。
