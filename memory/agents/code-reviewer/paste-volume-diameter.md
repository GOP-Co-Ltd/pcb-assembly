# code-reviewer: 点塗布の直径ベース塗布量推定（MR1: パージ廃止）

## MR1 verdict: request-changes → 全件対応して解消

### must-fix

| # | 指摘 | 裁定と対応 |
|---|---|---|
| M1 | ローディング待機中、ノズルが銅板の真上・AirPump ON のまま無期限ブロック。押すと板へ落ち、配分式が全 sample で系統的にずれる | **受理**。`_move_clear_of_plate` を追加し、applicator を開く前に board (0, -15mm) へ退避。可動域外なら移動せず jog を促すログに留める（装置を止めるほどではない） |
| M2 | 回転ローディングの ParamSpec 4 つが UI に届かず完全に死んでいる（`loading_controls.html` の回転セクションが `loading_rotation_defaults` で gate されている）。実レンダリングで実証 | **受理・選択肢1を採用**（回転 UI を出す）。ユーザーの「インタラクティブローディング」は既存 `loading` ジョブと同等の操作性を指すと解釈。`_paste_dataset_context` が `loading_` prefix を落として既定値を渡す。**再発防止に UI 実描画のテストを追加** |
| M3 | `skip_if_no_real_sessions` が「現版が1つでもあるか」なので、v3 を1本収集した瞬間にゲートが開き旧版 11 本で `from_roots` が失敗する | **受理**。`current_schema_sessions()` が現版 session の**パス列**を返し、テストはそれを `from_roots` へ渡す。旧版混在（実測 v1×5/v2×6）でも壊れない。tmp で実証確認済み |

### should-fix

| # | 指摘 | 対応 |
|---|---|---|
| S1 | 収集ジョブ docstring がパージ時代の前提のまま（「手動ローディングを挟まない」「この前提（手動プライム）」） | 受理・修正 |
| S2 | 要件書 L327 に同じ取り残し | 受理・修正 |
| S3 | テスト docstring が v2 のまま 5 箇所 | 受理・修正 |
| S4 | 収集ジョブのローディング組み込みにテストが無い | **部分対応**。フルランは実 Klipper が要るため、`loading_param`／`accepts_commands`／回転 ParamSpec の構造ピンと、M2 の UI 実描画テストで再発防止を敷く。実行経路そのものの確認はユーザーの実機検証に委ねる（計画どおり） |
| S5 | `capacity == len(grid)` が恒真で UI に同じ数が 2 回出る | 受理。表示を「格子 N セル（すべて計測可能点）」の 1 つへ集約。フィールドは `plan` 側のエラーメッセージで使うので残す |

### nit

| # | 指摘 | 対応 |
|---|---|---|
| N1 | `PASTING_LOADING_FEATURES` が stale（未参照の死んだ定数） | 受理・削除。docstring の「4 feature」も修正 |
| N2 | ピン fixture のキー順が DTO 出力順と食い違う | 受理。`to_dict()` の出力で書き直し、要件書の JSON 例も `total` → `loading` へ |
| N3 | `validate_dispense_reach` の空リスト初期化が冗長 | 受理。`for cell in plan.cells` の直接ループへ |
| N4 | `LoadingTotals.amount_ul/.rotations` と `PasteDatasetLoading.total_ul/.total_rotations` の名前揺れ | **却下**。`LoadingTotals` は web 層の既存型で、改名はこの MR の要求外。変換は 1 箇所のみで実害なし |

### レビューで確認され問題なしとされた点

要求 1（除外領域の廃止）/ 3（配分式から purge が消える）/ 4（v3 のみ・v2 を明示的に拒否）/
5（`src/ml/` 不変更）、スコープ外（`paste_solder` の初回パージ・testboard の PURGE pad）の温存、
seed 期待値の再確定が緩めでないこと、規約準拠。

______________________________________________________________________

## MR2 verdict: request-changes

対象は staged 分のみ（MR1 は `6fb6ba4` で commit 済み）。

### 検証結果

| コマンド | 結果 |
|---|---|
| `make format`（`pre-commit run -a`） | pass（書き換え無し） |
| `make type`（`pyright`） | pass（0 errors） |
| `pytest -m "not hardware"` 新規/影響分 | pass（paste_volume 135 / dataset 273） |

`tests/test_package.py::TestPastingImportLight` も pass。`import pcbasm.pasting` が
cv2 を引き込まない契約は壊れていない。全体スイートは orchestrator 側で実行中のため回していない。

### must-fix

