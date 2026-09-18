# 流量キャリブレーションで dispense_accel を変更しない レビュー

## verdict: approve

計画（`memory/agents/orchestrator/flow-calib-keep-accel.md`）の 4 点削除はすべて実施済みで、
削除した公開 IF の参照は src / tests / templates / JS に残っていない。
`apply_to_machine_toml` の呼び出しは 3 箇所（rotations_per_ul / max_dispense_rate /
max_fill_speed）のみで、`dispense_accel` を書く経路は ① から消えている。
diff の全行が要求へトレースでき、無関係な変更の混入はない。

## must-fix

なし。

## should-fix

1. **実害を pin するテストがない** — `tests/web/api/jobs/pasting/test_dispense_calibration.py`
    （36 行、`parse_run_calib_command` のみ）

    - 問題: 今回の実害は「`_calibrate_rotations_per_ul` が machine.toml へ
        `paste_dispenser.dispense_accel` を書く」こと。その挙動変化を見るテストがない。
    - 追加された `TestAdopt` の G-code accel アサーション
        （`tests/pcbasm/pasting/flowcalib/test_procedure.py:259-262`）は**修正前のコードでも通る**。
        `make_applicator` → `build_applicator`（`src/pcbasm/pasting/applicator.py:90-114`）は
        `config`（= `machine.paste_dispenser`）をそのまま `PasteApplicator` へ渡し、
        `DispenseSettings.from_config`（同 145 行）が `config.dispense_accel` を採る。
        `procedure._dispense_accel` は元々 applicator に届いていないため、G-code の
        μL/sec² は修正前後で常に config 値。つまりこのアサーションは回帰検出にならない。
    - 確信度: 高 / 深刻度: 中（将来 `apply_to_machine_toml` に accel が戻っても誰も気づかない）
    - コスト: `_calibrate_rotations_per_ul` は prompt / next_command / open_camera 駆動のため
        スクリプト化した JobContext が要る。モック最小化方針との兼ね合いで must-fix にはしない。

## nit

2. `tests/pcbasm/pasting/flowcalib/test_procedure.py:244` — メソッド名
    `test_adopt_updates_values_and_rebuilds_applicator` の "values"（複数形）が残る。
    更新するのは `rotations_per_ul` だけ。クラス docstring は修正済み。確信度: 高
3. `src/pcbasm/pasting/flowcalib/flow.py:313` — `RotationsPerUlRound.evaluate` の docstring に
    「``dispense_accel`` は設定値のまま触らない」とあるが、このモジュールは dispense_accel を
    一切扱わない。再導入への注記として意図的なら残してよい。確信度: 中
4. `tests/pcbasm/pasting/flowcalib/test_procedure.py:85-99` — `_move_param` が呼び出し元
    `_dispense_accels_ul_s2` より後ろに定義されている（読み順のみ、動作に影響なし）。確信度: 高
5. `memory/agents/implementation-planner/pasting-mr4-flowcalib.md:71,72,86,89,118,125` に
    削除済み IF の記述が残る。履歴メモとして残す方針なら不要。確信度: 中

## 確認事項（指摘ではない / ユーザー判断）

- ローディング画面 `/pasting/loading` の「すべて反映」ボタン
    （`src/web/ui/static/js/loading_controls.js:150-154`）は今も `dispense_accel` を
    `rotations_per_ul` と同時に machine.toml へ書く。計画どおりの温存だが、
    「吐出加速度が勝手に変わる」を完全に断つ意図ならここも確認が要る。
- 物理的意味の変化: `dispense_accel` [μL/sec²] を固定すると、モーターの角加速度
    （= `dispense_accel × rotations_per_ul`）は rotations_per_ul の変更に比例して変わる。
    旧実装は角加速度を保存していた。ユーザー要求どおりだが、実機で加速度の体感が
    変わる可能性があるので実機確認で見てほしい。

## 検証結果

- make format: pass
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（3343 passed, 184 deselected, 134.64s）
- 成果物汚染（`</content>` 等の末尾混入）: なし
