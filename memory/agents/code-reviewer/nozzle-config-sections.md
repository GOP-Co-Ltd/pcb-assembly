# ノズル位置設定を paste_dispenser 配下へ移す レビュー

対象: `git diff feature/2026-09-14/nozzle-clean...HEAD`（1 commit `586a5f8`）

## verdict: request-changes

## must-fix

### M1. 移行が旧セクションを黙って捨てる（out-of-order table）

- 対象: `src/web/api/config_store.py:407-427` `_migrate_legacy_nozzle_sections`
- 問題: `del doc[name]` を「入れられるか」判定の前に実行している。
    `doc["paste_dispenser"]` が `OutOfOrderTableProxy`（`[paste_dispenser]` 群が
    他のトップレベルテーブルで分断されている）だと `isinstance(parent, Table)` が
    False になり `continue` するため、旧セクションの値がどこにも残らず消える。
    ログも例外も出ない。教示済みのキャップ座標が消えれば駐機が効かずノズルが乾く。
- 根拠（再現、tomlkit 実挙動）: `[paste_dispenser]` → `[camera]` →
    `[paste_dispenser.pad_align]` → `[nozzle_cap]` の順に並んだ toml へ
    `_migrate_legacy_nozzle_sections` を適用すると、出力から `nozzle_cap` が
    完全に消える（`tomllib.loads(out)["paste_dispenser"]` に `nozzle_cap` 無し、
    トップレベルにも無し）。`type(doc["paste_dispenser"])` は
    `tomlkit.container.OutOfOrderTableProxy`。
- 同根の派生: 書き込みキーが 1 階層（`nozzle_clean.*`）から 2 階層
    （`paste_dispenser.nozzle_clean.*`）になったため、同じ toml で
    `write_machine_settings` の `assert isinstance(child, Table)`
    （`config_store.py:395`）が AssertionError → 500 になる経路と、
    `_lookup_toml` が `paste_dispenser.*` を全て None と読む経路に、
    ノズル設定が新たに晒される。
- 到達性: 現在の `config/machine.toml` と `data/config-templates/*` は
    paste_dispenser が連続しているので即座には踏まない。手編集された
    machine.toml でのみ起きる。
- 確信度: 高（挙動は実験で確認済み。分岐の形からして意図的な仕様ではない）

### M2. `legacy_nozzle_sections` の記述が実装と逆で、production から未使用

- 対象: `src/pcbasm/config.py:637`（`LEGACY_NOZZLE_SECTIONS` のコメント
    「（読まない）」）、`src/pcbasm/config.py:784-791`（docstring
    「旧セクションは読まないので、残ったまま気づかないとキャップ駐機が黙って
    効かなくなりノズルが乾く」）
- 問題: `_nozzle_section`（`config.py:776-781`）は旧トップレベルを**読む**。
    テスト `test_reads_the_legacy_top_level_section` がそれをピンしている。
    安全性に関わる振る舞いについて、公開プロパティの docstring が逆を述べている。
- 併せて: `legacy_nozzle_sections` の production 呼び出し元が無い
    （grep で `tests/pcbasm/test_config.py` のみ）。計画書の
    「旧キー警告 → `/api/state` → ページ表示」行が自動移行への切り替えで落ちた
    残骸。AGENTS.md「diff の各行をユーザー要求へ直接トレースできる状態にする」
    に反する。
- 併せて: docstring「宣言順に返す」も誤り。返るのは `LEGACY_NOZZLE_SECTIONS`
    の定数順。`test_reports_both_in_declaration_order` は両方の順序で同じ結果に
    なるため、名前が主張する内容を検査していない。
- 確信度: 高

## should-fix

- S1. `src/web/ui/templates/pasting/nozzle_cap.html:20,48` の手順文が
    「machine.toml の [nozzle_cap] / [nozzle_clean] に保存される」のまま。
    保存先は `[paste_dispenser.*]`。利用者向け文言の移行漏れ。確信度: 高