| # | 対象 | 問題 | 根拠 | 確信度 |
|---|---|---|---|---|
| M1 | `estimator.py:29,122-130` | 計画に無い 4 つ目の不採用理由 `pixel_per_mm_mismatch` を estimator 層で hard reject | 計画書 L188 は 3 分岐と明記。L237（MR4）は「pixel_per_mm の条件不一致は失敗させず `condition_mismatch` に載せて評価は続行」。両立しない。`_SCALE_TOLERANCE = 0.2` は実測の裏付け無し | 高（計画不一致）/ 中（実害） |
| M2 | `calibration.py:141` | `detection` セクションのキー欠落を attrs 既定値で黙って埋める（`min_contrast` を消しても 20.0 で通る。実証済み）。`model`/`conditions` は既定値が無く弾ける | 計画書 L219「検出ハイパラは校正と不可分」。`DotDetectionSpec` の既定値を変えるとキー欠落ファイルの意味が黙って変わる | 高（実証） |
| M3 | `detect.py:143` | `min_area_px=0`（`validate()` が許す）で `detected=True, diameter_mm=0.0` が出る。`aggregate_views` の中央値母数に 0 が混じり直径が落ちる | `DotMeasurement` docstring L80-81 の不変条件（detected=False ⟺ 0.0）を破る。MR3 で ParamSpec 露出（計画書 L224） | 高（実証） |

### should-fix

| # | 対象 | 問題 | 確信度 |
|---|---|---|---|
| S1 | `aggregate.py:3-6` / `estimator.py:83-84` / `test_estimator.py:181-199` | 「厳密に一致」は view 数が奇数のときだけ。偶数は中央 2 つの平均で崩れる（実素材 2 view の相対差 1.1e-4〜3.6e-6）。5 view 中 1 つ落ちて 4 view になる = フォールバック経路そのものが偶数 | 高 |
| S2 | `test_estimator.py:71-75` | 期待値が指令量 0.20。実データのラベルは指令量×0.713（index 46: commanded 0.2000 / label 0.1427）。推定 0.1340 はラベルに対して −6%、指令量に対して −33%。`rel=0.5` が食い違いを隠している | 高 |
| S3 | `test_model.py:179-189` | 「被覆域内で V(d)<=0」の拒否（`model.py:150-154`）がテスト 0 件。`if model is not None` の分岐で常に非拒否側を通る。到達入力あり | 高 |
| S4 | `test_model.py:195-201` | `is_monotonic_in_range()` が False を返すケースが無い。到達入力あり | 高 |
| S5 | `estimator.py` 全体 | 中央値集約の前提である `diagnostics.monotonic_in_range` を誰も参照しない | 中 |
| S6 | `detect.py:99-100,169-172` | 引数名 `pre_rgb`/`post_rgb` と `cv2.COLOR_BGR2GRAY` が食い違う。リポジトリ規約は BGR（`vision/image.py:33`）。計画書のシグネチャがこの名前なので実装は計画どおりだが、RGB を渡されると静かにずれる | 高 |
| S7 | `estimator.py:98-99` | 1 view が構造不正だとセル全体を捨てる。マルチ view の目的（フォールバック）と整合しない可能性 | 中 |
| S8 | `estimator.py:106` | view 0 個を `no_deposit_detected` として返す。原因の切り分けが効かない | 中 |

### nit

- `test_detect.py:10,13` の `Path` / `np` が未使用（ruff は F401 ignore）
- `test_estimator.py:101` / `test_model.py:79,86` の関数内 `import attrs`
- `tests/pcbasm/test_atomic.py:3` の docstring が「計画書 MR1『新規 src/webui/atomic.py』節」のまま（移設前からの持ち越し）
- `reader.completed_sessions` の docstring の排除根拠と実装がずれる（`finalize_incomplete` は `.incomplete` へ metadata.json を書いてから rename する）
- `detect._darkening` の結果を 2 箇所が別々に `.astype(np.uint8)` する

### 問題なしと確認した点

要求 1（Protocol / ABC が 1 つも無い）、2（`measure_dot` の `(None, 理由)` は構造的不正のみ、
「写っていない」は正常系）、4 のうち未知キー拒否・暗黙変換拒否・版違い拒否と `strict_bool`
（dataset schema に bool フィールドが無く既存 hook への影響なし。dataset 273 件 pass で確認）、
5（path traversal 防御：絶対 path・脱出・欠損すべて拒否）、6（正確な小数値のピンなし）、
7（cv2 非引き込み契約）、成果物汚染なし。

### 注記（レビュー中の作業並行）

行番号はすべて **staged 版**（`git diff --cached`）に対するもの。レビュー中に
`detect.py` へ unstaged で `detection_mask()` が追加された（MR3 のモンタージュ用と
思われる。呼び出し元もテストも無い）。`fit.py` / `test_fit.py` も untracked で出現。
MR2 の commit へ混ぜないこと。

