# 直行性テスト対話巡回フロー レビュー指摘の適用（code-simplifier）

対象: `fix/20260727/orthogonality-interactive` の未コミット変更
入力: `memory/agents/code-reviewer/orthogonality-interactive.md`（採用分のみ）

## 適用内容

### S1 nearest 順の座標系不整合（`src/webui/jobs/posctrl.py:_orthogonality_points`）

`sort_by_nearest` の比較を machine 座標同士へ統一。並べ替え対象を `p.center`
（board 座標の Point2d）のままにし、`key=lambda center: board_transform.apply(center).to3d()`
で machine 座標に射影して `stage.get_position()` と比較する。`_corrected_entries`
と同じ形。巡回先の集合・ラベル形式・点数は不変（返す座標は board 座標のままで、
呼び出し側の `board_transform.apply` は変えていない）。

### S2 点列構築を周回ループの外へ

`points = _orthogonality_points(result)` を `while` の前に移動。`Grid k/n` と
pad の対応が周回間で固定され、周ごとの `stage.get_position()` 往復も消える。
docstring の「周回ごとに呼び直す」→「machine 座標で比較」に修正し、
`_run_orthogonality_test` の docstring に点列固定を明記。

### S4 指標が「調整前」であることの明記

再計測はしない（指示どおり）。`ctx.log("調整前の直行性指標:")` を指標 2 行の前に
追加し、summary を「調整前の軸間角ずれ … / 調整前 scale X … Y …」に変更。
docstring にも「指標は開始時の 1 回計測なので、巡回中のテンション調整は反映
されない」と記載。

### S5 テストヘルパの重複解消（`tests/webui/jobs/test_board_ops.py`）

`catalog.register(JobDefinition(...))` を `conftest.register_synthetic` 呼び出しへ
置換。`JobDefinition` の import を削除（`JobCatalog` は fixture 型注釈で使うため
残す）。prompt spec 取得ループは `record.pending_prompt` から spec を読む必要が
あるためローカルに残置（`answer_next_prompt` は spec を返さない）。

### nit

- `board_ops.py` モジュール docstring に「巡回プロンプト」を追加
- `_run_confirm_job(answers: list[Any])` → `list[bool]`、`typing.Any` import 削除
- `test_posctrl.py` の docstring 内の全角空白由来の折り返しを修正
- ほぼ恒真な `assert len(answered) == 5` を削除し、summary の実質検証
  （`"巡回 5 点"` / `"1 周目で終了"`）へ置換
- `test_posctrl.py` のモジュール docstring / hardware クラス docstring を S1・S2・S4
  の実態へ同期
- 見送り: `ctx.clear_frame()` 追加（意図した挙動）、progress percent が 100 に
  到達しない点（`_run_board_tour` と同形で許容）、`job_console.js:appendLog` の
  DOM 単調増加（pre-existing・JS は触らない方針）

## 付随して見つけた欠陥（修正済み）

hardware テスト `test_orthogonality_test_prompts_each_point_and_quit_succeeds` の
`assert "周回" in result.summary` は summary が「巡回 N 点（M 周目で終了）」で
「周回」を含まないため、実機実行で必ず失敗する状態だった。上記の
`"巡回 5 点"` / `"1 周目で終了"` 検証に置換して解消。

## 検証

- `make format`: pass（docformatter が docstring 折返しを 1 回整形。再実行で全 hook Passed）
- `make type`: pass（pyright 0 errors）
- `make test-no-hardware`: pass（1608 passed / 88 deselected / 53.20s）
- 実機テスト（`-m hardware`）: 未実行（ユーザー実行）
- README / docs に「直行性」への言及はなく、同期対象なし

## 公開 IF

変更なし（`confirm_next_point(ctx, label) -> bool`、`orthogonality_test` の params
とも不変）。JS / CSS / template / router は未変更。
