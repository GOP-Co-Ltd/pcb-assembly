# docs-keeper メモ: paste-align-max-failures

2026-07-10。結論: **ドキュメント変更なし**。

## 確認内容

1. **docstring 整合**: `git diff -- src/` を精査。
   - `board_ops.pad_align_abort_message` — Args / Returns が契約（None=無制限、失敗数<=許容数で None、超過でメッセージ）と一致
   - `board_ops.align_component_groups` — Args に max_failures（None は無制限＝board_tour 用）、Raises: ValueError あり。実装（append 後判定→即 raise）と一致
   - `pcbasm.config.PadAlign.max_failures` — inline コメント「照合失敗の許容部品数。超過で塗布ジョブを即中止」で十分
   - `pasting.py` :773 のコメント追記も実装と一致
   → 過不足なし、修正不要

2. **README への max_failures 追記**: 対象なし。
   - ルート README.md — machine.toml キーの記述なし
   - configs/README.md — machine.toml は最小例示スニペット（machine_type / klipper / camera）のみで、キー一覧は存在しない。paste_dispenser 系キーは一切列挙されていないため、max_failures だけ足すのは不整合・過剰整備
   - src/webui/README.md — 存在しない

3. **旧挙動（照合失敗＝無補正で続行）の誤記述**: `grep "無補正|照合に失敗|照合失敗"` を *.md 全体（memory/agents 除く）にかけてヒットなし。修正対象なし。
   なお pasting.py の「未照合 pad は無補正塗布」フォールバックは許容内失敗で引き続き有効（計画書どおり変更なし）。

4. **`</content>` 等のゴミ混入**: 変更 8 ファイル + 新規 test_board_ops.py に grep でヒットなし。
