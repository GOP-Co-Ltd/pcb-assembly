# ノズルクリーニング機能（塗布ジョブ開始時）

計画書: `~/.claude/plans/claude-n-shimmying-hopcroft.md`
ブランチ: `feature/2026-09-14/nozzle-clean`

## 確定仕様（ユーザー回答）

- クリーニング位置で少量パージ → 十字往復でこすり（X に ±n → Y に ±n を passes 回）
- 塗布ジョブ開始時のみ（pad N 枚ごと・時間ごとはスコープ外）
- クリーニング面 Z と押し込み量を分離（シリコン摩耗時に押し込み量だけ調整）
- 教示 UI は既存「ノズルキャップ位置の設定」ページに同居。コア＋ジョブ＋記録 UI を 1 MR

## 採用した判断と理由

- こすり幅は X/Y 共通の 1 パラメータ（`stroke`）。要求は「前後左右 n mm」であり、
    X/Y 別振幅は要求されていない柔軟性
- `NozzleClean.x/y/z` はデフォルトを持たない。持たせると設定画面から `press_depth`
    だけ保存したとき座標欠落テーブルが structure に成功し、座標 0,0,0 へ移動する事故になる
- `clean_nozzle` は retract しない。既存 `paste_solder.py` の `retract()` を後ろに置いたまま
    流用する。二重リトラクトは `FillSequence` のプライム収支（`retract_amount` ぶん prime
    する前提）と合わず初弾が痩せる
- `validate_reach` を送信前に必ず通す。パージ後にこすりで ValueError が出ると
    シリコン上に塊を残してジョブが落ちる
- ドメインは `parking.move_to_cap` 型（純関数が GCode を返す）。`FillSequence` 型は
    吐出レート等の導出計算を複数呼び出し側で共有するための形で、本機能は導出値が
    `press_z` と clamp 済み速度の 2 つだけ
- 置き場所は `pasting/nozzle_clean.py`。`parking.py` がトップレベルなのは終了時駐機が
    machine_type 非依存だから。本機能は paste 専用かつ `pasting.applicator` に依存する

## 却下した案

- 新規 feature ページ「ノズルクリーニング位置の設定」→ キャップと同一の物理作業・
    同一タイミングなので、1 語違いのページが 2 つ並ぶ取り違えリスクだけが増える
- `/settings` の数値欄だけで教示 → クリーナー面 Z は設計値から計算できず、
    ジョグして座標を読み取り手打ちは転記ミスの温床
- `deposit_at` でパージ → board 座標系の Z を組み立てるためマシン座標の面と噛み合わない
- `purge_dwell`（パージ後の待ち）→ 初版で効果を確認できない knob を増やさない

## 進捗

- [x] 段階 1 計画（承認済み）
- [ ] commit 1 設定モデル
- [ ] commit 2 ドメイン
- [ ] commit 3 ジョブ組込み
- [ ] commit 4 記録 API と設定露出
- [ ] commit 5 ページ
- [ ] 自己レビュー + code-reviewer
- [ ] MR

## 実装中の気付き（段階 4 で回収する）

（随時追記）
