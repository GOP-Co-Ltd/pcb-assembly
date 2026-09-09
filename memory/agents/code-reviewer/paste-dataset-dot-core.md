# paste-dataset-dot-core (MR1) レビュー

対象: `refactor/2026-09-08/paste-dataset-dot-core` の未 commit 変更（`git diff HEAD` + untracked）
仕様: `/home/gop/.claude/plans/docs-image-based-dispense-calibration-m-generic-frog.md`
（末尾「設計レビュー反映（確定・上記より優先）」節を正典として判定）

## verdict: approve

must-fix は 0 件。仕様準拠・教師体積の配分・同一ピクセル寸法の保証・削除漏れ・テスト方針は
いずれも確認できた。下記 should-fix は動作上正しいが、規約逸脱と設計の帰結の確認事項で、
MR を出す前に処理することを勧める（特に 1 は MR4 が依存する前に直すのが安い）。

## must-fix

なし。

## should-fix

### 1. `PasteSession.point_transform` の未使用引数（orchestrator 指摘 1 の裁定：妥当）

対象: `src/pcbasm/pasting/session.py:141-151`、呼び出し側 `src/web/api/jobs/pasting/dataset.py:462,483`

orchestrator の判断（引数を落として `plate_transform(*, height_plane)` へ改名）は妥当。根拠:

- AGENTS.md 開発原則 2/3（要求されていない柔軟性を追加しない・diff の各行を要求へトレース）に
    反する。`del point` はその自覚の表明であって解消ではない
- MR4 でも解消しない。MR4 節は「`point_transform` をパージの transform に使う」だけで、
    パージ点でも変換は点に依らない（`_point_chain(Identity(), height_plane)`）。点ごとの補正が
    必要になるのは「任意位置ごとに別の補正を当てる」設計が出てきたときだけで、それは MR4 の
    要求ではない
- 実害の芽: 引数を取る以上「その点に固有の変換」と読める。実際は点に依らないので、点 A で
    得た transform を点 B に使い回しても誰も気付かない。改名すれば型で誤用が防げる
- 改名すればジョブ側でループ外に 1 回だけ組める（現在は `deposit_at` ごとに
    `session.point_transform(target.center, ...)` を呼んで `Compose` を作り直している）

確信度: 高（規約逸脱の事実）／中（MR4 でも不要という将来判断）
付随作業: 計画書 MR1・MR4 節の記述と実装ノート 7 の更新。

### 2. `flowcalib/flow.py` の `sweep_schedule` re-export（orchestrator 指摘 2 の裁定：妥当）

対象: `src/pcbasm/pasting/flowcalib/flow.py:22`、`tests/pcbasm/pasting/flowcalib/test_flow.py:20,196-209`

テスト側を `pcbasm.pasting.sweep` へ向け直して re-export を落とす判断は妥当。根拠を補強:

- production 側で `flow` 経由の `sweep_schedule` 利用は 0 件（`lines.py` は `sweep` へ移設済み。
    grep 済み）。re-export はテスト 1 ファイルのためだけに存在しており、テストが production の
    公開面を縛る逆転になっている
- must-fix 2 の意図（「flowcalib と dataset の双方が sweep を import する」）は、`flow` が
    `sweep_schedule` を公開し続けている限り半分しか達成していない
- lint では検出できない。`pyproject.toml:169` で F401 を ignore しているので、この未使用 import は
    `make format` を通る。人間のレビューでしか落とせない類の負債
- 併せて skill `testing-strategy` の「1 source ファイル 1 test ファイル」に違反している。
    `sweep.py` に対応する `tests/pcbasm/pasting/test_sweep.py` が無く、`TestSweepSchedule` は
    `flowcalib/test_flow.py` に残っている。**クラスごと `tests/pcbasm/pasting/test_sweep.py` へ
    移す**のが両方の解になる（クラス docstring「②③ が共用する」も dataset を含む記述へ）

