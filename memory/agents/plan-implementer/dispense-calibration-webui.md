# 吐出量キャリブレーション webui 統合（dispense_calibration ジョブ + メニュー + ①②③ + UI + 既存資産削除）

承認済み計画: `claude-webui-algorithm-practical-1-drifting-dewdrop.md`
ブランチ: `feature/20260624/dispense-calibration`（A 基盤・B 算出は実装済み・合流グリーン）。

本 agent は webui 統合のみ担当（pcbasm A/B は厳守して使う）。

## スコープ
1. 新ジョブ `dispense_calibration`（jobs/pasting.py）: 共通土台確立 + メニューループ + ①②③ + ApplyPayload。
2. 既存資産削除: flow_calibration ジョブ / 質量キャリブ表 / loading/calibration endpoint。
3. UI: dispense_calibration.html / partials/calibration_menu.html / calibration_menu.js。
4. pages.py 登録、ルーター/JS リネーム追従。
5. テスト: test_pasting(jobs)・test_pasting(routers)・test_pages 更新。

## 計画外の判断ログ

### ③ max_fill_speed スイープでの rate_cap の扱い（計画書の矛盾を解消）
計画書 §③ は同一文に「各速度で `rate = q×v`」と「`draw_line(rate_cap=∞)`（cap 無効）」の
**両方**を書いており矛盾している。`FillSequence` の実効塗布速度は
`effective_fill_speed = min(max_fill_speed, rate_cap / q)`。
applicator の `max_fill_speed` は config 固定で per-line に変えられず、`draw_line` にも
速度引数は無い。よって `rate_cap=math.inf` だと全線が同一 `max_fill_speed` で引かれ、
**速度スイープが成立しない**（機能の目的に反する）。

採用: 各線で `rate_cap = q × v`（v=schedule 速度, q=total_amount/line_length）を渡す。
これにより `effective_fill_speed = min(max_fill_speed, v) = v`（applicator の max_fill_speed が
v を下回らない限り）。スイープが正しく機能する唯一の経路。`draw_line` の戻り値（実効速度）も
log してユーザーが頭打ちを目視できるようにする。`FillSpeedSweep.speed_at(index)` で確定。
（pcbasm IF は不変・厳守。webui 側でこの rate_cap を計算するだけ。）

### loading_controls とメニューの段階共有（"_run_loading_loop を一般化" の解釈）
計画書は「メニューループは `_run_loading_loop` を一般化」「dispense_calibration.html は
loading_controls を include」と言う。loading_controls.js は
`progress_stage === data-loading-stage` のときのみボタンを有効化する。

採用: dispense_calibration の loading_controls は **押出/吸引のみ**（rotation/質量キャリブ無し）を
出し、`data-loading-stage` を `CALIBRATION_MENU_STAGE`（"キャリブレーションメニュー"）に
設定して、メニュー段階で押出/吸引（プライム）できるようにする。menu loop は run_calib に
加えて extrude/suck（`parse_loading_command`）も処理する。loading_controls の finish ボタン
（{type:"finish"}）は menu loop が graceful に無視ログする（終了は calibration_menu の
終了ボタン= run_calib finish を使う）。
→ loading_controls.html の `data-loading-stage` を template 変数 `loading_stage`
（既定 "ローディング"）でパラメータ化。既存 loading 画面は既定値で不変。

### retract は各キャリブの線引き前に 1 回だけ（線間・線後の追加 retract は不要）
`FillSequence.to_gcode` は prime→吐出→retract を 1 本に内包する（step4 が retract_amount を
引き戻す）。よって draw_line 後の dispenser は既に retract 済みで、次の draw_line の prime が
再び正しく効く。当初 ①②③ で各線後に明示 `retract()` を呼んでいたが、これは **二重 retract**で
過剰引き戻しになるため除去。`_run_paste_solder` の方式に倣い、各キャリブの線引きループ**直前に
1 回だけ** `retract()`（プライム済みペーストを baseline に戻す）を入れる形に修正。

（以降随時追記）

## 既知の制約・残課題
- e2e（make test-e2e）は親が実行。`TestDispenseCalibrationOverBrowser` /
  `TestLoadingOverBrowser`（質量キャリブ表削除追従）を追加済み。
- 実機での ①②③ 実測・ApplyPayload の妥当値確認・ブラウザ目視はユーザー
  （large-refactor-workflow）。flow_calibration の hardware テスト 2 件は削除。
- ②の質量計測は per-rate prompt（計画書通り）。各レートの線を個別計量する運用は
  ユーザーの物理プロトコル次第。

## 検証結果
- make format: pass（ruff / ruff-format / docformatter すべて Passed）
- make type: pass（pyright 0 errors）
- make test-no-hardware: pass（1353 passed / 62 deselected[hardware]）
