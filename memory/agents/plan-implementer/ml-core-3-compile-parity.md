# コア ML 基盤 MR3 第 2 波: compile parity と architecture 契約

対象: `src/ml/evaluation/compile_parity.py`、`tests/ml/evaluation/test_compile_parity.py`、
`tests/helpers.py`（Inductor probe）、`tests/ml/test_architecture.py`（`RUNTIME_MODULES`）。

計画書は `memory/agents/implementation-planner/ml-core-3-model-evaluation.md`。

## 計画外の判断ログ

- **deepcopy 後の「同一 weight の確認」を入れなかった。** 計画書の本文にある
    「model は 2 つ deepcopy して同一 weight を確認したうえで」のうち、確認の部分を落とした。
    `copy.deepcopy` を 2 回呼んだ直後に weight が食い違う経路が存在せず、AGENTS.md 開発原則 2
    （起こり得ないシナリオ向けの処理を増やさない）に反する死にコードになるため。
    公開インターフェースは計画書どおり。
- **`eager_seconds` は warm-up なしの 1 pass。** compile 側は「warm-up（=
    `compile_setup_seconds`）→ `zero_grad` → 計測 pass（= `compiled_seconds`）」の 2 pass で、
    初回 compile 費用を `compiled_seconds` から切り離した。eager 側にも warm-up を入れると
    公平だが、pass が 1 回増える割に得るものが少ないので入れていない。docstring に明記した。
- **model は Tensor のタプルを返す契約とした。** 単一 Tensor を返す model を
    `(tensor,)` へ正規化する寛容化はしない（`loss` の型が `Callable[[tuple[Tensor, ...]], Tensor]`
    である以上、タプル前提が計画書の意図）。違反は `ValueError`。
    この検査は eager 実行（compile より前）で走るので、compile 失敗の理由文字列に化けない。
- **`TensorDifference.maximum_relative_difference` の定義を `|a - b| / max(|a|, |b|)` にした。**
    両方 0 の要素は 0。片方だけ 0 でも 2 以下に収まり、報告値が inf / nan にならない。
    合否判定 (`within_tolerance`) は `torch.allclose` と同じ
    `|a - b| <= absolute + relative * |a|` で別に持つ。
- **`ParityTolerance.validate()` は非有限（nan / inf）も弾く。** 計画書は「validate を持つ」
    としか書いていない。inf を許すと合否が常に真になるため。
- **`_run_pass` の `module` 引数の型は `Callable[..., object]`。**
    `torch.compile` の戻り値は stub 上 `nn.Module` ではないため（pyright が
    `reportArgumentType` で落ちた）。勾配を持つ側は別引数 `owner: nn.Module` で受ける。
- **`FLOAT32_PARITY_TOLERANCES` の値**は計画書に数値指定がないので
    output / loss = (relative 1e-4, absolute 1e-6)、gradient = (relative 1e-3, absolute 1e-6)
    とした。Inductor での実測（下記）はこの範囲に収まる。

## 他 implementer への IF 変更通知

なし（第 1 波の `src/ml/model/`、`src/ml/evaluation/{regression,slices}.py` は読むだけ）。

`tests/helpers.py` に `skip_if_no_inductor` を追加した（既存 API は変更なし）。
probe `_inductor_compile_available()` は `functools.cache` 付きで、torch を関数内 import する
（`ml-runtime` 未 install の環境でも collection が通るようにするため）。

## 既知の制約・残課題

- **Inductor テストの所要時間。** この Raspberry Pi 5 で
    `test_inductor_backend_matches_eager_within_float32_tolerances` は 27 秒（probe の初回
    compile 込み）。CI の 30 分 timeout には十分収まるが、model が大きくなると伸びる。
    超えるようなら計画書どおり `@mark_hardware` へ移す。
- **graph break 計測は入れていない**（orchestrator 裁定）。`CompileOptions.fullgraph=True` の
    テストを 1 本置いて、break がエラーになることで代替している。
- **`src/ml/model/` と `tests/ml/model/` が untracked のままだった。** `pre-commit run -a` は
    `git ls-files` 基準なので、staging しない限り format 対象に入らない。
    `--files` 指定で単独に流したところ全 hook が Passed だったため内容は変更していない。
    commit 前に `git add` すること。

## 第 1 波コードで気になった点（未修正・報告のみ）

- `ml.evaluation.regression._weighted_percentile` は `weight > 0` で絞ったあと
    `sorted_values.numel() == 1` を早期 return するが、`positive` が 1 件も無い
    （有効 sample の weight が全て 0）場合は `torch.sort` が空 tensor を返し、
    `positions` も空になって `upper == 0` 経路で `sorted_values[0]` を引き IndexError になる。
    ただし `gaussian_regression_metrics` は事前に `weight_sum <= 0` を弾くので、
    現在の呼び出し経路からは到達しない。`slices.py` から別経路で呼ばれるようになると露出する。
- `ml.model.blocks.ImageEncoder.padding_pixel` が `nn.Parameter` をそのまま返す public
    property になっている。MR4 の部分 fine-tuning 用という docstring の説明どおりだが、
    外から `.data` を書き換えられる。現時点では意図どおりと理解している。

## 検証結果

- `make format`: pass（2 回連続で実行し、2 回目は無変更）
- `make type`: pass（0 errors）
- `make test-no-hardware`: pass（3107 passed, 140 deselected, 112.72s）
- `tests/ml/evaluation/test_compile_parity.py` 単体: 25 passed
- Inductor probe: **真**（この環境では skip されず実行された）