確信度: 高

### 3. 塗布（ノズル）位置の可動域が事前検証されていない

対象: `src/pcbasm/pasting/dataset/plan.py:371-393`（`validate_capture_reach`）、
`src/web/api/jobs/pasting/dataset.py:375-388`（`_check_reach`）

`board_to_stage=session.board_transform` で**撮影位置**だけを検査している。塗布位置は
`board_to_machine`（= `board_transform` + `toolhead_offset`）で、`config/machine.toml:35-36` の
toolhead は `(1.7946, 22.8349)` mm。つまり塗布目標は撮影目標から Y +22.8 mm 離れる。撮影が
可動域に入っても塗布が入る保証はなく、事前検証を通ったのに塗布の途中で Klipper が範囲外を
返して落ちる（incomplete が残る）経路がある。

must-fix 7 の文面は「全（点 × view）の**撮影目標**が stage soft limit に入るか」なので仕様としては
満たしている。実害は「途中失敗」で物理的危険はないが、装置を動かす前に落とすという設計意図
（must-fix 7 の「装置を動かす前の検証を増やす」）からは漏れている。`_check_reach` に
`board_to_stage=session.board_to_machine`（view offset なし）の 1 回目を足せば済む。

確信度: 高（未検査であること）／中（実運用で踏むか）

### 4. 「先頭 retract をしない」前提が実行時に運用者へ伝わっていない

対象: `src/web/api/jobs/pasting/dataset.py:320-344`（`_confirm_dataset_collection`）、
docstring 402-413

`fill_sequence.py` を独立に読んで検証した結果、**実装者の結論（先頭 retract を入れない）は正しい**。
`FillSequence.to_gcode` は `retract_amount + prime_extra + total_amount` を押し出し（149-154）、
塗布移動が速度 0 になった時点から `retract_amount` を引き戻す（169-177）。よって 1 シーケンスの
正味送り量は `total + prime_extra` で、開始・終了状態はどちらも「retract 済み」。自己完結している。

定量的にも確認した。gap を g、retract 量を R、指令量を V とすると 1 シーケンスの吐出量は
`R + V - g`（終了時 gap は R）。

- 期待状態（g = R）で retract しない → 吐出量 = V（誤差 0）
- 期待状態で retract を入れる（g = 2R）→ 最初のシーケンス（パージ、V = 0.2 uL）が 0.17 uL に
    痩せ、以降は正常。`retract_amount = 0.03 uL`（`config/machine.toml:22`）なのでパージが吸収する
- 手動プライム後（g = 0）で retract しない → パージが 0.23 uL 出る。以降は正常

つまり**期待状態では retract しない方が厳密に良く**、ずれた場合の影響はどちらもパージが吸収する。
結論は支持できる。

ただし、この設計は「直前のジョブが retract 済みで終わっている」という**外から見えない状態**に
依存するようになった（旧実装は先頭 retract を持っていた: `git show HEAD:src/web/api/jobs/pasting/dataset.py`
の 191-193）。手動プライムがあった場合、パージが R = 0.03 uL 過剰吐出し、その分が総質量に入って
回転数比で全 sample へ配分される（合計 ≈ 2.1 uL に対し約 1.4% の系統バイアス）。達成条件は
`|mean(e)| + std(e) <= 0.10` なので、mean 側の予算の 14% を無条件に食う。

要件書 790 行付近の達成条件には「追加の手動ローディングを行わない」と書いてあるが、**実行時の
confirm プロンプトには書かれていない**（「吐出量キャリブレーションが完了していることを確認して
ください。」だけ）。プロンプト文へ「キャリブレーション後に手動プライム／ローディングをして
いないこと」を足すのが最小の対処。

確信度: 高（力学の検証）／中（プロンプト追記の必要性）

### 5. 既定設定でサンプルが銅板の上端 2 行に固まる

対象: `src/pcbasm/pasting/dataset/plan.py:239-278`

