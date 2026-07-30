# region-local-correction のテスト（spec-test-author）

ブランチ: `feature/20260729/region-alignment-average`。補正モデルを「大域アフィン最小二乗」から
「区ごとの局所補正」へ作り直す仕様を `tests/` のみに翻訳した。`src/` は一切編集していない
（mutation 検証の一時編集は毎回 md5 一致まで復元済み）。

## 仕様の要点（テストが固定していること）

- タイルは等サイズの正方格子なので **最近傍の区中心 = その pad を含む区**。
  「属する区を使う」と「区が無い / 失敗したら近傍」が距離最小の 1 規則で表現でき、
  実装に包含判定とフォールバックの場合分けがあってはならない
- 距離は **board 座標**で測る（機械座標の anchor ではない）
- 距離重み付き補間は却下された設計。隣接区の値が混ざったら落ちる
- 適用は per-pad で、機械座標へ出る最後の 1 回だけ

## 書いたテスト

| ファイル | 内容 |
|---|---|
| `tests/pcbasm/posctrl/test_region.py` | `TestBoardCenter` を新設（区中心が region_mm の整数倍格子に乗る / `board_transform.apply(board_center) == anchor` / 区内 5x5 サンプルで最近傍 = 包含、Identity と Rotation(25°) の両方）。既存のタイル張り・pad なし区スキップ・sharpness 閾値・外周マージンのピンは無変更 |
| `tests/pcbasm/posctrl/test_alignment.py` | `fit_displacement` / `DisplacementFit` / 残差のテスト群を**削除**。`TestBoardAlignmentCorrectionFor`（包含区の値・board 座標での距離・純並進・失敗区のフォールバック・全区の外側・同距離の決定性）、`TestBoardAlignmentLocality`（隣接区が混ざらない / 区境界 0.2mm で 0.36mm の段差 / どの区も平均を受け取らない）、`TestBoardAlignmentStatistics`（平均・母標準偏差・1 区で 0・一様場・空で ValueError）を新設。`RegionAlignmentSession` のテストは維持（`board_center` のピンを追加、解像度検証テストを既定値非依存へ） |
| `tests/pcbasm/posctrl/test_aligner.py` | `AlignmentRegion` の `board_center` 追加への追随と、`TestLibraryDefaultsMatchTheConfigDefaults`（`RegionAligner.__init__` の `max_passes` / `converge_tolerance_mm` 既定が `PadAlign()` の既定と一致すること）の追加。**反復計測のピン（二重計上防止・3 パス目の累積・早期打ち切り・increment・max_correction は累積判定）は 1 行も弱めていない** |
| `tests/pcbasm/pasting/test_applicator.py` | `TestPerPadCorrection` を新設。補正は機械座標を平行移動するだけで吐出量も経路点数も変わらない（board 座標のポリゴンへ共役適用する構造への回帰を落とす）/ 同じ applicator で pad ごとに違う補正 / transform 省略時は `__init__` 既定 / `deposit_at` も per-pad |
| `tests/webui/jobs/test_board_ops.py` | ログ契約を局所補正へ。`局所補正` / `平均変位` / `ばらつき` をピンし、撤去した `affine` / `translation` / `ppm` / `スキュー` / `残差` / `RMS=` が**出ないこと**を負のアサートで固定。`TestMeasureRegions._local_case` は区ごとに違う変位を与え、pad の補正は区中心から 1.3mm ずらした点で引く（中心ちょうどでは補間実装も通ってしまうため） |
| `tests/pcbasm/test_config.py` | 既定値を `region_size_px` 100 / `max_passes` 5 / `converge_tolerance` 0.005 / `min_regions` 4 へ |

`tests/pcbasm/posctrl/test_correction.py` は変更していない（実機検証済みの符号規約）。
`tests/webui/test_config_store.py` / `tests/webui/routers/test_settings_api.py` /
`tests/e2e/test_webui_e2e.py` は既定値をアサートしておらず（明示 PUT 値のみ）、変更不要だった。

