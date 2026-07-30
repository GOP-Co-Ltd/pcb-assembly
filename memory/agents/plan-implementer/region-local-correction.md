# region-local-correction — plan-implementer の判断ログ

指示: orchestrator（大域アフィン → 区ごとの局所補正へ作り直し）
ブランチ: `feature/20260729/region-alignment-average`
担当: `src/` と設定テンプレート 2 本のみ（`tests/` は `spec-test-author` = `spec-local-correction`）
前ラウンド: `memory/agents/*/region-affine-correction.md`

## IF 変更通知（指示に無い追加。`spec-local-correction` へ通知済み）

**`PasteApplicator.apply()` / `deposit_at()` に `transform: Transform | None = None` を追加した。**

補正が pad ごとに変わる一方、applicator は `__init__` で受けた `_transform` を
全塗布で使い回す構造だった。到達点（「機械座標へ出る最後の 1 回だけ適用する」
「board 座標のポリゴンへ共役適用しない」）を保ったまま pad ごとに違う変換を
使う経路が他に無いため、per-call の上書きを足した。`None` は既定値
（`__init__` の `transform`）で、既存呼び出し（吐出量キャリブの 2 箇所）は無変更。
`_fill` / `_draw_polyline` へ透過的に渡すだけで塗布挙動は変わらない。

`pasting.py` 側は applicator の既定 transform を**補正なし**
（`board_transform → toolhead_offset → height_plane`）にし、塗布・初回パージは
必ず `transform=pad_transform(center)` を渡す。既定を補正付きにすると
「どの区の補正か」が曖昧な値になるため、あえて素の変換にしてある。

## 計画どおりに実施した削除

- `alignment.py`: `fit_displacement` / `DisplacementFit` / `DisplacementModel` /
  `_MIN_ANCHOR_SPREAD_MM`、および `model` / `translation` / `residuals` /
  `residual_rms` / `residual_max` / `machine_transform` を削除。`numpy` と
  `Compose` / `Matrix2d` の import も落ちた（正味 −106 行）
- `board_ops.py`: `_log_alignment` を関数ごと削除し `measure_regions` に 2 行の
  ログとしてインライン。`OrthogonalityMetrics` の import も削除
  （`posctrl.py` の board_transform 診断では引き続き使う → `orthogonality.py` は残す）
- `posctrl.py` `_corrected_entries`: 引数から `session` を落とし、戻り値を
  `list[tuple[Pad, Point2d]]` に単純化（projector は pad ごとに巡回ループ内で作る）

## 実装上の決定（軽微・自分で決めた）

- **タイブレークは `min()` の安定性に任せる**（同距離なら `results` の先頭）。
  等サイズ正方格子では区中心の最近傍は一意なので、`region.index` 最小などの
  明示規則は起こり得ないシナリオへの対処になる。docstring に 1 行だけ書いた
- **`displacement_spread` は軸ごとの母標準偏差**（n で割る）。ログ専用の指標
- `mean_displacement` / `displacement_spread` は「補正には使わない」ことを
  docstring に明記した。将来また平均へ戻すのを防ぐため
- `AlignmentRegion.board_center` は `_Candidate.board_xy` をそのまま入れる
  （区中心は採点時に board 座標で出ているので追加計算なし）

## 既定値

`region_size_px 300 → 100` / `max_passes 2 → 5` /
`converge_tolerance 0.01 → 0.005` / `min_regions 4`（据え置き）。
`data/config-templates/kurousagi.paste/machine.toml` と
`data/testing/config/machine.toml` を同期（canny / blur は機体ごとの値を維持）。
テンプレートのコメントも「アフィン補正」→「区ごとの局所補正」に書き換え、
`region_size_px` の枠計算を `100 + 2*42 = 184` に更新した。

## 追記（orchestrator 裁定）

`RegionAligner.__init__` のライブラリ既定も config の既定へ揃えた
（`max_passes 2 → 5` / `converge_tolerance_mm 0.01 → 0.005`）。当初は
「設定から常に上書きされる」として据え置いたが、同じ概念の既定が 2 箇所で
食い違っていると設定を通さない呼び出し（テスト・将来の scripts）が本番と
違う挙動になるため、揃えるのが正しいとの裁定。

## 検証

`spec-local-correction` の `tests/` 追随が入ったあとの最終状態:

- `make format` … 全 hook パス
- `make type` … 0 errors
- `make test-no-hardware` … 1752 passed
- `make test-e2e` … 51 passed
- `grep -rn '</content>' src data` … ヒットなし

## code-reviewer 差し戻し対応（should-fix 3 件 + N1）

verdict は approve（must-fix なし）。orchestrator が採用した 3 件に対応した。

### ② 変換合成を job 層から pcbasm へ（S2）

合成順（補正は `toolhead_offset` の前・`height_plane` は最後尾）はドメイン規則
なのに `webui/jobs/{pasting,posctrl}.py` が別々に持っていた。2 段に切り出した:

- `posctrl/alignment.py` の `corrected_board_transform(board_transform, alignment,
  board_point) -> Compose` … board → **カメラ**機械座標（補正は board 変換の直後）。
  board_tour が使う
