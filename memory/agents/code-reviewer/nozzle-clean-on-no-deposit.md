# ノズルクリーニングを「塗布未検出時のみ」に変更 レビュー

## verdict: request-changes

## must-fix

### M1. `_clean_and_purge` の `retract()` がプライム状態と対になっておらず、クリーニングのパージが痩せる

対象: `src/web/api/jobs/pasting/flow_calibration.py:215-238`

根拠:

- `FillSequence.to_gcode`（`src/pcbasm/pasting/fill_sequence.py:149-177`）は先頭で
    `retract_amount` を prime し、末尾で同量 retract する自己完結型。`run_flow_calibration`
    の最後の `deposit_at` を終えた時点で plunger は `retract_amount` 引き込まれている
- `_clean_and_purge` はその状態から `clean_nozzle` を呼ぶ。`clean_nozzle` は
    `applicator.load(clean.purge_ul)`（`nozzle_clean.py:232-233`）を押すだけなので、
    先端から実際に出るのは `purge_ul - retract_amount`。実 config
    （`config/machine.toml`: `retract_amount = 0.03` / `nozzle_clean.purge_ul = 0.1`）では
    0.1 uL のつもりで 0.07 uL しか出ない。ログは `clean_nozzle` が「パージ 0.100 uL」と
    出すので表示と実体がずれる
- 旧 `paste_solder` は `clean_nozzle` を FillSequence より前（プライムが抜けていない状態）で
    呼び、直後の `retract()` が唯一の「プライム → 引込み」遷移だった。新しい呼び出し位置では
    その前提が崩れている
- 同じ落とし穴は `src/web/api/jobs/pasting/paste_volume_calibration.py:587-590` に明記済み
    （「先頭で retract すると plunger が baseline より引き込まれた状態で全点が走る」）
- `src/web/api/routers/nozzle_cap.py:143-146` のテスト実行は「塗布ジョブが行うのと同じ手順」を
    謳って fresh applicator から `clean_nozzle` → `retract` する。テスト実行と本番ジョブで
    実際の吐出量が食い違う

派生（`purge_ul == 0` または `nozzle_clean is None` のとき）: 何も押していないのに `retract()`
だけが走り、plunger が `2 * retract_amount` 引き込まれる。`purge` も `None` ならその状態で
2 回目の測定 1 点目が走り、`retract_amount` ぶん（0.2 uL 指令に対し 0.03 uL = 15%）少なく
吐出する。詰まりを判定しようとしている当の測定にバイアスが乗る。

確信度: 高（機構）。0.07 uL で詰まりが抜けるかの実害判断は実機・ユーザー側。

## should-fix

### S1. 流量キャリブレーションが無効な基板ではクリーニングが一切行われなくなる

対象: `src/web/api/jobs/pasting/paste_solder.py:121-134`

`plan_flow_calibration`（`runtime.py:96-101`）は `not config.enabled or not points` で `None` を
返し、`points` は基板ごとの `model.flow_calibration_points`。測定点未設定の基板や
`calibration_file` 未設定のマシンでは `flow is None` になり `run_flow_calibration_with_cleaning`
を通らないので、クリーニングが 1 度も走らない。変更前は無条件に走っていたため機能が消失する。
`nozzle_cap.html` の文言にもこの条件は書かれていない。確信度 高（経路）／仕様の可否はユーザー確認。

### S2. `NO_DEPOSIT_DETECTED` が 2 つの意味を兼ねている

`estimator.py:136` の `detected_view_count == 0`（本当に写っていない）と `estimator.py:146-147`
の `mean <= 0.0`（検出はできたがモデル体積が非正）が同じ理由文字列。後者は塗布が写っている
ので、全点がそれに当たるとクリーニング → 異常終了へ誤って落ちる。確信度 中（経路は確実、
被覆域かつ信頼域の内側で `mean <= 0` が起きるかは校正モデル依存）。

### S3. `nozzle_clean` も `purge` も `None` のとき、何もせずに再測定して落ちる

`flow_calibration.py:198-211` / `230-249`。ログは「ノズルをクリーニングしてやり直します」、
失敗文は「クリーニングとパージの後も塗布を検出できません」だが、どちらも実施していない。
静定待ち込みで 1 サイクル無駄に回してから同じ結論に至る。確信度 高、影響小。

