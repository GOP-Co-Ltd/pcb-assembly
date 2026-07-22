# region-pad-align ドキュメント整合

発端: code-reviewer レビュー nit 3（`memory/agents/code-reviewer/region-pad-align.md`）。
`src/pcbasm/posctrl/README.md` に旧シンボル `ComponentAlignments` / `sorted_top_component_pads` が残存。

## 修正したドキュメント

- `src/pcbasm/posctrl/README.md`: 「部品単位の銅箔照合」→「領域(ROI)単位の銅箔照合」、
  `ComponentAlignments` / `sorted_top_component_pads` → `RegionAlignments` / `sorted_top_pad_regions`
  に更新。領域サイズが crop 由来である旨を一言追加。

## 残した古い記述・理由

なし。grep で見つかった残骸は README のこの1箇所のみ（下記「調査結果」参照）。

## 調査結果（残骸なし）

- リポジトリ全体を `ComponentPads|group_pads_by_component|ComponentAlignments|sorted_top_component_pads|align_component_groups|min_roi` で grep（`.claude/worktrees/` の別タスク作業コピー・`memory/` の履歴ノートは対象外）。
  - ヒットは `tests/pcbasm/posctrl/test_render.py`（`min_roi` = PadResultRenderer 固有の別概念、対象外）と
    `src/pcbasm/posctrl/render.py`（同上概念のコード/コメント。code-simplifier 担当ファイルにつき不触）のみ。
  - README 修正後は旧シンボル名の残骸ゼロ。
- `部品単位|部品ごと|部品毎` でも repo 全体を grep。ヒットは `tests/webui/jobs/test_board_ops.py:66` の
  コメント `# 部品単位から領域単位への文言更新` のみで、これは移行を説明する正当な記述（テストファイルにつき不触）。
- `src/pcbasm/posctrl/`・`src/webui/` の docstring/ログ文言に「部品単位」等の残骸なし
  （pad.py / alignment.py は code-simplifier 並行作業中のため確認のみ・変更なし）。
- CLAUDE.md の posctrl 説明（1行、モジュール概要レベル）に部品単位言及なし、修正不要。
- `</content>` 混入チェック: `src/ tests/ configs/` 配下 grep でヒットなし（ノート内の過去言及2件のみで実害なし）。

## 後続に引き継ぐ事項

- code-simplifier が `alignment.py` / `render.py` を修正完了後、念のため両ファイルの docstring/log を
  再確認するとよい（今回は grep 上ヒットなしを確認済みだが並行編集中のスナップショット）。
- レビューの should-fix（非正方形 crop の収容制約式）はコード修正マターであり本タスク対象外。

## 検証

- `make format`: pass（README 差分以外の整形差分なし）