`mocker.Mock` は自前 HAL の fake としてのみ既存箇所で使われており、新規に足したのは
手書き stub と実オブジェクト（実 projector・実 Canny・FakeCamera）。

## 検証結果

- `make format` / `make type`（0 errors）/ `make test-no-hardware`（**1780 passed**）/ `make test-e2e`（51 passed）すべて緑
- `grep -rn '</content>' src tests` なし
- 実機テスト（`make test` / `@mark_hardware`）は実行していない

## mutation testing（テストがバグを捕まえることの確認）

| 壊した箇所 | 結果 |
|---|---|
| `correction_for` を距離の逆二乗で重み付き補間に変更 | **29 件 fail**（`TestBoardAlignmentLocality` 2 件、包含区の 24 件、フォールバック / 全区外 / 同距離、`test_board_ops` の 1 件） |
| `correction_for` を `mean_displacement` 一律に変更 | **38 件 fail**（上記 + `test_no_region_receives_the_mean_of_the_field` / `test_distance_is_measured_in_board_coordinates`） |
| `correction_for` の距離を `board_center` → `anchor` に変更 | **32 件 fail**（`test_distance_is_measured_in_board_coordinates` を含む） |
| `region.py` の `board_center` を区中心 → 区内の pad 中心に変更 | `test_board_centers_lie_on_a_uniform_grid` / `test_nearest_board_center_is_the_tile_that_contains_the_point` ほか計 7 件 fail |
| `aligner.measure` の投影補正を撤去（= 二重計上） | **11 件 fail**（`test_second_pass_does_not_double_count_the_displacement` ほか、3 パス目・session の 2 パステストも） |
| `_send_fill_line` の `transform` 上書きを無視 | `TestPerPadCorrection` 3 件 fail |
| `RegionAligner.__init__` の `max_passes` 既定を 5 → 2 に戻す | `TestLibraryDefaultsMatchTheConfigDefaults` 1 件 fail |

いずれも一時編集で、確認後に `src/` を md5 一致まで復元済み（`grep -rn MUTATION src tests` は空）。

補足: 「区中心ちょうど」で引くテストは補間実装でも通ってしまう（ノードでは重みが発散して
その区の値になる）。中心から 0.8〜0.9 * 半辺 ずらした点、および区境界を 0.2mm またぐ 2 点で
弁別している。区中心だけで引くテストを後から足すと空振りする点に注意。

## 追記 2: code-reviewer 差し戻し（S1〜S4 / N1 / N2）への追随

`plan-implementer` が S2（変換合成の pcbasm 移譲）・S3（借用補正の診断）・
S4（`transform` を required keyword 化）を実装したので、`tests/` を追随させた。

| ファイル | 内容 |
|---|---|
| `tests/pcbasm/test_session.py`（**新設**） | `PasteSession.pad_to_machine` の合成順ピン。補正が board 変換の**後**（機械座標）で効くこと・board 空間への共役適用と違う結果になること・`height_plane` が**ノズル**機械 XY で評価されること・pad ごとに違う変換が返ること・引く点と適用する点が独立であること。加えて `TestPerPadPasteLoop` が実 `PasteApplicator` を通し、「引く点を隣の区へ移すと指令 XY が変位の差ぶん丸ごとずれる」を実 stage stub の指令列で確認する |
| `tests/pcbasm/posctrl/test_alignment.py` | `TestCorrectedBoardTransform`（機械座標での適用・共役適用でないこと・点ごとに違う補正・引く点と適用点の独立）と `TestBorrowedCorrections`（自区内は借用 0・半辺ちょうどは自区・母集団は借用点のみ・距離は最近傍の**成功**区まで・空入力）、`region_size_mm` を追加 |
| `tests/pcbasm/pasting/test_applicator.py` | `transform` required 化への追随（25 件に `transform=Identity()`、`TestTransformApplication` は全変換を `apply` へ）。`test_omitting_transform_uses_the_constructor_default` を **削除**し、`apply` / `deposit_at` の省略が `TypeError` で、かつ G-code を 1 本も送らないことのピンへ置換（S4 の趣旨そのもの） |
| `tests/pcbasm/posctrl/test_region.py` | N1 対応。`TestBoardCenterUnderSkew` を追加し、スキュー 1° の board 変換でも「境界から 0.3mm 以上内側なら最近傍 = 包含」が成り立つことをピン（誤る帯は実測 27.9um） |
| `tests/pcbasm/pasting/test_applicator.py` docstring | N2 対応。「共役適用に戻ると面積が変わる」は誤りなので、「補正が経路生成より後段にある」ことのピンだと書き直した |