### S4. 2 回目の測定が同じ測定点へ重ね塗りする

`detected_view_count == 0` は「検出器が見つけられなかった」であって「出ていない」の証明では
なく、照明・コントラスト起因の検出失敗もここへ落ちる。1 回目が実際には吐出していた場合、
2 回目の pre 画像に 1 回目のドットが写り、直径 → 体積モデル（絶対直径前提）へ差分の増分が
渡って推定が過小になる。確信度 中。orchestrator メモの実機申し送り 3 は再パージ点の重ねだけを
挙げており、測定点の重ねは別問題。

### S5. `config_store.py:159` のコメントがドキュメント同期漏れ

`# [paste_dispenser.nozzle_clean] — 塗布開始時のノズル先端クリーニング（マシン座標）` のまま。
確信度 高。

### S6. `_clean_and_purge` の `applicator.retract()` に `ctx.progress` / `ctx.checkpoint` が無い

前後の 2 ステップには両方ある。中断の粒度が揃わない。確信度 高、影響小。

## nit

- `ctx.progress("パージ")` は `paste_solder` の「初回パージ」と別ラベルで、UI 上は別工程に見える
- `_clean_and_purge` の未記録ログが `nozzle_clean._NOT_RECORDED` と別文言。定数化の意図
    （WebUI と文言を揃える）から外れる
- テストが「クリーニング → パージ → 再測定」の順序を検証していない
    （`_approaches_to_clean` / `_purges` の回数だけで `moves` の並びは見ていない）
- 削除したコメント「ローディング直後の先端が最も汚れているので、掃除はその後に行う」の知見が
    失われた。対話的ローディング後は汚れた先端のまま 1 回目の測定に入る（仕様どおりだが記録価値あり）
- `resolve_nozzle_clean` の可動域エラーは今も塗布ジョブ全体を落とす。掃除が条件付きになったのに
    検証だけ無条件（計画で明記された判断）

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（3353 passed, 184 deselected）
- 成果物汚染（`</content>` 等）: なし

______________________________________________________________________

# 第 2 版レビュー（検出を初回パージで行う作り直し / aa6c7b9）

対象: `git diff main...HEAD`（b72d52d + aa6c7b9）

## verdict: request-changes

## 前回指摘の追跡

- M1（プライム収支）: 解消。`_clean` が `prime → clean_nozzle → retract` で囲み、
    テスト `loads == [retract, purge_ul, -retract]` が収支を固定している。確信度 高
- S5（ドキュメント同期）: `config_store.py` は直ったが、**tracked な config テンプレート
    2 本と `tests/pcbasm/pasting/test_nozzle_clean.py` に同じ記述が残っている**（下記 M1）
- S1（クリーニングが走らない経路）: 契機が流量キャリブレーションから初回パージへ移った
    ことで、条件も `initial_purge_ul > 0` へ移った（下記 S2）
- S4（やり直しの重ね塗り）: 測定点ではなくパージ点の重ねとして再発（下記 S3）
- 掃除中の移動がプライム状態になる点は、main の
    「掃除 → リトラクト（移動前の垂れ止め）」と同じ順序なので指摘しない

## must-fix

### M1. 仕様と矛盾する記述が tracked ファイルに残っている

対象:

- `data/config-templates/kurousagi.paste/machine.toml:65-66`
- `data/config-templates/usaremino.paste/machine.toml:68-69`
- `tests/pcbasm/pasting/test_nozzle_clean.py:3`

いずれも「塗布ジョブ開始時のノズル先端クリーニング」「セクションごと無いとクリーニングを
行わずに塗布を開始する」のまま。後者は現仕様では誤りで、セクションが無くても
`purge_with_cleaning` はパージのやり直しを行い、写らなければジョブを落とす
（`purge_check.py:72-81` / `_clean` の未記録分岐）。テンプレートは利用者が機体設定を
起こす入口なので、誤った説明のまま配る影響が大きい。同じ文を持つ `config.py` /
`config_store.py` / README / WebUI 文言は本 diff で直っており、ここだけ漏れている。
確信度: 高

### M2. `nozzle_cap` のテスト実行が「塗布ジョブと同じ手順」でなくなった

対象: `src/web/api/routers/nozzle_cap.py:142-147`（本 diff では未変更）

