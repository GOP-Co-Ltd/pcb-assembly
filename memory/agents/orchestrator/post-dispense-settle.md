# 塗布後の静定待ち（post_dispense_settle_seconds）

ブランチ: `feature/2026-10-23/post-dispense-settle`

## 要件（ユーザー合意 2026-10-23）

- すべての塗布方式で、塗布の移動が終わったあと、ヘッドを上げる前に静定待ちを入れる
- 全方式共通の新しい装置設定 `post_dispense_settle_seconds`（既定 0.1 s、0 以上）。pad ごとには上書きしない。
  微小線の下降前の静定待ち `micro_line_settle_seconds` はそのまま残す
- 通常の塗布（面・線・点、`FillSequence`）: 停止 → 静定待ち → リトラクトと上昇を同時に開始
- 微小線（`MicroLineSequence`）: 走行 → 静定待ち → 上昇 → リトラクト（上昇後のリトラクトは変えない）

## 設計

- `FillSequence` は速度 0 の `G4 P0` のあとに `G4 P<settle>` を置くだけ。Klipper の `manual_stepper.do_move` は
  MOVE の冒頭で必ず `sync_print_time()` を呼ぶので、`SYNC=0` のリトラクトも待ちの後から上昇と同時に始まる
  （最初は `SYNC=1` を挟んだが、レビューで冗長と分かり外した。`/home/gop/klipper/klippy/extras/manual_stepper.py:75`）
- `DispenseSettings.post_dispense_settle_seconds` で両シーケンスに渡す
- 設定ページは「Z 高さ」グループ（上昇高さの隣）に置く

## テスト観点

- config: 既定 0.1、負値の拒否
- FillSequence: G4 P0 → 待ち → SYNC → リトラクト → 上昇の順、0 なら従来どおり（点塗布も）
- MicroLineSequence: 走行 → 待ち → 上昇 → リトラクト
- applicator（FakeKlipper の実 G-code）: 線 pad で待ちとリトラクトの位置
- 設定 API の書き込み

## 計画外の判断・記録
- レビュー（code-reviewer、approve）の裁定
  - S1 採用: `SYNC=1` を外し、コメントを Klipper の実際の挙動に合わせた
  - S2 採用: 待ち 0 のテストが退行を検出できなかったので、G4 の数で確かめる形にした
  - S3 申し送り: dataset metadata（`PasteDatasetConfig`）は `post_dispense_settle_seconds` を記録しない。
    待ちの前後で集めたデータを区別できない。schema 改訂を伴うので今回は見送り
  - S4 実機確認へ: 流量キャリブレーションと paste_volume の校正は待ちなしで取ったもの。取り直しが要るか
  - S5 申し送り: PR #34 の tact 見積りに、成分 1 本ごとの塗布後の静定待ちを足す
  - nit 採用: docstring、テストの G1 位置（`g1[-2]`）、ラベル「塗布後の静定待ち（上昇前）」