- S2. 未移行ファイルで `/settings` と `/pasting/nozzle_cap` の表示が食い違う。
    `read_machine_settings` は `_lookup_toml(doc, "paste_dispenser.nozzle_cap.x")`
    で None、`_resolved_value`（`routers/common.py:262`）は
    `machine.paste_dispenser.nozzle_cap`（旧フォールバック無し）を辿って None。
    一方ノズル位置ページは `Machine.nozzle_cap` 経由で旧値を表示する。
    初回書き込みで自己修復するが、それまで「未設定」と「記録済み」が並ぶ。確信度: 高
- S3. 「structure が例外を投げる」前提の docstring が各所に残る。実際は
    `_structure_or_none` により None が返り、例外は出ない:
    `src/web/api/state.py:143-165`、`tests/web/api/conftest.py:84-86,96-98`、
    `tests/web/ui/test_pages.py:132-135,144-147`、
    `tests/web/api/routers/test_machine_control.py:102-106`
    （`ClassValidationError` は ExceptionGroup 派生…の説明）。確信度: 高
- S4. `_structure_or_none`（`config.py:650-655`）の警告に例外が載らない。
    「読めない」だけで理由（どのキーが欠けたか）が分からず切り分けできない。
    加えて `AppState.machine()` は毎回ロードし、`/api/state` 構築のたびに
    両プロパティを読むので、不完全設定のまま放置すると同じ警告が出続ける。
    また broad except は「不完全」だけでなく「不正値」（`wipe_speed` 負、
    `passes=-1`）も飲み込み、ジョブコンソールには「未記録」として出る。
    確信度: 高（挙動）／中（対処の要否）
- S5. 移行でコメントが孤立する。旧セクション直前の独立コメント行は
    `del doc[name]` 後もその位置に残り、次のセクションの見出しコメントになる。
    実験: `# タスク終了時の駐機先。…` + `[nozzle_cap]` → 移行後そのコメントは
    `[audio]` の直上に残る。同梱テンプレートは `[nozzle_cap]` 直上にコメントを
    持たないので踏まないが、手で注釈した設定では誤読を招く。
    なお旧テンプレート由来の `# [nozzle_clean]` コメントブロックは純コメントなので
    移行対象外で、既存配備ファイルには旧パスの例が残る。確信度: 高
- S6. 移行テストの欠け: out-of-order（M1）、`[paste_dispenser]` が無いファイル、
    コメント保存。`test_write_keeps_other_lines_when_there_is_nothing_to_migrate`
    は行数しか比べていない（並び替えや内容変化を通す）。確信度: 高
- S7. `MACHINE_FIELDS` 内でノズル 2 セクションが `reference_point.offsets` と
    `camera` の間に残っている。`grouped_fields` は `itertools.groupby`（連続）なので
    `/settings` では「ペーストディスペンサー / ノズル…」が他の
    ペーストディスペンサー節から離れて表示される。toml テンプレートでは
    paste_dispenser 群の直後へ動かしたので、揃えるのが自然。確信度: 中

## nit

- N1. `_nozzle_section` の `self._data.get("paste_dispenser", {}).get(name)` は
    `paste_dispenser` がスカラーだと AttributeError。`_structure_or_none` の
    外なので、`park_or_present` が try の外で落ちるという回避したかった失敗形が
    理屈上は残る（入力としてはほぼあり得ない）。
- N2. `tests/pcbasm/test_config.py:666` の docstring に余分な空白
    （「移す。 どのセクションが」）。
- N3. 1 つの契約（読みフォールバック / 書き込み移行）が
    `tests/pcbasm/test_config.py` と `tests/web/api/test_config_store.py` に
    分かれている。相互参照が docstring にあると追いやすい。

## 良かった点

- 「新が旧に勝つ」が読み（`_nozzle_section`）と書き（`name in parent` で旧を捨てる）で
    一貫している。冪等性もテストで固定されている
- 不完全サブテーブルで `PasteDispenser` 全体を落とさない対処と、その根拠
    （座標に既定値を与えない理由）がコード・テスト双方に残っている
- `NozzleCap` / `NozzleClean` の移動は中身無変更で、diff が読める

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（3208 passed, 127 deselected）
- 実機テストは未実行（方針どおり）