### mutation testing（追随ぶん）

| 壊した箇所 | 結果 |
|---|---|
| `corrected_board_transform` が `board_point` を無視（全 pad 同一補正） | 5 件 fail |
| 補正を board 変換の**前**へ挿す（board 空間への共役適用） | 9 件 fail |
| `pad_to_machine` の `height_plane` を `toolhead_offset` の前へ | 1 件 fail |
| `pad_to_machine` が補正を落とす（無補正の塗布） | 4 件 fail |
| `borrowed_corrections` の境界を `>` → `>=` | 1 件 fail |
| `borrowed_corrections` の母集団に自区の点も含める | 3 件 fail |

### 追記 3: seam 追加後（すり抜けていた 3 件を塗った）

`plan-implementer` が `corrected_pad_targets`（posctrl）と
`PasteSession.pad_transforms` を追加し、per-pad のルックアップが job 層から
pcbasm へ移った。これに対して:

| 追加したテスト | 内容 |
|---|---|
| `test_alignment.py::TestCorrectedPadTargets` | 区をまたぐ 2 pad が自分の区の変位を受け取る（段差 0.36mm）/ **入力順を崩さない**（designator 辞書順と違う `R9, R1, R5` で渡し、値と designator の対応まで見る）/ 空入力 |
| `test_session.py::TestPadTransforms` | 同じく pad ごとの補正 / 入力順の保持 / **パージ前置きのスライス契約**（`[*purge, *routed]` を 1 回呼び `entries[0]` がパージ・`entries[len(purge):]` が塗布）/ 空入力 |

#### mutation testing（coordinator 指定の 3 件 + 順序契約）

| 壊した箇所 | 結果 |
|---|---|
| `pad_transforms` が全 pad に `pads[0].center` の変換を使う | **2 件 fail** |
| `corrected_pad_targets` が全 pad に `pads[0].center` の補正を使う | **1 件 fail** |
| `corrected_pad_targets` が固定点 `Point2d(0, 0)` で引く（巡回の固定点化） | **1 件 fail** |
| `pad_transforms` が入力順を崩す（`sorted(pads, key=designator)`） | **1 件 fail** |
| `corrected_pad_targets` が入力順を崩す | **1 件 fail**（初版は designator が R0..R3 で既に昇順だったため**すり抜けた**。非昇順の入力へ直して塗った） |

前ラウンドで「全緑ですり抜けた」と報告した 4 件は、これで全部落ちる:

1. 全 pad 同一補正 → `pad_transforms` / `corrected_pad_targets` の mutation で落ちる
2. `apply` から `transform=` を落とす → required keyword なので `TypeError`（`TestPerPadCorrection` の 2 件）
3. 初回パージの補正落ち → パージ用の変換を組み直す場所が job 層から消え、順序・スライス契約が `TestPadTransforms` でピンされている
4. 巡回の固定点化 → `corrected_pad_targets` の mutation で落ちる

#### なお残る（job 層で書けば書ける変異）

