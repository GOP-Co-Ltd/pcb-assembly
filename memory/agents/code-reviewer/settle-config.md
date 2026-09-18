# 待ち時間・検出サンプリングの設定化 レビュー

対象: `feature/2026-09-18/settle-config`（`git diff main`、staged）
計画: `memory/agents/implementation-planner/paste-tact-phases.md` MR-A 相当 /
実装判断: `memory/agents/orchestrator/settle-config.md`

## verdict: request-changes

## must-fix

### M1. `make format` が fail する（README のテーブルが未整形）

- 対象: `data/config-templates/README.md:47-54`
- 問題: mdformat がテーブルのパディングを書き換えるため hook が Failed になる。
    検証が通らない状態では commit できない（AGENTS.md「検証通過前に commit しない」）
- 根拠: `make format` 実行で `mdformat ... Failed - files were modified by this hook`。
    `git diff` にテーブル行の整形差分が出る。README は staged 済みなので
    `memory/make-format-skips-untracked.md` の既知事象とは別件
- 確信度: 高

### M2. 「machine.toml の値が実際に G4 として送られる」ことを見るテストが無い

- 対象: `src/pcbasm/pasting/session.py:83-97`、`src/pcbasm/pasting/capture.py:39`、
    `src/pcbasm/posctrl/alignment.py:146`、`src/pcbasm/posctrl/setup.py:249,276,287,298`
- 問題: 今回追加した機能そのもの（設定値 → 静定待ち）の観測テストが 1 件も無い。
    `tests/pcbasm/pasting/test_probe.py:80` はコンストラクタ引数 0.5 → `G4 P500` を見るだけで、
    `machine.settle.probe_sec` が届くことは見ていない。`session.py` で `move_sec` と `probe_sec` を
    取り違えても全テストと `make type` が通る。既定値を外した狙い（渡し忘れを型で検出）は
    「渡し忘れ」しか守らず「取り違え」を守らない
- 根拠: 計画書 MR-A「検証で確かめること」に
    「設定値が G4 の文字列（`G4 P500`）として移動 gcode に現れる」が明記。
    `grep -rn "G4 P" tests/` は `test_gcode.py` / `test_probe.py` / `test_fill_sequence.py` のみ
- 追加コストは小さい: `tests/pcbasm/pasting/test_capture.py` は既に `FakeKlipper` で送信 gcode を
    保持しており、`[settle] move_sec = 0.8` の machine.toml を tmp に作って `G4 P800` を見れば足りる
- 確信度: 高（テストが無いこと）／中（must か should かは orchestrator 裁定）

## should-fix

### S1. docstring の Example が必須引数を欠いたまま

`src/pcbasm/posctrl/position.py:20-26`、`board.py:30-37`、`offset.py:22-27`。
`settle_sec` は keyword-only の必須引数になったので、Example をそのまま実行すると `TypeError`。
確信度: 高

### S2. WebUI から `minimum_sample_count > sample_count` を保存できる

`src/web/api/config_store.py:300-315`。`Detection.__attrs_post_init__` の相互制約が web 側に無く、
`sample_count=10` / `minimum_sample_count=20` が machine.toml へ書ける。保存後は `machine.detection` が
ValueError を投げ、設定ページでは `routers/common.py:_resolved_value` の握り潰しで実効値が None に
なるだけ。実害はジョブ実行時の失敗として遅れて出る。既存の `probe.min_samples` / `max_samples` にも
同じ穴があるので新規劣化ではないが、`write_machine_settings` は `PasteDispenser.validate_values` と
同じ形でセクション単位の相互検証を掛けられる。確信度: 高（穴の存在）

### S3. `validate_positive_int` を追加したのに web 側で使っていない

`src/pcbasm/config.py:170-175` で追加した検証関数を `config_store.py:304-315` が使わず、
`value < 1` のインライン if を新規に足している。直上に同形の if（`pad_align.region_size_px` /
`max_passes`）があり、キーを既存 set へ足せば済む。結果として同じルールの実装が 4 箇所
（`Detection`、`OffsetObserver.__init__:75-88`、`config_store` の 2 ブロック）に散った。
refactor-conventions / webui-thin-wrapper の「ドメインルールを web に複製しない」に反する。確信度: 高

