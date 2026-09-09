# paste-dataset-dot-core (MR1) code-reviewer should-fix 5 件の適用

対象ブランチ: `refactor/2026-09-08/paste-dataset-dot-core`（未 commit）
裁定: `/home/gop/.claude/plans/docs-image-based-dispense-calibration-m-generic-frog.md`
末尾「code-reviewer の should-fix 裁定（MR1・確定）」

## 1. 使用セルを `rng.sample` で板全体へ散らす

`src/pcbasm/pasting/dataset/plan.py` の `plan_dot_grid`。

`available[: spec.target_count]`（行優先で先頭から詰める）を、
`random.Random(seed)` から `sample(range(capacity), target_count)` → `sorted` へ変更した。
`sample` と量・blank の `shuffle` は同じ `rng` インスタンスを共有するので、同 seed なら
使用セルと割り当ての両方が再現する。昇順へ並べ直すため index の行優先採番と
`order` の意味は変わらない。

テストの契約更新（`tests/pcbasm/pasting/dataset/test_plan.py`）:

- 追加 `test_used_cells_are_spread_over_the_whole_plate`（seed 3 種を parametrize）。
    既定 40x40（格子 12x12 / capacity 143 / target 19）で使用行・使用列がそれぞれ 6 種以上、
    最大行 index が 6 以上であることをピン。旧実装は 2 行（y = 2.0, 5.0）に固まっていた
- 旧 `test_shuffling_does_not_change_which_grid_cells_are_used` を
    `test_seed_also_chooses_which_grid_cells_are_used`（seed で使用セルが変わる）へ反転
- `test_same_seed_reproduces_the_same_placement` の比較タプルへ `cell.rect` を追加

## 2. 塗布位置の可動域検証

`plan.py` に `validate_dispense_reach(plan, *, board_to_machine, x_limits, y_limits)` を追加。
パージ点と全塗布セル（blank は塗布しないので除外）のノズル目標を検査する。既存の
`validate_capture_reach` とは検査対象（view の有無・blank の有無・変換）が違うので別関数にし、
可動域判定の共通部分だけを private `_reach_error(...)` へ寄せた。

`src/web/api/jobs/pasting/dataset.py` の `_check_reach` は撮影（`session.board_transform`）と
塗布（`session.board_to_machine`）の 2 本を回して最初の理由文で `ValueError`。

テスト追加 `TestValidateDispenseReach`: 可動域内は None / toolhead offset ぶんずれた塗布点だけが
外れる場合を検出（撮影側は通る）/ パージ点も検査対象 / blank は塗布目標に含まれない。

## 3. 先頭 retract を入れない前提を confirm へ明記

`_confirm_dataset_collection` の 1 つめの confirm 文へ「その後に手動プライム・手動ローディングを
行っていないこと」と、その理由（先頭リトラクションを行わないため、手動プライムが残っていると
パージが過剰吐出し回転数比配分で全 sample へ系統バイアスが乗る）を追記。prompt は増やさず
既存の 2 つを維持した。`_run_paste_dataset_collection` の docstring にも「この前提は装置の外から
観測できないので confirm で確認させる」を 1 段落追加。

## 4. `point_transform` → `plate_transform`

`src/pcbasm/pasting/session.py`:
`point_transform(point, *, height_plane)` → `plate_transform(*, height_plane)`。
未使用引数と `del point` を削除し、docstring を「対象点に依らず 1 回組めば板上のどの点にも
使える」へ書き替えた。

ジョブ側はループ外で 1 回だけ組む形に変更（パージと各セルの `deposit_at` が同じ
`transform` を共有）。`Compose` の作り直しが 1 + targets 回から 1 回になる。

## 5. `sweep_schedule` の間接 import を落とす

- `src/pcbasm/pasting/flowcalib/flow.py` から re-export 用の import とその説明コメントを削除
    （production 側の `flow` 経由利用は 0 件）
- `TestSweepSchedule` をクラスごと `tests/pcbasm/pasting/test_sweep.py`（新規）へ移し、
    `pcbasm.pasting.sweep` から直接 import。クラス docstring の「②③ が共用する」を
    「流量キャリブレーションの掃引と dataset の吐出量列が共用する」へ
- `tests/pcbasm/pasting/flowcalib/test_flow.py` の import から `sweep_schedule` を削除

## ドキュメント同期

- `docs/image-based-dispense-calibration.md`: セル抽出（無作為抽出 + 板全体へ散らす理由）、
    事前検証への塗布位置の追加、収集手順の「手動プライム・手動ローディングをしない前提を
    確認プロンプトで確認させる」、手順 2 と WebUI 責務 2 の文面
- `src/pcbasm/pasting/README.md`: `session.py` 行へ「銅板の一括変換」を追記
- `plan.py`: モジュール docstring（塗布も可動域検証の対象）、`DotGridSpec.shuffle_seed` の
    説明、`plan_dot_grid` の docstring（抽出 → 昇順 → 割り当ての手順と理由）
- `dataset.py`: `shuffle_seed` ParamSpec の help、`_check_reach` docstring、実行順序 docstring
- 計画書（`~/.claude/plans/...`）MR1・MR4 節の `point_transform` 表記を `plate_transform` へ
    更新（MR4 が旧名を前提にしないため）

## 公開 IF の変更

- `PasteSession.point_transform` → `PasteSession.plate_transform`（`point` 引数を廃止）
- `pcbasm.pasting.dataset.plan.validate_dispense_reach` 追加
- `pcbasm.pasting.flowcalib.flow.sweep_schedule` の re-export を廃止（正典は
    `pcbasm.pasting.sweep.sweep_schedule`）
- `plan_dot_grid` の戻り値の内容が変わる（使用セルが seed 依存になる）。シグネチャは不変

## 検証

`make format && make type && make test-no-hardware` → 3532 passed / 140 deselected。
実機テスト（`make test` / `pytest -m hardware`）は未実行。