- `PasteSession.pad_to_machine(board_point, *, alignment, height_plane) -> Compose`
  … 上に `toolhead_offset` と `height_plane` を積んだ board → **ノズル**機械座標。
  塗布が使う。既存の `board_to_machine` の隣に置いた

「補正は board 変換の直後」という規則は `corrected_board_transform` の 1 箇所だけに
書かれ、`pad_to_machine` はそれを再利用する（Compose のネスト）。job 側は呼ぶだけ。

### ④ `transform` 渡し忘れが黙って無補正になる（S4）

`apply()` / `deposit_at()` の `transform` を **required keyword** にし、
`_run_paste_solder` のデッド既定（`base_transform`）を削除した。これで渡し忘れは
`make type` が静的に落とす。`_fill` / `_draw_polyline` も required に揃え、
`self._transform`（`__init__` 由来）を使うのは `draw_line`（キャリブ用
プリミティブ）だけになった — docstring にその旨を書いた。
吐出量キャリブの `deposit_at`（`pasting.py:1856`）は `transform=Identity()` を
明示するよう更新（元から Identity 構築だったので挙動は不変）。

### ③ 借用補正の距離をログに出す（S3）

`BoardAlignment.borrowed_corrections(board_points, *, region_size_mm)
-> BorrowedCorrections(pad_count, borrowed_count, median_distance, max_distance)`
を追加。「自区を持つ」は最近傍区までの距離が区の半辺以下かで判定（等格子なので
厳密な包含判定は不要）。半辺を出すのに `RegionAlignmentSession.region_size_mm`
（`region_size_px / pixel_per_mm`）を公開した。塗布ジョブが 1 行ログに出す。
**新しい設定キーも自動の救済措置も入れていない**（指示どおり）。`min_regions` の
意味の変質は据え置き。

### N1（採用）

「最近傍の区中心 = 包含区」は board→pixel が**相似のときだけ厳密**。
`correction_for` の docstring と README を「実質的にそうなる」+ 実害の大きさ
（スキュー 0.06° で 1.4um、1° でも 27.9um の帯。照合ノイズ 5um 級以下）に修正。

### 差し戻し先（`tests/`）

`transform` 必須化で `tests/pcbasm/pasting/test_applicator.py` の 26 件が
`TypeError: missing 1 required keyword-only argument: 'transform'` で落ちる。
うち 25 件は `transform=` を足すだけの機械的修正だが、
`TestPerPadCorrection::test_omitting_transform_uses_the_constructor_default` は
**削除した挙動をピンしているので置き換えが必要**。他のスイートは全部 green。

## 追記 2: per-pad seam の追加（orchestrator 承認・`spec-local-correction` 提案）

should-fix ① の残り（job 層の 3 ミューテーションがすり抜ける）を、job 層を
テストするのではなく **「pad ごとに引く」を純粋関数の戻り値に出す**ことで塞いだ。

- `posctrl.corrected_pad_targets(board_transform, alignment, pads)
  -> list[tuple[Pad, Point2d]]` … 入力順を保つ。board_tour 用
- `PasteSession.pad_transforms(pads, *, alignment, height_plane)
  -> list[tuple[Pad, Compose]]` … 入力順を保つ。塗布用

**初回パージも同じ列から引く。** `pad_transforms([purge_pad, *routed_pads], ...)`
と 1 回だけ呼び、パージは先頭要素、塗布は `[len(purge_pads):]` のスライスを使う。
パージ用の変換をもう 1 箇所で組み直さないので「パージだけ補正が抜ける」変異が
書けなくなる（`initial_purge` が無ければ `purge_pads` は空リストで、スライスは
全件になる）。

`_corrected_entries` は TOP pad の抽出と `sort_by_nearest` だけの薄い関数になった。
塗布ループは `(pad, ResolvedPaste, transform)` の 3 つ組を回すだけで、pad 中心を
引数に渡す箇所が job 層から消えた。

純粋なリファクタで、塗布位置・巡回順・吐出量・パージ量はいずれも不変。
`make format` パス / `make type` 0 errors / `make test-no-hardware` 1773 passed /
`make test-e2e` 51 passed（既存テストは 1 件も赤くなっていない）。

## 追記 3: seam のテスト合流後の最終状態と、意図的に塞がない穴

`spec-local-correction` が seam のテストを追加し、job 層のすり抜け 3 件は
`corrected_pad_targets` / `pad_transforms` に対する unit で落ちるようになった。

最終検証: `make format` パス / `make type` 0 errors /
`make test-no-hardware` 1780 passed / `make test-e2e` 51 passed。

**残る 2 variant は意図的に塞がない。**

- 塗布ループが `pad_entries[0][1]` を使い回す
- パージを `session.board_to_machine`（無補正）で塗る

どちらも「列から自分の要素を取る」形を**能動的に書き換えないと起きない**。
`_run_paste_solder` は `setup_board` が実 Klipper のホーミングを要求するため
`tests/` からは走らせられず、これを unit で守るには job 層のループを
pcbasm 側へ引き上げる（＝進捗・checkpoint・abort 境界まで移す）ことになり、
テストのためだけに責務の境界を壊す。CLAUDE.md 原則 2（投機的な実装をしない）に
従い、ここは実機確認と code-reviewer の diff 読みを担保とする。

「うっかりでは起きない形」までは seam で到達した、が結論。