### MR2 の裁定（orchestrator）

**全 16 件を受け入れて対応した（却下ゼロ）。**

| 指摘 | 対応 |
| --- | --- |
| M1 | `_SCALE_TOLERANCE` と `pixel_per_mm_mismatch` を削除。条件照合は MR4 の evaluate 層へ。テストは「スケール差は被覆域の判定として現れる」形に書き換え |
| M2 | `parse_calibration` に `_missing_detection_keys()` を追加。`DotDetectionSpec` の全 field が document にあることを要求。欠落 5 パターンと非 object を parametrize でピン |
| M3 | `DotDetectionSpec.validate()` の `min_area_px` 下限を 0 → 1 へ。`min_area_px=0` の拒否をピン |
| S1 | `aggregate.py` と `estimator.py` の docstring を「奇数なら厳密／偶数は 2 次の微小差」へ修正。実素材相当の view ばらつき（相対 0.2%）で相対 1e-4 未満に収まることを `test_model.py` でピン |
| S2 | 期待値をラベル 0.1427（実データで比 0.7135 を確認）へ、tolerance を `rel=0.5` → `rel=0.15` へ。`data/testing/paste-volume/README.md` に教師ラベル列を追加 |
| S3 / S4 | レビュアーが示した到達入力をそのまま使い、拒否側・単調性 False 側をピン |
| S5 | 「条件照合も `monotonic_in_range` も推定器は見ない」と `DiameterVolumeEstimator` の docstring に明記 |
| S6 | `pre_rgb` / `post_rgb` → `pre_bgr` / `post_bgr` へ全面改名し、docstring に「OpenCV の BGR」を明記。計画書のシグネチャより実装の正しさを優先した |
| S7 | 「構造的不正は呼び出し側のバグなので早く落とす」を docstring に明記（挙動は据え置き） |
| S8 | `no_views` を 4 つ目の理由として追加。M1 で 1 つ減っているので分岐数は変わらない |
| nit ×5 | 未使用 import 削除、関数内 `import attrs` を module 冒頭へ、`test_atomic.py` の docstring 修正、`completed_sessions` を名前でも弾く実装へ変更しテスト追加、`_darkening` を uint8 で返して二重変換を解消 |

**学び**: レビュアーが「実測した」と書いた数値（ラベル比 0.7135、拒否に到達する係数入力）は
すべて再現できた。指摘に到達入力を添えてもらうと裁定が要らなくなる。

## MR3・MR4 のレビュー（verdict: request-changes）

must-fix 2 / should-fix 5 / nit 7。**全件受け入れて対応した（却下ゼロ）。**

| 指摘 | 内容 | 対応 |
| --- | --- | --- |
| M1 | `save_name` が suffix 付きだと sanitize を通らず、`../../../tmp/x.paste-volume.json` や `/etc/x.paste-volume.json` で保存先の外へ書けた。`sub/x...` は保存に成功するのに `list_calibrations` が top level しか見ないので一覧に出ない | `calibration.calibration_path(root, name)` を新設し、path 片を必ず 1 つへ畳んで root 直下に閉じる。脱出 5 パターンと「書いたら一覧に出る」をピン |
| M2 | `persisted_params` に `volume_calibration` を足したのに、JS が `input.value` を読まず `select.value` も設定しないので、前回の選択が毎回捨てられる。忘れると無言で検証されない。fetch 失敗時は選択肢ゼロの `<select>` だけが残る | 差し替え前に `input.value` を退避して `select.value` へ復元。消えた校正は「（見つかりません）」の選択肢として残す。fetch は差し替え**前**に行い、失敗時はテキスト入力のまま残す |
| S1 | 「例外を投げない」と docstring にあるのに、artifact の `write_text` と matplotlib が guard の外。落ちるとジョブ FAILED で zip が作られない | `_evaluation_artifacts()` へ切り出して `try/except` で包む。誤差はログに出ているので summary だけ返す |
| S2 | 評価レポートで blank と本物の検出失敗が `rejected_reasons` に合算され、達成条件の「検出失敗 0」が見えない | `detection_failure_count` / `blank_count` / `dispensed_count` を追加。採用率の分母も塗布セルに直した |
| S3 | `pasting/README.md` の表に `evaluate` が無い（`reader` も MR2 の取り残し） | 両方追記 |
| S4 | 選択肢ラベルと詳細行を JS で連結していた（計画書 L249 違反） | router が `option_label` / `details` を返す形へ。JS は代入のみ |
| S5 | 「壊れた校正名でも収集は SUCCEEDED」のピンが無い（存在しない名前だけ） | parse 失敗の経路をピン |
| nit ×7 | `job-param-note` の CSS 未定義／MR3 メッセージが実 diff と食い違う／`_spread(cells,1)` の ZeroDivisionError／`detection_mask` が `min_area_px` を反映しない／`contrast_percentile` の minimum／無警告上書き／`_SCALE_TOLERANCE` の根拠 | CSS は `job-param-help` を使う。MR3 は `git commit --amend --only` でメッセージ修正。`count<=1` を guard。docstring 追記。上書き時にログ。5% の由来を注記。`contrast_percentile` は ParamSpec.minimum が閉区間なので排他下限を表現できず据え置き |