既定（40×40、余白 2、セル 2 + 間隔 1、19 targets）で実測すると、capacity 143 のうち使うのは
`available[:19]`＝行優先の先頭 19 セルで、**y = 2.0 と 5.0 の 2 行だけ**（x は 2.0〜35.0）。
seed を変えても使うセル集合は変わらない（`test_shuffling_does_not_change_which_grid_cells_are_used`
がこの挙動を固定している）。

計画書 MR1 節の「残ったセルへ先頭から割り当てる」の通りなので**実装は仕様準拠**であり、
「位置と量の相関を切る」という目的も帯の中では達成されている。一方で

- 全 sample が板の上端 5 mm 以内、かつパージ blob と同じ帯に入る
- 板の 87% を使わない（143 セル中 19 セル）
- 照明ムラ・板の反り・端部の反射が帯単位で共通に乗る

という帯特有のバイアスは残る。`random.Random(seed).sample(available, target_count)` にすれば
位置も散り、板全体を使える（計画書の文面変更が必要なのでユーザー判断）。

確信度: 高（事実）／中（実データへの影響度）

## nit

- `PasteDatasetWriter._validate_crop`（`writer.py:208-213`）は `crop.image.shape` だけを見て
    `crop.pixel_rect` の寸法（`x1-x0 == crop_size_px`）を検査しない。`crop_centered` が両者を
    同時に作るので現状は破れないが、「永続化境界で寸法を強制する」（must-fix 4）を pixel_rect まで
    広げるなら 1 行。実際 `test_recorder.py:355` は image 9x9 / pixel_rect 8x6 の crop を通している
    （そのテストの意図は pre/post 不一致検出なので問題はない）。確信度: 中
- `metadata.config` のキー名が計画書 MR1 節の例（`crop_pixel_size`）ではなく `crop_size_px`。
    末尾レビュー節が `crop_size_px` と書いており、そちらが優先なので実装が正しい。計画書 MR1 節の
    例が古いままなので、後続 MR で混乱しないよう計画書側を直すか申し送ること。確信度: 高
- `ctx.checkpoint()` は target 単位（`dataset.py:471`）。view 5 本なら 1 target で 11 回のステージ
    移動・撮影が走るので、abort 応答は最悪 1 target ぶん遅れる。view ループ内にも置くかは好み。
- `MIN_COMMANDED_ROTATIONS = 0.1 rev` は実装者が「暫定・ユーザー確認事項」としている通り根拠の
    無い定数。実機値 `rotations_per_ul = 15.208294` では 0.0066 uL 未満でしか発火しないので、
    既定 0.05 uL は余裕で通る（実測確認済み）。ゲートとしては緩いが無害。
- `plan_views(0, float("nan"))` は count = 0 でも radius の有限性検査で弾かれる（`plan.py:305-306`）。
    view を撮らないなら radius は無関係なので、わずかに過剰。無害。
- `_generate_plate`（`dataset.py:347-356`）は `dispense_calibration._generate_calibration_board` と
    ほぼ同一。2 箇所目なので「3 度現れたら抽出」の目安では抽出しないのが正しい。記録のみ。
- 範囲外の観察: `paste_solder.py:96-97` は `interactive_loading` が False でも無条件に
    `applicator.retract()` する。本 MR で確立した論理を当てれば、ローディングを挟まない実行では
    plunger が baseline より R ぶん引き込まれる（直後の初回パージが吸収するので実害は小さい）。
    本 MR の範囲外だが、同じ論点なので記録する。

## 確認済み（指摘なし）

- **教師体積の配分**: `recorder.finalize`（`recorder.py:157-161`）の `rotations` は
    `self._executions`（sample + purge）だけから作り、blank は `record_execution` を通らないので
    分母に入らない。`allocate_volume_by_rotations` は比例配分なので
    `Σ samples + purge == total.measured_volume_ul` が恒等的に成立し、テストでも固定されている。
