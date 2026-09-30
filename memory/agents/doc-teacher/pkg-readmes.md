# pcbasm パッケージ README 群 理解度テスト

## 対象
- ファイル: src/pcbasm/{geometry,hal,pasting,pnp,posctrl,vision}/README.md
- 読者と用途: 開発者が各パッケージの責務・主要モジュール・入口を把握し、変更をどのファイルに入れるか判断する。

## 評価用問題
### Q1 1 枚のカメラ画像から基準点の円を検出する新しい検出器を追加したい。どのパッケージのどのファイルに入れるか。
- 要点: `pcbasm.vision` の `detection.py`（`CircleDetector` などと同じ場所）

### Q2 `XYZStage.move(x=10, y=20)` を呼んだだけでステージは動くか。動かすには何が必要か。
- 要点: 動かない（`GCode` を返すだけ）
- 要点: 返った `GCode` を `Klipper.send_gcode()` で送る

### Q3 ポリゴンの最小回転外接矩形の計算を直したい（塗布の知識は含まない）。どのファイルを編集するか。
- 要点: `pcbasm/geometry/polygon.py`（`oriented_bbox`）

### Q4 `pcbasm.pasting` の applicator を使うコードで `from pcbasm.pasting import PasteApplicator` と書いてよいか。
- 要点: 書けない／書かない（`pcbasm.pasting` は re-export しない）
- 要点: `from pcbasm.pasting.applicator import PasteApplicator` と直接 import する

### Q5 銅箔位置合わせで、pad ごとに採用する領域補正の選び方（同点時の優先順位など）を変えたい。どのファイルのどのクラスか。
- 要点: `pcbasm/posctrl/alignment.py` の `BoardAlignment`

### Q6 小さい pad に対する pad ごとの逐次位置合わせを無効にしたい。何をどう設定するか。
- 要点: machine 設定 `paste_dispenser.pad_align.refine_max_short_side`（`[paste_dispenser.pad_align]` の `refine_max_short_side`）
- 要点: 0 にする

### Q7 Pick and Place の実装で基板の位置合わせが必要になった。位置合わせ処理を `pcbasm/pnp/` に新しく書くべきか。
- 要点: 書かない（複製しない）
- 要点: `pcbasm.posctrl` を使う

### Q8 ステージを動かしながら撮影を繰り返し、ずれが収束するまで計測する処理を `pcbasm.vision` に追加してよいか。理由と正しい置き場所も。
- 要点: 追加しない
- 要点: vision はハードウェアに触らない／`hal` を import しない（画像だけで完結する処理の置き場）
- 要点: `posctrl` か `pasting` に置く

### Q9 `pcbasm.pasting` に新しい検証関数を書く。入力が不正なときと正常なときに、それぞれどう結果を伝えるか。不正時に例外を投げてよいかも答えよ。
- 要点: 不正ならエラー文（`str`）を返す
- 要点: 正常なら `None` を返す
- 要点: 例外は投げない

### Q10 `Compose([Shift(...), Rotation(...)])` を点に適用すると、Shift と Rotation のどちらが先に適用されるか。
- 要点: `Shift` が先（リストの先頭から順に適用）

### Q11（H3 から移動） 塗布前後 2 枚の画像から「塗布で暗くなった量」を作る処理を直したい。どのファイルか。
- 要点: `pcbasm/vision/dot.py`（`darkening`）

## 保留問題
### H1 新しい Klipper 制御デバイス（例: 真空バルブ）の HAL クラスを `pcbasm/hal/` に追加する。コンストラクタは何を受け取り、動作メソッドは何を返す形にするか。
- 要点: `ReadonlyKlipper` を受け取る
- 要点: 機械を動かさず `GCode` を返す（送信は呼び出し側）

### H2 矩形のビンパッキングを使うとき `from pcbasm.geometry import pack_rects` と書けるか。
- 要点: 書けない（`packing.py` は re-export されていない）
- 要点: `from pcbasm.geometry.packing import pack_rects`

## ラウンド記録
### R0（執筆: 改稿前 129 行 → 217 行）
- geometry 5→31、hal 5→34、pnp 3→6、vision 9→25: 薄すぎたため構成表・置き場所の判断を追加（依頼で許可済み）
- posctrl 37→51: モジュール表を追加、pad_align 設定の所在を明記
- pasting 70→70: 検証規約行が `str | None` の `|` で表崩れしていたのを修正（矛盾）、dataset に `pending` を追記
- vision の旧記述「座標変換」は実態なし（geometry の担当）→ 削除

### R1（217 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1-Q7, Q10 | 正解 | - | - |
| Q8 | 正解 | - | 理由は「機械を動かしながら撮り直す手順だから」。文書の置き場所基準を正しく引いているため正解とした |
| Q9 | 不正解 | 問題不良 | 要点「例外は投げない」が問題文の「何を返すか」の範囲外だった。例外の可否を明示的に問う文へ直し、R2 で再出題 |
- 生徒の「読みにくかった箇所」はなし。文書は変えていない

### R2（217 行、pasting の 1 行だけ修正）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1-Q10 | 正解 | - | - |
- 全問正解。保留問題 H1-H3 を出題
- オーケストレーターの依頼で dataset の記述を `dataset/__init__.py` と突き合わせた。「撮影は `capture.py`」は実在しない `dataset/capture.py` と読める曖昧さがあった → 「dataset 外の `pasting/capture.py`」と明記。`recorder` にも役割を付記

### 汎化確認（H1-H3）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 不正解 | 曖昧 | pasting 表の `detect` が「pre/post 差分 → Otsu」と書かれ、差分処理の実体が paste_volume にあると読めた。vision/dot.py 行も「共用」で主従が不明。detect 行に「差分・2 値化の実装は vision.dot」、dot 行に `darkening` / `background_darkening` と「呼ばれる側」を明記。サブパッケージ行の名前がモジュールファイルを指すことも 1 文追記（生徒の指摘）。217 → 219 行 |
- H3 を評価用 Q11 へ移した。未使用の保留問題は残っていない

### R3（219 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1-Q11 | 正解 | - | - |
- 全問正解でループ完了。保留問題は H1・H2 のみ残存（汎化確認では正解済み）