### S4. setup 側 observer は `max_attempts=1` のまま `minimum_sample_count` だけ 1 → 5

`src/pcbasm/posctrl/setup.py:257-263`。品質ゲートを 5 倍厳しくしたのに再試行が無い。
基準点が 10 枚中 4 枚しか写らない機体では、全ジョブの最初のステップが即 `CircleDetectionError`。
`toolhead_offset.py:404-406` は同条件で `max_attempts=3` / `retry_delay=0.5` を持っており非対称。
計画書 MR-A 手順 5「`OffsetObserver.retry_delay` の既定を 0.5 に揃える」も未実施（既定 0.0 のまま。
`max_attempts=1` 下では無害だが計画からの欠落）。確信度: 中（実機の検出率次第）

### S5. 30 → 10 の根拠（フレーム毎 σ ≒ 0.02 mm）が実測として残らない

`OffsetStatistics.std` は毎観測で計算されているのに、成功時はどこにも出ない
（`posctrl/setup.py:126-145` は失敗メッセージだけで使用）。成功時に `std_mm` をログへ出せば
10 枚で足りるかを実機ログから事後検証できる。今のままでは「実機で確認が要る 3」を目視の
再現性でしか判定できない。
なお `max_standard_deviation_mm` のゲートが見るのは `pstdev`（フレーム毎のばらつき）であって
平均の標準誤差ではないので、30 → 10 でゲートの期待値は動かない。ただし pstdev 自体の推定誤差が
相対で約 13% → 22% に増え、境界付近の点の採否が振れやすくなる。
確信度: 高（std が記録されていない）／中（統計の評価）

### S6. `[detection]` を性質の違う 2 つの検出器で共有している

`setup_board_calibration` の `CircleDetector`（機械的な基準円）と `ToolheadOffsetProcedure` の
`PasteDotDetector`（濡れた塗布痕）の両方を 1 セクションが駆動する。`settle` の合成（同一の物理現象）
と違い検出対象の性質が異なり、旧既定も setup=1 / toolhead=5 と実際に食い違っていた。
統合自体は「1 はゲートとして機能していない」という判断で妥当だが、README / docstring に
「どちらの検出にも効く」と書かれていないので、片方だけ渋いときに気づきにくい。確信度: 中

## nit

- N1: `tests/pcbasm/test_config.py` の `("sample_count", 1.5)` は直接構築だけの話。TOML 経由では
    cattrs が `int(1.5) = 1` に丸めるので弾かれない（実測確認済み）
- N2: `tests/pcbasm/pasting/test_height.py:217` の `settle_sec=0.0` が `min_samples` と `max_samples`
    の間に入っていて並びが崩れている
- N3: `machine.settle` / `machine.detection` は毎アクセスで cattrs structure し直す。ローカルへ
    受けている箇所と受けていない箇所が混在（`setup.py` / `toolhead_offset.py`）
- N4: 計画書 異常系の「`Settle` の非有限で ValueError」はテストが無い。
    `validate_non_negative_number` 側でカバーされ実害は無い（`inf` / `nan` が弾かれることは確認済み）

## 確認した点（指摘なし）

- **合成の妥当性**: A 群 8 箇所の `move_sec` 統合は妥当。実挙動が変わるのは 2 箇所だけ
    （`posctrl/setup.py` の基準点移動、`toolhead_offset.measure()`。どちらも 1.0 → 0.5 で申し送り済み）。
    残り 6 箇所は旧既定 0.5 と同値。`probe_sec`（PROBE 後）を分けたのも現象が違うので妥当
- **`time.sleep` → `GCode.wait` の等価性**: 等価。`G1 → G4 → M400` の順で、M400（`wait_moves`）が
    dwell 込みの print time を待つため `send_gcode` は dwell 経過後に返る。`measure()` の次の動作は
    `_adjustor.adjust()` → `observe()` の撮影なので、待ち → 撮影の順序は保たれる。変わるのは長さのみ
- **必須引数化の漏れ**: 8 クラスすべての構築サイトを grep で確認、漏れ無し。`**kwargs` / ファクトリ /
    レジストリのような動的経路も無い。ただし「取り違え」は型で守れない（→ M2）
- **WebUI**: 設定フォームは汎用なので JS / router にロジック漏れ無し。
    `tests/web/ui/test_layout.py:202` が SECTION_LABELS の網羅を既に強制している
