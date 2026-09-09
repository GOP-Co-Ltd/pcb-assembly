# MR1 (paste-dataset-dot-core) の裁定記録

`refactor/2026-09-08/paste-dataset-dot-core` で spec-test-author が上げた
曖昧点 5 件に対する orchestrator の裁定。

## 1. blank セルの識別方法 — 別型を採用

計画書の指示は sentinel（`volume_index = -1` / `amount_ul = 0.0`）だったが、
実装は `DotCell` / `DotBlank` の別型 + `type DotTarget = DotCell | DotBlank` を採った。

**裁定: 実装の別型構造を採用する。**

理由: sentinel は `volume_index` の documented range（`0..volume_divisions - 1`）を壊す。
`DotBlank` は `commanded_volume_ul` も `volume_index` も `order` も持たないので、
「塗布していないものに塗布パラメータを持たせない」ことが型で表現できる。
`DotGridPlan.targets` が index 昇順で両者を束ねるため、撮影ループは union のまま回せる。
metadata 側も `samples[]` / `blanks[]` の 2 配列に自然に対応する。

## 2. 総点数の名前 — 実装を採用

計画書の `point_count` ではなく `sample_count`（量点のみ）/ `target_count`
（blank 込みの撮影対象総数）の 2 分割。撮影枚数の算出に使うのは後者で、
容量判定に使うのも後者。2 つの意味を 1 語に潰さないほうが読み違いが起きない。

## 3. `samples[].order` の定義 — plan 時採番を採用

plan がセル index 昇順で採番し、recorder は写すだけ。撮影順序を interleave 固定に
したので計画順 = 実行順で一致する。`record_execution` の呼び出し順を定義に
すると recorder が順序の真実を持つことになり、plan の決定性検証が効かなくなる。

blank は order を消費しない（塗布していないため）。

## 4. `metadata.blanks[]` のキー集合 — fixture の定義を採用

`{index, cell, center, measured_volume_ul, views}`。`execution` と
`commanded_volume_ul` を持たないことをテストで `not in` でピンしてある。

## 5. エラー文の語彙 — 実装を採用

「不正値の `repr` が理由文に載る」ことだけをテストで固定し、語彙には依存させない。
既存 `validate_dataset_run` のフィールド名ベースと、新規 validate の UI ラベルベースが
混在しているが、UI へ出るのは後者なのでラベルのほうが利用者に伝わる。