**MR2 のコミットに `detection_mask` と `min_area_px` 修正が紛れ込んでいた。**
レビュー中に MR3 用の変更を `detect.py` へ入れ、その後 MR2 のレビュー対応で同じ
ファイルを触ったため、`git add src/pcbasm` が両方を拾った。**レビュー実行中に
レビュー対象ファイルを触らない**か、触るなら commit 前に `git diff --cached` を
1 ファイルずつ読む。

**学び（2 巡目も同じ）**: レビュアーが指摘に添えた実測値（脱出する path、
`merge_job_param_defaults` を入れた描画結果）はすべて再現できた。到達入力つきの
指摘は裁定コストがゼロになる。

## ページ統合コミット `fccebbd` のレビュー（verdict: request-changes）

must-fix 2 / should-fix 10 / nit 8。**全件受け入れて対応した（却下ゼロ）。**

| 指摘 | 内容 | 対応 |
| --- | --- | --- |
| M1 | `open_kernel_px` に上限が無く `np.ones((k,k))` が MemoryError。`build_calibration` が try の外で「例外を投げない」契約が破れ、1 時間の収集が FAILED になる。実測で 1048577 が preflight を通過し 1 TiB 確保を試みた | `MAX_OPEN_KERNEL_PX = 99` を `validate()` へ（`parse_calibration` 経由の推定経路も同時に守られる）。**zip 生成を校正生成の前へ移し**、契約に依存せず順序で解いた。`fit_calibration` 全体も try で包んだ |
| M2 | `report_calibration` が図 → 保存の順で、matplotlib が落ちると**フィット成功でも校正が保存されない**。まとめも「図が無い」しか言わない | 保存を図より前へ。図は `_render_artifacts` 内で guard し `(artifacts, error)` を返す。まとめに「保存先」と「診断図なし」を併記 |
| S1 | `detection_spec_from_params` / `diagnostic_lines` が外部から呼ばれていない | `_` prefix へ |
| S2 | 収集側のハイパラ preflight にテストが無い | `TestPasteDatasetCollectionPreflight` へ 2 件（偶数カーネル・巨大カーネル）＋ `min_area_px=0` は ParamSpec.minimum が start 前に弾くことをピン |
| S3 | `persisted_params` が両ジョブで未ピン | 全量 parametrize でピン |
| S4 | **除外理由が誤りだった**。`calibration_path` は suffix 付きの名前でしか上書きせず、通常名は timestamp が付く。一方で 1 時間の主成果物が既定で保存されないのは筋が通らない | 収集用に `COLLECTED_SAVE_NAME_PARAM` を分け、空なら label から自動命名して**必ず保存**。`save_name` を persisted へ。再フィット側は「空＝保存しない」を維持 |
| S5 | `save_name` と `volume_calibration` が同一ファイルを指すと自己参照の検証になり、総体積誤差ほぼ 0 が「良い結果」に見える | preflight で拒否（収集前に落とす） |
| S6 | ページ導入文が収集の説明のまま | 校正まで作ることと、再フィットの案内を追記 |
| S7 | 新 fieldset 2 つと既定 True の初 bool の描画が未ピン | 全 7 フィールドの描画と checked をピン |
| S8 / S10 | README とページ docstring に旧ジョブ名 | 修正 |
| S9 | 図の失敗経路が未ピン | savefig を失敗させて「校正は保存され図だけ無い」をピン |
| nit ×8 | まとめの語／persisted の表現／`_MONTAGE_DISPENSED` のコメント／plan.py のコメント内 path 分割／`.paste-dataset-form` class と CSS コメント／e2e class 名／配置プレビューが無関係な入力で再取得 | すべて対応。`paste_dataset_finalize` で復旧した session に校正が付かない点は設計として妥当と判断し、docs に再フィットへの導線を書いて据え置き |

**学びの更新**: レビュアーは「例外を投げない」の検証に対し、葉を数え上げるのではなく
**順序で解け**（zip を先に作る）と指摘した。不変条件はコードの外側の順序で満たす方が
壊れにくい。3 巡連続で、指摘に到達入力が添えられていたものは裁定コストがゼロだった。