- **成果物汚染**: 変更ファイルに `</content>` 等の混入なし

## 検証結果

- make format: **fail**（mdformat が `data/config-templates/README.md` を書き換える → M1）
- make type: pass（0 errors）
- make test-no-hardware: pass（3368 passed, 184 deselected）

______________________________________________________________________

# 再レビュー（指摘対応後）

## verdict: approve

must-fix 2 件・should-fix 6 件すべて対応済み。新たな must-fix / should-fix は無し。

## 対応の確認

- **M1**: `make format` を 2 回連続で実行し、どちらも unstaged 差分なしを確認（解消）
- **M2**: `TestSettleWiring` は狙いどおり配線を固定している。`session.py` で `move_sec` /
    `probe_sec` を入れ替えると `test_probe_dwells_for_the_probe_settle` が落ち、片側だけ誤ると
    `test_height_measurement_dwells_for_the_move_settle` の `G4 P800` が落ちる。
    `session.probe_executor` / `height_measurer` は元から公開 attrs で、テスト都合の public 化ではない
- **S1**: 3 クラスの Example に `settle_sec` が入った
- **S2**: `_validate_detection_counts` は書き込み前（追加ファイル保存より前）に走り、TOML 側の
    既存値・`Detection` の既定値とマージして判定する。実測で確認:
    セクション無し + `minimum=11` → 撥ねる / `minimum=9` → 通る / TOML に `sample_count=-5` が
    残っている状態でも撥ねる / 両方同時に上げれば通る
- **S3**: 既存 set へ統合され、if の新設は解消
- **S4**: `DETECTION_MAX_ATTEMPTS` / `DETECTION_RETRY_SEC` を `posctrl` の公開定数にして
    基準円側・塗布痕側で共有。計画書 MR-A 手順 5 の retry 既定の不整合もこれで解消
- **S5**: `OffsetObserver.observe()` 成功時に「有効検出数 / 撮影枚数、標準偏差 X・Y」を INFO 出力。
    README にも読み方を記載
- **S6**: `Detection` docstring と README に「基準点の円と塗布痕の両方に効く」と明記

## nit（非ブロッキング。据え置き可）

- `tests/pcbasm/pasting/test_capture.py` の `test_height_measurement_dwells_for_the_move_settle` は、
    docstring が主張する「点間移動は probe_sec ではなく move_sec を使う」を assertion が固定していない。
    `G4 P800` と `G4 P300` の**両方が存在する**ことしか見ていないので、`session.py` の 2 引数を
    完全に入れ替えても（移動 300 / プローブ 800 になっても）このテスト単体は通る。
    スイート全体では `test_probe_dwells_for_the_probe_settle` が落ちるので漏れは無い。
    強くするなら `sent_lines` の順序を使い、XY 移動の `G1` の直後の dwell を見る
- `_validate_detection_counts` は、手で壊した TOML（`sample_count = "abc"`）に対して
    `int()` の素の `ValueError` を投げる（`UnknownFieldError` ではないので 400 に落ちない）。
    ただし同じ TOML では `read_machine_settings` も先に落ちるので設定ページ自体が開けず、
    新規に生じた経路ではない
- `position.py` / `offset.py` の Example は `settle_sec=0.5` をハードコードしている。
    `board.py` は `machine.settle.move_sec` を示しており、こちらが今回の意図に沿う書き方
- `DETECTION_RETRY_SEC` と引数名 `retry_delay` で単位サフィックスの流儀が揃っていない
- `OffsetObserver.observe()` の INFO ログは観測ごとに 1 行出る（`XYPositionAdjustor` は最大 10 反復 ×
    4 コーナー）。セットアップで十数行、ツールヘッドオフセットで点数ぶん増える
- 前回から据え置きの 2 件（TOML 経由の `sample_count = 1.5` 丸め、`machine.settle` の毎アクセス
    structure、テスト引数の並び）は判断に同意する。UI 側は int 型で 1.5 を撥ねることを確認済みで、
    手編集 TOML の丸めは他の int 設定と同じ挙動

## 検証結果（再実行）

- make format: pass（2 回連続で差分なし）
- make type: pass（0 errors）
- make test-no-hardware: pass（3373 passed, 184 deselected）
