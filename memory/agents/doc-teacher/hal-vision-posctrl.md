# src/pcbasm/{hal,vision,posctrl} docstring 理解度テスト

## 対象
- ファイル: `src/pcbasm/hal/`、`src/pcbasm/vision/`、`src/pcbasm/posctrl/` 配下の全 .py（docstring・コメントのみ）
- 読者と用途: 装置制御を保守する開発者が、HAL の抽象と実装、画像処理・キャリブレーション、位置合わせ制御の公開 API の契約（座標系・単位・前提・失敗時の戻り値）を理解し、正しく使い・修正できるようにする
- 生徒に読ませるファイル（R1 時点 1473 行）: `hal/stage.py`、`hal/klipper.py`、`hal/manual_stepper.py`、`posctrl/__init__.py`、`posctrl/position.py`、`posctrl/setup.py`、`posctrl/alignment.py`

## 評価用問題
### Q1 `XYZStage.move(x=10.0, y=20.0)` を呼んだ。これだけでステージは動くか。動かすには何をするか。
- 要点: 動かない（G-code を返すだけで送信しない）
- 要点: 返った G-code を `Klipper.send_gcode` で送る

### Q2 ステージが X=10 にある。`stage.move(x=1, relative=True)` を 2 回呼んで得た G-code を連結し、1 回の `send_gcode` で送った。移動後の X はいくつか。理由も。
- 要点: X=11（12 にはならない）
- 要点: 相対量は生成した時点の `get_position()` に足して絶対座標にするため、2 つとも X=11 への移動になる

### Q3 `ManualStepper` で、ステッパーを今の位置から 5 mm 進めたい。`move(5)` を送るだけでよいか。どうするか。
- 要点: よくない。`MOVE=` は絶対位置の指令
- 要点: 先に `reset_position()` で現在位置を基準値（0）に置いてから `move(5)` を送る

### Q4 `send_gcode` から戻った時点で、送った移動は物理的に完了しているか。完了まで待ちたいときはどうするか。
- 要点: 完了しているとは限らない（Klipper がスクリプトを処理し終えただけ）
- 要点: G-code の末尾に `GCode.wait_for_done()`（M400）を付ける

### Q5 `Klipper.send_gcode` が送出する例外を、(a) Moonraker に接続できないとき、(b) Moonraker が HTTP 400 以上を返したとき、それぞれ答えよ。
- 要点: (a) `httpx.HTTPError`（RuntimeError に包まない）
- 要点: (b) `RuntimeError`

### Q6 printer.cfg の `max_velocity` を書き換えて Klipper を再起動した。以前から使っている同じ `Klipper` インスタンスの `get_config()` で新しい値が取れるか。取るにはどうするか。
- 要点: 取れない（結果をインスタンスごとにキャッシュしている）
- 要点: `Klipper` を作り直す

### Q7 `observe().apply(Point2d(0, 0))` が `(0.2, -0.1)` を返した。検出対象は画像中心から見てどちらにどれだけずれているか。単位も。
- 要点: 右に 0.2、上に 0.1（+y が画像の下向きなので -0.1 は上）
- 要点: 単位は mm（カメラ mm 空間）

### Q8 `setup_board_calibration` の中で、「カメラ回転角の計測」と「Board 変換の計測」はどちらを先に行うか。そうする理由も。
- 要点: カメラ回転角の計測が先
- 要点: Board 変換の計測で使う位置補正（`XYPositionAdjustor`）が、回転角の計測結果 `offset_transform` を使うため

### Q9 `setup_board_calibration` を呼ぶ処理を新しく書く。途中で例外が出ても装置を安全な姿勢で終えるには、呼び出し側で何をするか。
- 要点: `machine_session` の中で呼ぶ
- 要点: `setup_board_calibration` 自体は終了時の駐機をしない（`machine_session` は例外時も駐機する）

### Q10 `RegionAlignmentSession.align()` が照合に失敗したとき、呼び出し側には何が返るか。`setup_board_calibration` の中で基準点マーカーを検出できなかったときとの違いも答えよ。
- 要点: `align()` は例外を出さず None を返す
- 要点: `setup_board_calibration` の検出失敗は例外（`CircleDetectionError`）で伝わる

