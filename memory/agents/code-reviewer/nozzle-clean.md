# ノズルクリーニング（feature/2026-09-14/nozzle-clean）レビュー

対象: `git diff main...HEAD`（5 commit / 25 files）
計画書: `~/.claude/plans/claude-n-shimmying-hopcroft.md`

## verdict: request-changes

must-fix は 1 件（M1）。それ以外は approve 可能な水準。

## must-fix

### M1. 座標欠落 `[nozzle_clean]` で塗布ジョブが cattrs 例外で落ちる（UI は「未記録」と表示）

- 対象: `src/web/api/jobs/pasting/paste_solder.py:63`（`nozzle_clean = result.machine.nozzle_clean`）
- 問題: `Machine.nozzle_clean` は座標が欠けたテーブルで
    `cattrs.errors.ClassValidationError`（`While structuring NozzleClean (3 sub-exceptions)`）
    を送出する。`AppState.nozzle_clean()` は broad except で None へ潰すので
    `/api/state` とページは「未記録」と表示するが、ジョブは `setup_board`
    （ホーミング + 基板計測）完了後に FAILED する。
- 再現: `/pasting/nozzle_cap` の「ノズルクリーニング — 動作設定」で押し込み量だけ入力
    → `[nozzle_clean] press_depth = 0.4` だけの machine.toml
    （`tests/web/api/test_config_store.py` の往復 parametrize と
    `tests/web/ui/test_pages.py::test_partially_recorded_clean_shows_placeholder`
    がこの状態を正常系として固定している）→ はんだ塗布ジョブ → 例外。
- 根拠: この状態を作るのは本 MR が追加した UI 自身で、同じ UI が「未記録」と表示する。
    `memory/agents/orchestrator/nozzle-clean.md` の「実装中の気付き」も同結論（diff 未反映）。
- 確信度: 高

## should-fix

- **S1** `nozzle_clean.py:120` `validate_reach` のメッセージが `Point3d` repr の羅列。
    `stage.move` は `制限外の移動先です: x=999.0` と軸名を返す。計画書も「違反軸と値を
    含む日本語 1 行」。passes=2 で最大 16 点が 1 行に並ぶ。テストも `"999" in error`
    しか見ておらず契約が固定されていない。確信度: 高（挙動）/ 中（重要度）
- **S2** パージが面 Z（先端がシリコンに触れる高さ）で行われる。`approach_gcode` の最後が
    `G1 Z clean.z`、その直後に `applicator.load(purge_ul)`。教示手順は「触れるだけ当てる」
    なので押し出した 0.2 uL に逃げ場がない。確定仕様は「クリーニング位置で少量パージ」
    までで Z は決めていない。確信度: 低（実機判定が必要）
- **S3** `templates/pasting/nozzle_cap.html` の動作設定フォームが `field.value` のみを描き、
    既定値で動いている項目が空欄「未設定」になる。`TestUnsetMachineSettingsShowResolvedValues`
    （MR4）が「未記載キーは backend の `resolved` で描く」を規約として固定し、同種の
    `pad_refinement_fields` は `resolved` へフォールバックしている。位置だけ記録した状態
    （最頻）で全欄が空になる一方、記録位置ラベルには「押し込み 0.50 mm」と出て食い違う。
    確信度: 中
- **S4** 同一ページで `press_depth` を即保存しても `#ncl-current` のラベルが更新されない。
    `job_console.js` の `state_changed` は `refreshHeader()`（PCB チップのみ）。AGENTS.md
    「編集後はサーバー応答または再取得で更新する」に反する。確信度: 中
- **S5** `clean_nozzle` の前提（applicator 有効化済み）をどのテストも固定していない。
    `test_nozzle_clean.py` の `applicator` fixture も
    `test_pasting.py::test_nozzle_clean_motion_returns_to_travel_z` も `with` に入らない。
    実機テストは `purge_ul=0` だから通っているだけ。確信度: 高（事実）/ 中（重要度）
- **S6** `wipe_gcode` の `min(wipe_speed, max_velocity)` clamp を固定するテストがない
    （travel 側だけ `test_caps_travel_speed_at_stage_max_velocity` がある）。確信度: 高
- **S7** ジョブ組込み（commit 3）に実機不要テストが 0 件。「未記録ならスキップしてログ 1 行」
    「クリーニングは retract より前」は計画書が明示した契約だが誰も見ていない。M1 の見落とし
    と同根。確信度: 中
- **S8** `routers/nozzle_cap.py` `record_nozzle_clean` の読み戻し失敗時 500 が
    cap 側のステータス契約（200/400/409/423/502）から外れ、書き込み成功後に
    `publish_state_changed()` がスキップされる。確信度: 高（経路）/ 低（頻度）

## nit

- N1 `tests/pcbasm/pasting/test_nozzle_clean.py:30` `import attrs` 未使用（ruff は F401 ignore）
- N2 `tests/web/api/routers/test_app_state.py:25` `from pytest import approx` はリポジトリ唯一
- N3 `depart_gcode(stage, clean)` の `clean` 未使用
- N4 `test_logs_one_line_with_purge_amount_and_passes` が passes を検証していない
- N5 `test_never_retracts` の `all(...)` は MOVE 0 件でも通る
- N6 `test_two_passes_do_not_repeat_the_center` の `points[6] == points[0]` は自明に成立
- N7 `pasting/README.md` の新行が `clean_position_label` を落としている
- N8 `models.py` の「既定は fail-closed」コメントが `nozzle_clean` には当てはまらない
- N9 テンプレの `min="0" step="any"` が `wipe_speed`（0 不可）/ `passes`（整数）とずれる
- N10 `config_store._coerce` の int 分岐は bool を弾かず `passes = true` を書ける（既存の穴）
- N11 計画書の E2E 2 件（502 トースト / `/settings` の `passes` 保存）が未実装

## 誤認していない点（確認済み）

- `clean_nozzle` が retract しないこと、`validate_reach` を送信前に必ず通すこと、
    `NozzleClean.x/y/z` が既定値を持たないこと、`depart_gcode` が必ず Z0 へ戻すこと、
    `press_depth` の上限を設定層で検証しないことは、いずれも意図どおりで問題なし
- `validate_reach` の検証漏れは無い。`approach_gcode` / `wipe_gcode` / `depart_gcode` が
    送る全セグメントの軸と feed が `approach` 3 点 + `wipe_points` 全点でカバーされている
- `wipe_points` の縮退（`stroke<=0` / `passes<=0`）と中心非重複は正しい
- `_record_current_position` の括り出しでキャップ側の挙動（ステータス、`machine_lock` が
    `klipper_errors_to_502` の外側、`publish_state_changed()` がロック解放後）は不変
- 例外経路でノズルが押し込まれたまま残っても `PasteSession.__exit__` →
    `park_or_present` → `move_to_cap` が先に Z0 へ上げる
- 要求からトレースできない「ついで改善」の混入は見当たらない
- 新規ファイルに `</content>` 等の混入なし

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（3186 passed, 127 deselected）
- make test-e2e: 未実行（ユーザー報告に依拠）
- 実機テスト: 未実行（規約どおり）
