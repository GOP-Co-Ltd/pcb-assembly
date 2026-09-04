# コア ML 基盤 MR3: `src/ml/model/` 実装

計画書: `memory/agents/implementation-planner/ml-core-3-model-evaluation.md`
担当範囲: `src/ml/model/{__init__,blocks,heads,loss,inspection}.py` と
`tests/ml/model/test_{blocks,heads,loss,inspection}.py`。
`src/ml/evaluation/`、`tests/ml/evaluation/`、`tests/ml/test_architecture.py` には触れていない。

## 計画外の判断ログ

1. **`ImageEncoder.padding_pixel` 読み取り専用 property を追加した。**
   計画書の公開 IF 案には無い。学習可能 padding pixel は `_padding_pixel` という
   private な `nn.Parameter` なので、計画書のテスト観点「padding pixel の grad が非ゼロ」を
   private へ触れずに検証する手段が無かった。MR4 の部分 fine-tuning が更新対象として
   選ぶ経路にもなる。setter は無い。

2. **`GaussianRegressionHead.conditioning_features` property を追加した。**
   計画書は `head.input_features` だけを前提にしているが、条件変数の次元数も
   合成側・呼び出し側から見えた方が自然で、テストからも参照する。

3. **「mean は常に正」を「常に非負」に読み替えた。**
   Softplus は数学的には正だが、pre-activation が極端に負（feature を 1e6 倍した場合など）だと
   float32 で厳密に 0 へ丸まる。計画書のテスト観点「入力を 1e6 倍しても mean が正」は
   そのままでは成立しない。epsilon を足して `softplus(raw) + eps` にする案は取らず、
   計画書どおり `mean = softplus(raw)` を保ったうえでテストを
   「通常入力では正」「極端入力では非負かつ有限」の 2 本に分けた。
   評価側は `mean <= 0` の sample を無効として除外する設計なので下流の契約と矛盾しない。
   docstring も「常に非負」と書いてある。

4. **`build_group_norm_convolution` の `kernel_size` は検証しない。**
   偶数 kernel だと `padding = kernel_size // 2` が非対称になるが、既定 3 以外を渡す
   利用者が現時点で居ないため、起こり得ない検証は足さない方針に従った。

5. **`measure_model_size` は `try` / `finally` を使う。**
   例外処理ではなく後始末（forward hook の除去と training mode の復元）のため。
   forward が失敗したときに hook が model へ残り続けるのを避ける。
   `memory/feedback_no_try_catch.md` は入力バリデーションの話なので抵触しない。

6. **`replace_invalid_pixels` は検証を持たない純関数にした。**
   shape / dtype 検査は計画書どおり `ImageEncoder.forward` 側に置いた。

## 他 implementer への IF 変更通知

計画書の公開シグネチャからの変更は無い（上記 1、2 の property 追加のみ）。
`ml.evaluation` 側が参照する型は無いため影響しない。

## 既知の制約・残課題

- `tests/ml/test_architecture.py` の `RUNTIME_MODULES` へ
  `ml.model.blocks` / `ml.model.heads` / `ml.model.inspection` / `ml.model.loss`
  を足す作業は別 agent の担当。未反映。
- `torch.compile` parity（`ml.evaluation.compile_parity`）は別 agent の担当。
- テストの過学習ケースは Adam 200 step / 4 sample / 16x16 画像で約 3 秒。
  Raspberry Pi 5 CPU 前提の規模に収めてある。

## 検証結果

- pre-commit（対象 10 ファイル、2 回連続）: pass。2 回目で無変更を確認。
  `make format`（`pre-commit run -a`）は **実行していない**。別 agent が
  `src/ml/evaluation/` を編集中で、全ファイル書き換えが競合しうるため。
  docformatter が日本語段落を壊さないよう、説明部は 1 文 1 段落かつ
  折り返し幅（72 桁 - インデント）未満に収めてある。
- `uv run pyright`（リポジトリ全体）: 0 errors, 0 warnings
- `uv run pytest tests/ml -m "not hardware" -q`: 324 passed
  （うち `tests/ml/model` は 100 件）
- `uv run pytest -q -m "not hardware and not e2e"`（= `make test-no-hardware`）:
  3082 passed / 140 deselected（114 秒）。他 agent 起因の失敗も無し。