- **同一ピクセル寸法**: `crop_size_px` は `_plan_collection`（`dataset.py:291-293`）で 1 回だけ決まり、
    capturer / writer / `DatasetRunInfo` の 3 箇所へ同じ変数が渡る。`crop_centered` は範囲内なら
    必ず `(n, n, 3)` を返し、writer が `(n, n, 3)` uint8 を強制する。抜け道は見つからなかった。
    事前の `CalibrationResult.load` と `setup_board_calibration`（`posctrl/setup.py:223`）は同じ
    `machine.camera.calibration_file` を読むので pixel_per_mm の食い違いもない。
- **`measure_height_plane` の銅箔明示**: `_measure_plate_height`（`dataset.py:359-363`）が
    `[Copper(layer=Layer.TOP, polygon=session.pcb.outline.polygon)]` を明示（must-fix 1）。
    `generate_rect_pcb` の外形は `(0,0)-(w,h)`（`pcb/generate.py:245`）なので、
    `_plate_center_z` の `Point2d(w/2, h/2)` と `plan` の板左上原点前提も整合する。
- **interleave**: パージ → target ごとに pre 全 view → 塗布 → post 全 view。applicator の
    context は全ループを包み（撮影中もポンプ ON。paste_solder と同じ形）、progress の分母は
    `len(targets)`（blank 込み）、`record_post` の pre/post `pixel_rect` 一致検査は同じ
    `stage_xy` から計算するので常に成立する。例外時は `mark_incomplete` へ落ちる。
- **削除の完全性**: `PolygonCrop` / `crop_polygon` / `validate_crop_margins` / `DatasetTargets` /
    `plan_dataset_targets` / `resolve_dataset_initial_purge` / `DATASET_PURGE_PAD_ID` /
    `InitialPurgePurpose` / `purpose` クエリ の参照は src / tests / data / docs に 0 件。
    `data/testing/schemas/paste_dataset_metadata_v1.json` は must-fix 9 の通り残っている。
- **テスト品質**: `mocker` / `monkeypatch` / `patch` の使用 0 件、private 属性アクセス 0 件。
    `test_capture.py` は `FakeCamera` / `FakeKlipper`（自前 HAL ABC の fake）＋実 `XYZStage` /
    `PasteSession` / `CopperProjector` / 実 PCB。cv2 は実物で PNG round-trip を見ている
    （`Image` は BGR 保持なので `cv2.imwrite` の色順も正しい）。
- **WebUI の薄さ**: 新テンプレートは `job_form` / `preview_pane` / `job_console` の include のみ。
    `pages.py` の `_FEATURE_CONTEXT` / `_MACHINE_SETTINGS_FEATURES` から dataset を外し、
    JS 追加なし。`build_initial_purge` の `purpose` 分岐も消えて常に 400 を投げる形に単純化。
- **成果物汚染**: 変更・新規ファイルに `</content>` 等の混入なし。TODO/FIXME なし。
- レビュー節の must-fix は 1〜10 の 10 件（依頼文の「1〜14」は数が合わない）。10 件すべて実装を確認した。

## 検証結果

orchestrator 実行済みの `make format` / `make type` / `make test-no-hardware`
（3525 passed / 140 deselected）を前提とし、本レビューでは再実行していない
（実機テストは実行しない）。追加で行った確認:

- `plan_dot_grid(DotGridSpec())` の実測（capacity 143 / targets 19 / 使用行 y=2.0,5.0）
- `crop_centered` の窓配置の実測（幾何中心は射影点から常に 0.5 px 以内。pixel を
    `[i, i+1)` とする `CopperProjector.pixel_of` の規約と整合しており問題なし）
- `validate_min_rotations(DotGridSpec(), rotations_per_ul=15.208294)` → None
- `sweep_schedule` / 削除シンボルの参照 grep、F401 ignore の確認