### Q11 `BoardAlignment(results=<pad 中心照合の結果>, fallback_results=<領域照合の結果>)` で、どの pad 中心照合の結果にも覆われない点に `correction_for` を呼んだ。補正はどの結果から、どう選ばれるか。
- 要点: fallback_results（領域照合の結果）から選び直す
- 要点: その中に点を覆う領域があれば、変位が他と最も揃うものを選ぶ
- 要点: 覆う領域が無ければ、領域中心が点に最も近いものを選ぶ

## 保留問題
### H1 `correction_for` が返した補正は、どの座標系のどの点に適用するか。
- 要点: 機械座標の点 `board_transform.apply(board_point)` に適用する
- 要点: 補正は純並進

### H2 `XYPositionAdjustor.adjust()` が正常に返した位置と、その時点のステージの現在位置はどういう関係か。収束しなかったときはどうなるか。
- 要点: 差は tolerance 未満（収束した回は移動しない）
- 要点: 最大反復回数内に収束しなければ RuntimeError

### H3 `ManualStepper.home(..., direction=HomingDirection.BACKWARD)` を指定すると、ステッパーは逆向きに動くか。
- 要点: 動く向きは変わらない（向きは目標位置 `distance` で決まる）
- 要点: BACKWARD は停止条件を指定する（反応が解けたら止まる、STOP_ON_ENDSTOP=-1 まで答えれば満点。問いが BACKWARD の中身を直接問わないため必須にしない）

## ラウンド記録
### R0（初稿）
- 対象全体: 4639 → 4828 行（+4%）。生徒に読ませる 7 ファイル: 1366 → 1473 行（aligner.py は行数制限のため外した）
- 矛盾を直した箇所
    - `hal/camera.py` `create_camera`: backend の既定を "usb" と書いていたが実物は "csi"
    - `hal/klipper.py` Example: 存在しない `klipper.wait_for_move()` を使っていた
    - `hal/manual_stepper.py`: `move`/`rotate`/`home` の位置引数を「移動距離」と書いていたが、Klipper の `MOVE=` は絶対位置
    - `hal/manual_stepper.py` `HomingDirection`: 「方向」と書いていたが、実体は STOP_ON_ENDSTOP の停止条件
    - `posctrl/offset.py` `measure`: 手順 5 が `(o1 - o2)` だったが実装は `o2 - o1`
- 欠落を足した箇所: HAL は G-code を生成するだけ（klipper/stage/manual_stepper/air_pump）、send_gcode の完了の意味と例外、get_config のキャッシュ、relative 移動の基準、座標系一覧（posctrl/__init__）、Offset の符号、match の None 条件、adjust/measure/setup の Raises、BoardAlignment の選択規則、AlignmentRegion/RegionAlignment の属性、plan_alignment_regions の戻り値と例外、CheckerboardCalibrator の引数と z_position
- docformatter（pre-commit と同じ引数）を scratchpad のコピーに当てて差分ゼロを確認。要約行を 1 行に収めた

### R1（生徒に読ませる 7 ファイル 1473 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1 | 正解 | - | - |
| Q2 | 正解 | - | - |
| Q3 | 正解 | - | - |
| Q4 | 正解 | - | -（「完了していない」は「限らない」の言い過ぎだが、M400 で待つ行動は正しいので可） |
| Q5 | 正解 | - | - |
| Q6 | 正解 | - | - |
| Q7 | 正解 | - | - |
| Q8 | 正解 | - | -（根拠は setup.py の配線順。offset_transform を XYPositionAdjustor へ渡す箇所が支える） |
| Q9 | 正解 | - | - |
| Q10 | 正解 | - | - |
| Q11 | 正解 | - | - |
- 11/11。読みにくかった箇所の指摘なし。文書は変更しない。次は保留問題 H1〜H3 で汎化を確認する

### 保留問題（汎化確認、1473 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 正解 | 問題不良（軽微） | 要点の 2 つ目は問いが直接聞いていない内容だった。必須から外した。文書は変えない |
- 3/3。文書の改稿なしで完了