docstring は「塗布ジョブが行うのと同じ手順を走らせ、続けてリトラクトする。ジョブでは
`clean_nozzle` の直後に `retract` が呼ばれるので、テストも同じ正味の状態で終える」と
明記している。ジョブ側は `prime → clean_nozzle → retract`（`purge_check.py:167-171`）に
変わったので、この記述は成り立たない。

実害: プランジャは直前の塗布ジョブの最後の `apply`（`FillSequence.to_gcode` 末尾の
retract）で引き込まれた状態で残る。そこからテスト実行すると `clean_nozzle` の
`load(purge_ul)` の先頭 `retract_amount` が引き込みの穴埋めに消え、先端から出るのは
`purge_ul - retract_amount`。実 config（`retract_amount = 0.03` / `purge_ul = 0.1`）で
0.07 uL。ジョブ側は prime 済みなので 0.1 uL。**WebUI のテストボタンで `purge_ul` を
追い込むと、本番のジョブでは 40% 多く出る。**
確信度: docstring の不整合は 高 / 吐出量の差は 中（テスト実行時のプランジャ位置は
どこにも記録されていないため、直前操作に依存する）

## should-fix

### S1. カメラ位置が可動域外だと「確かめられないこと」でジョブが落ちる

対象: `src/web/api/jobs/pasting/purge_check.py:9-11, 104-131`

モジュール docstring は「**確かめられないことではジョブを落とさない**」と太字で宣言し、
拾っているのは `capturer.capture` の `(None, 理由)`（crop が frame に収まらない）と
`measure_dot` の `(None, 理由)` だけ。`PointCapturer.capture`（`capture.py:58-67`）の
`stage.move` は可動域外で `ValueError` を投げ、これは握られずジョブごと落ちる。

カメラ位置は `camera_point_target` = board 点のマシン座標（ツールヘッドオフセットを
**含まない**）なので、パージ点そのものとは Y で 23.04 mm（`machine.toml` の
`paste_dispenser.toolhead.y`）ずれる。ノズルが届くパージ点でもカメラ位置が可動域外に
なり得る構成では、main では成功していたジョブが失敗する。位置合わせが基板上をカメラで
舐めている以上は実際上まれだが、明示指定のパージ点（`resolve_initial_purge` は基板外形の
内側しか検証しない）では起こり得る。確信度: 機構 高 / 実機で踏むかは 中

### S2. 初回パージが無効な機体ではクリーニングも詰まり検出も一切走らない

対象: `src/web/api/jobs/pasting/paste_solder.py:106-117`

`purge_with_cleaning` の呼び出しは `purge is not None` の内側。`resolve_initial_purge` は
`amount_ul == 0` を機能無効として `None` を返す（`initial_purge.py:55-56`）ので、
`initial_purge_ul = 0` の機体ではクリーニングが 1 度も走らない（main では無条件に走って
いた）。順路 pad が 0 件でも同じ。`nozzle_cap.html` の新しい文言にもこの依存は書かれて
いないので、運転者は「クリーニングが効いている」と読む。仕様上の可否はユーザー判断だが、
設定間の依存は WebUI 文言に書くべき。確信度: 経路 高

### S3. やり直しのパージが同じ点へ重なり、偽陰性がジョブ異常終了へ直結する

対象: `purge_check.py:104-131`

2 回目も同じ `purge.point` へ塗る。1 回目が実際には出ていて検出だけ失敗した場合
（照明・コントラスト起因）、2 回目の pre 画像には既に 1 回目のドットが写っており、
`measure_dot` が見るのは増分の暗化だけになる。ペースト上へペーストを足しても
`min_contrast = 20`（`dot.py:49`）に届かなければ `detected=False` となり、ノズルが
正常でもジョブが `ValueError` で落ちる。前回 S4 は測定点の重ねによる体積の過小推定
だったが、こちらは帰結が「基板 1 枚を止める」なので影響が重い。しかも失敗時点で
パージ点（既定は順路先頭 pad の中心）には 2 回ぶんのペーストが乗っている。
確信度: 中（機構は確実、`min_contrast` を割るかは実機依存）

### S4. crop 4.0 mm の根拠が「測定点より広い」だけで、検出感度の下限が記録されていない

対象: `purge_check.py:28-31`, `tests/web/api/jobs/pasting/test_purge_check.py:291-296`

