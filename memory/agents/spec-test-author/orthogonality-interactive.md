# orthogonality_test 対話フロー復帰 — テスト（生きた仕様書）

## 追加・変更したテスト

### tests/webui/jobs/test_board_ops.py

モジュール docstring に `confirm_next_point` の契約記述を追加。`TestConfirmNextPoint`
を新設（実 JobManager + 実 JobContext + 合成ジョブ経由。モックなし）:

- `test_next_returns_true_and_quit_returns_false` — 2 点分連続で呼び、戻り値列が
  `[True, False]`（「次へ」= 継続 / 「終了」= 打ち切り）
- `test_prompt_spec_pins_confirm_kind_labels_and_default` — `kind == "confirm"` /
  `default is True` / `true_label == "次へ"` / `false_label == "終了"`
- `test_prompt_message_contains_the_point_label` — message に巡回先ラベルを含む

ヘルパ `_run_confirm_job(manager, catalog, wait_until, labels, answers)` は
`record.pending_prompt` から spec を取り出してから `respond_prompt` する（応答前に
spec を確認する必要があるため conftest の `answer_next_prompt` は使えない）。

### tests/webui/jobs/test_posctrl.py

- モジュール docstring に `orthogonality_test` の追加契約（巡回先の構成、各点での
  confirm、「終了」で SUCCEEDED、周回継続）と検証分担を明記。装置なしで巡回本体が
  検証できない理由 = `setup_board` が実 Klipper のホーミング + 基準点合わせ必須で、
  test-fixture では最初の prompt に到達する前に FAILED になる。
- `TestPosctrlHardware` docstring に実機手順（各点でプロンプト → 調整して「次へ」→
  打ち切る点で「終了」= 正常終了）を追記。
- `test_machine_job_runs_to_success` の parametrize（board_tour / orthogonality_test）
  を分割。**理由**: 対話フロー化により orthogonality_test は応答なしでは終端しない
  ので、旧テストは 900s タイムアウトで必ず失敗する。
  - `test_board_tour_runs_to_success`（旧挙動のまま）
  - `test_orthogonality_test_prompts_each_point_and_quit_succeeds` — 四隅 4 点を
    「次へ」で通し 5 点目で「終了」→ `SUCCEEDED`、summary に「軸間角」「周回」

## 省略した判断

- **prompt 待機中 abort の JobAborted 伝播テストは書いていない**。
  `test_manager.py::TestPrompt::test_abort_while_waiting_prompt_is_immediate` が
  `JobContext.prompt` 一般の契約として同等の検証（ABORTED + pending_prompt クリア）
  を既に持つため、`confirm_next_point` 側で重複させない。`confirm_next_point` は
  `ctx.prompt` の戻り値を bool 化するだけで例外を扱わないので、固有の挙動はない。

## 実装側への要求

なし。検証時点で `src/webui/jobs/board_ops.py` の `confirm_next_point` は確定契約
どおり実装済み。summary の「軸間角」「周回」文言は実機テストのみが参照するので、
実装側が別語彙を使うなら実機テストの substring を合わせる必要がある（実装は
「周回」を含む前提で書いた）。

## 検証

`make format` / `make type`（0 errors）/ `make test-no-hardware`（1608 passed,
88 deselected）すべて通過。`@mark_hardware` テストは未実行（ユーザー実行）。