seam は「自然な書き方の退行」を塞ぐが、job 層を任意に書き換える変異は依然
`tests/` から検出できない。実測で確認した残存 variant:

| 壊した箇所 | 結果 |
|---|---|
| 塗布ループが `transform=pad_entries[0][1]` を使い回す | 全緑（すり抜ける） |
| パージを `transform=session.board_to_machine`（無補正）で塗る | 全緑（すり抜ける） |

`_run_paste_solder` は `setup_board` が実 Klipper のホーミングを要求するため
`tests/` から走らせられないので、ここは実機確認（`@mark_hardware` / ユーザー）と
`code-reviewer` の diff 読みが担保になる。ただし変異を入れるには「列から自分の
要素を取る」形を能動的に壊す必要があり、うっかりでは起きない形にはなった。

**この 2 件は塞がない方針で確定**（`plan-implementer` の判断・
`memory/agents/plan-implementer/region-local-correction.md` の追記 3）。unit で守るには
塗布ループ自体を pcbasm へ引き上げ、進捗 / checkpoint / abort 境界まで移すことになり、
テストのために責務の線を壊すことになるため（CLAUDE.md 原則 2）。今後このループを
触る人は、per-pad の対応が `pad_transforms` の戻り値の**順序**に乗っていることを
前提に読むこと。

### （seam 追加前の記録）残っていた穴

レビュアーの mutation 4 件のうち **2 件は今も検出できない**。実際に入れて確認した:

| job 層 mutation | 結果 |
|---|---|
| `pasting.py` の `pad_transform(pad.center)` → `pad_transform(routed_pads[0].center)` | **全緑（すり抜ける）** |
| `posctrl.py` `_corrected_entries` の `pad.center` → `Point2d(0, 0)` | **全緑（すり抜ける）** |
| 初回パージの `transform` を `session.board_to_machine` に差し替え | **全緑（すり抜ける）** |
| `apply` から `transform=` を落とす | 検出可（required keyword なので `TypeError`。ピンは `TestPerPadCorrection` の 2 件） |

`_run_paste_solder` / `_run_board_tour` は `setup_board` が実 Klipper のホーミングと
基準点合わせを要求するため、`tests/` からは走らせられない（レビュアー自身も
S1 で「実機依存で直接はテストできない」としている）。`_corrected_entries` は
module-private なので、直接テストすると `memory/feedback_no_private_test.md`
（private の直接テスト禁止）に抵触する。

**塞ぐには `src/` 側の seam が要る**（spec-test-author の担当外）。提案:

- `posctrl` に `corrected_pad_targets(board_transform, alignment, pads) -> list[tuple[Pad, Point2d]]`
  を置き、`_corrected_entries` は並べ替えだけにする
- `pasting` の塗布ループが使う `(pad, transform)` の列を
  `PasteSession.pad_transforms(pads, alignment=..., height_plane=...)` として公開する

どちらも「pad ごとに引く」ことが純粋関数の戻り値に現れるので、上記 3 件が
そのまま unit で落とせるようになる。

## 追記: ライブラリ既定と config 既定の一致

orchestrator 裁定で `RegionAligner.__init__` の `max_passes` / `converge_tolerance_mm` が
config 既定（5 / 0.005）へ揃えられた。既存の `test_aligner.py` は全て既定値を明示指定して
いたためこの変更に対して**無防備**（片方だけ戻しても緑のまま）だったので、2 つの既定が
一致することを `TestLibraryDefaultsMatchTheConfigDefaults` で契約として固定した
（`inspect.signature` + `PadAlign()`、`@pytest.mark.api_contract`）。値そのものではなく
「2 箇所が食い違わないこと」を見る。

## 実装側への申し送り

なし（`plan-implementer` の回答どおりに実装され、赤は残っていない）。今後 IF を変える場合は
上記 mutation の 4 点（最近傍 = 包含・board 座標での距離・局所値が混ざらない・二重計上防止）を
落とさないこと。