`measure_dot` の blank ガードは crop 全体の **99 パーセンタイル**（`contrast_percentile`
既定 99.0）で判定するので、ドットが crop 面積の 1% を超えないと検出できない。
crop を 2 mm → 4 mm にすると面積が 4 倍になり、検出に必要な最小ドット径は 0.22 mm →
0.45 mm へ上がる。実機（`pixel_per_mm ≈ 28.68`、初回パージ 0.3 uL、データセットの
ドットは 0.2 uL で 0.6〜0.9 mm）なら 5% 前後で余裕はあるが、余裕は 20 倍から 5 倍へ
落ちている。定数のコメントに書いてあるのは「広く取っても差分で消える」ことだけで、
**広く取ると感度が下がる**ほうが書かれていない。確信度: 中（計算は確実、実機の
ドット径は未測定）

### S5. `PURGE_CROP_SIZE_MM` が job 層の直書き定数

対象: `purge_check.py:31`

対になる流量キャリブレーションの crop は `pcbasm.config.FlowCalibration.crop_size_mm`
で機体ごとに設定でき、WebUI の設定欄（`config_store.py:110`）にも出ている。パージ側
だけコード直書きで、レンズ・倍率の違う機体で調整できない。skill `webui-thin-wrapper` の
点検リストは「幾何計算が router/job に直書き」を漏れのサインに挙げている。
`flow_calibration.py` が job 層で撮影・塗布を束ねている前例があるので関数の置き場所自体は
妥当だが、寸法はドメイン側（config か `pcbasm.pasting`）が持つべき。確信度: 中（規約の
読み方に幅がある）

### S6. 仕様の中核分岐にテストが無い

対象: `tests/web/api/jobs/pasting/test_purge_check.py`

モジュール docstring が太字で置いている「撮影・計測に失敗したら判定を諦めて先へ進む」に
テストが 1 本も無い。`purge_with_cleaning` の 3 経路（crop 寸法を決められない /
塗布前の撮影失敗 / 塗布後の撮影失敗）はいずれも「パージはするがジョブは落とさない」で、
落ちないことと余計なクリーニングをしないことの両方を固定する価値がある。
`pixel_per_mm` を大きくした `PasteSession` を作れば crop が frame からはみ出すので、
fake の範囲で再現できる。確信度: 高

### S7. crop 寸法が実際に 4 mm であることを振る舞いで確かめていない

対象: `tests/web/api/jobs/pasting/test_purge_check.py:291-296`

`test_is_wider_than_a_flow_calibration_crop` は定数と config 既定値を比べるだけで、
コードを 1 行も通らない。skill `testing-strategy` の「書かない」に挙がる
「定数 literal の追試」に近い。検出系のテストはドットを frame 中央に置いているので、
crop 寸法をどう変えても通る（= 4.0 という値は実質未検証）。crop の外側にだけ暗い円を
置いたフレームで「4 mm の外は見ない」を確かめるほうが価値がある。確信度: 中

## nit

- `_purge_and_detect` の戻り値を `is not False` / `is False` で受ける三値分岐は、
    呼び出し側 2 か所で二重否定になって読みにくい。`detected is None or detected` の
    ような素直な書き方か、明示的な enum のほうが意図が出る
- やり直し中の `ctx.progress` が「ノズルクリーニング」のまま。2 回目のパージと撮影が
    クリーニング工程として表示される
- `_purge_and_detect` は撮影の前に `ctx.checkpoint()` を置いていない
    （`flow_calibration._capture_all` は撮影ごとに置く）。中断の粒度が工程間で揃わない
- `paste_solder` の summary は `初回パージ {purge.amount_ul}` 固定で、やり直した場合の
    実際の吐出量（2 倍）が残らない
- `_clean` の未記録ログ（`purge_check.py:165`）が `nozzle_clean._NOT_RECORDED` と別文言。
    定数化の意図（WebUI と揃える）から外れているのは前回 nit と同じ
- 静定待ちを置かずに塗布後を撮る点: 判定が `detected` だけなら移動時間 +
    `PointCapturer` の 0.5 s で足りる見込みで、流量キャリブレーションの 10 秒待ちは
    直径を測るための要件なので同列ではない。妥当だと思うが根拠がコードに書かれていない

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（3351 passed, 184 deselected）
- 成果物汚染（`</content>` 等）: なし
