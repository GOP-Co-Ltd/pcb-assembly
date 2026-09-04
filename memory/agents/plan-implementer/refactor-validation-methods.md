# 値オブジェクトの検証をクラスのメソッドへ移す（MR-A）

計画書: `memory/agents/implementation-planner/refactor-validation-methods.md`
規約: `memory/feedback_validation_method.md`

## 実装したもの

確信度「高」の 3 件のみ。orchestrator の裁定どおり `validate_custom_pad()` と
`config.py` の scalar validator 8 個は触っていない。

| 移動元 | 移動後 |
| --- | --- |
| `validate_view(view)` | `DatasetView.validate(self) -> str \| None` |
| `validate_line_layout(layout)` | `LineLayout.validate(self) -> str \| None` |
| `validate_board_config(config)` | `BoardConfig.validate(self) -> str \| None` |

後方互換 alias は作っていない。旧名は `src/` `tests/` `scripts/` から消えている。

## 計画外の判断ログ

- **テストクラス名 `TestValidateLineLayout` → `TestLineLayoutValidate` に改名。**
    計画書に記載はないが、消えた module-level 関数名をクラス名が指したままになるため。
    テストの中身と件数は変えていない。`test_metadata.py` の `TestDatasetView` と
    `test_config.py` の `TestBoardConfig` は元からクラス名なので変更なし
- **`BoardConfig.validate()` の docstring 直後の空行を残した。**
    移動元の書式をそのまま維持している
- **`memory/feedback_validation_method.md` の最終節を書き直した。**
    「この規約のためだけの一括改名はしない」が本 MR と矛盾して読めるため、
    移行済みモジュール / 機会に合わせて移す / scalar validator は据え置き の 3 点に整理した
- **`memory/MEMORY.md` のフィードバック索引行**（規約ファイルへのリンク）も同じコミットに含めた

## 計画どおりに実施した振る舞い変更

`BoardConfig.validate()` から外側の `isinstance(config, BoardConfig)` ガードを削除した
（メソッドでは到達不能なため）。フィールドの isinstance チェック
（`board` / `purge_pad` / `custom_pads` / `patterns` / 各要素）は残している。
`test_invalid_runtime_values_do_not_leak_exceptions` が引き続き緑であることを確認済み。

`git diff -w` を取ると `config.py` の実質差分は
「ガード 1 個の削除」「`config.` → `self.`」「呼び出し 2 箇所」だけになる。

## 追随させた呼び出し元

- `src/pcbasm/pasting/dataset/recorder.py:30`（import 削除）、`:91` → `view.validate()`
- `src/pcbasm/pasting/flowcalib/lines.py:33`（docstring `:func:` → `:meth:`）、`:152`、`:223`
- `src/web/api/jobs/pasting/dispense_calibration.py:27`（import 削除）、`:227`、`:378`
- `src/pcbasm/pasting/flowcalib/board/config.py:341`、`:530`
- `tests/pcbasm/pasting/dataset/test_metadata.py:17`（import 削除）、`:40`、`:44`、`:59`
- `tests/pcbasm/pasting/flowcalib/test_lines.py:13`（import 削除）、`:119`、`:122`
- `tests/pcbasm/pasting/flowcalib/board/test_config.py:17`（import 削除）、
    `:96`、`:133`、`:206`、`:222`、`:281`

`scripts/` は 0 件（計画書の想定どおり）。

## 他 implementer への IF 変更通知

なし（並列実装なし）。

## 既知の制約・残課題

- `BoardConfig.validate()` が同一モジュールの module-level `validate_custom_pad()` を
    呼ぶ形が残っている。union 型（`CustomPadDraft | CustomPadSpec`）の分解は設計変更なので別扱い
- `_move_to` の重複解消（MR-B）は 1 MR 1 関心事のため同梱していない

## 検証結果

各コミットの前に実施。すべて pass。

- `make format`（2 回連続、2 回目で無変更）: pass
- `make type`（pyright エラー・警告 0 件）: pass
- `make test-no-hardware`（2758 passed, 140 deselected）: pass
