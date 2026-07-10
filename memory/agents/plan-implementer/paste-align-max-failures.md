# plan-implementer 判断ログ: paste-align-max-failures

計画書: memory/agents/implementation-planner/paste-align-max-failures.md
公開 IF は計画書のシグネチャどおり実装。IF 変更なし。

## 実装内容（計画どおり）

1. src/pcbasm/config.py — `PadAlign` 末尾に `max_failures: int = 0` 追加、
   `__attrs_post_init__` 新設（bool・負値を ValueError で拒否）
2. src/webui/config_store.py — MACHINE_FIELDS pad_align ブロック末尾
   （blur_ksize 直後）に FieldSpec 追加。`_coerce` int case に
   max_failures 限定の負値拒否を追加
3. src/webui/jobs/board_ops.py — `pad_align_abort_message` 新設。
   `align_component_groups` に kw-only `max_failures: int | None = None` 追加。
   失敗分岐は log → on_failure → append → 判定 → 超過なら raise ValueError。
   docstring に Args / Raises 追記
4. src/webui/jobs/pasting.py — align_component_groups 呼び出しに
   `max_failures=session.machine.paste_dispenser.pad_align.max_failures` を渡す。
   照合コメントに中止条件 1 行追記

## 計画からの軽微な逸脱（構造のみ、挙動は計画どおり）

- `_coerce` の int case: 計画は float case（initial_purge_ul）と同型の
  ガード挿入だが、int case は「int そのまま」「float→int 変換」の 2 分岐で
  return するため、ガードを 2 回書かずに済むよう `coerced_int: int | None`
  に集約してから max_failures 限定チェック → return とした。
  他の int キー（camera.device_id / probe.min_samples / blur_ksize 等）の
  挙動は不変（負値もこれまでどおり受理）。

## 検証結果（2026-07-10）

- make format: pass（初回に ruff-format / docformatter が自動整形、再実行で全 pass）
- make type: 0 errors
- make test-no-hardware: 1524 passed, 77 deselected
  （spec-test-author の新テスト tests/webui/jobs/test_board_ops.py ほか
  4 ファイルの変更分を含む。全 pass）
- </content> 等のゴミ混入: grep で無しを確認
- make test / make test-e2e は未実行（合流後に親が実施）
- コミットなし
