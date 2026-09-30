# src/pcbasm/pasting/ docstring・コメント 理解度テスト

## 対象
- ファイル: `src/pcbasm/pasting/` 配下の全 .py（docstring・コメントのみ）
- 読者と用途: 塗布ロジックを保守する開発者が、塗布パス生成・校正・データセット・ジョブの流れと各公開 API の契約（単位・前提・戻り値）を理解し、正しく使い・修正できるようにする。
- 生徒に読ませるファイル（通常塗布ジョブの主経路に絞る。計 1574 行）:
    - `src/pcbasm/pasting/__init__.py`
    - `src/pcbasm/pasting/workflow.py`
    - `src/pcbasm/pasting/route.py`
    - `src/pcbasm/pasting/session.py`
    - `src/pcbasm/pasting/fill_path.py`
    - `src/pcbasm/pasting/applicator.py`
    - `src/pcbasm/pasting/paste_volume/runtime.py`

## 評価用問題
### Q1 通常塗布ジョブで、「初回パージ」と「運転時流量キャリブで `rotations_per_ul` を差し替える」のはどちらが先か。
- 要点: 初回パージが先（その後に運転時流量キャリブ → pad ごとの apply）

### Q2 基板上の pad ではない任意の点（例: 明示指定した初回パージ点）へ `deposit_at` するとき、`transform` に何を渡すか。複数の点に同じ変換を使い回してよいか。
- 要点: `session.point_transform(point, correction)`
- 要点: 使い回さない。補正はその点を覆う位置合わせ領域から内挿するので点ごとに組み直す

### Q3 新しい塗布ジョブで `PasteApplicator.apply` の `transform` に `session.board_to_machine` を渡してよいか。理由と、代わりに使うものを答えよ。
- 要点: よくない。位置合わせ補正も高さ面も含まない
- 要点: pad なら `session.pad_transform(pad, correction)` を使う

### Q4 `session.make_applicator()` で作った applicator で pad を塗り始めるまでに、呼び出し側がすることを順に答えよ。2 番目の操作を省くと何が起きるか。
- 要点: `with` に入ってディスペンサーを有効化する
- 要点: 最初の塗布の前に `retract()` を 1 回呼ぶ
- 要点: 省くと最初の 1 回だけ prime の分（リトラクション量）余計に出る

### Q5 `dispense_mode = "auto"`、ノズル径 0.4 mm、`auto_area_short_side_factor = 2.0`、`auto_line_aspect_ratio = 3.0` のとき、最小回転外接矩形が 0.6 mm × 2.4 mm の pad はどの方式で塗られるか。理由も答えよ。
- 要点: line（線塗布）
- 要点: 短辺 0.6 が 0.4×2.0=0.8 を超えないので area ではなく、長短比 4 が 3 を超えるので line
- （線塗布が成立しなければ dot になる、は加点不要・減点しない）

### Q6 面積 2.0 mm² の pad を `ul_per_mm2 = 0.05` で `apply` し、面塗布の連結成分が 2 つになった。各成分のポリラインに指令される塗布量は何 μL か。
- 要点: 0.05 μL（2.0×0.05=0.1 μL を成分数 2 で均等配分）

### Q7 ある pad に `line_direction = "outward"` を設定したが、線の向きが部品から外向きに揃わなかった。文書から考えられる原因を 2 つ答えよ。
- 要点: 実際の方式が line でなかった（area / dot では line_direction は無視）
- 要点: `line_reference`（部品位置）が無い（対応部品が無い）ので unconstrained 扱いになった

### Q8 運転時流量キャリブで 1 点 0.2 μL を 4 点塗った。1 点は推定不能（accepted=False）、残り 3 点の推定量の合計が 0.75 μL、補正前の `rotations_per_ul` は 10.0。補正後の係数と `clamped` の値を答えよ。
- 要点: ratio = 0.75 / (0.2×3) = 1.25（不採用点は指令側からも除く）
- 要点: 補正後 = 10.0 / 1.25 = 8.0
- 要点: clamped = False（1/3〜3 倍の範囲内）

### Q9 ペーストを替えたので `machine.toml` の `rotations_per_ul` そのものを測り直したい。`pcbasm.pasting.flowcalib` と `pcbasm.pasting.paste_volume.runtime` のどちらを使うか。理由も答えよ。
- 要点: `flowcalib`
- 要点: runtime は塗布ジョブ中のその場補正で `machine.toml` を書き換えない。flowcalib が線を引いて計量し machine.toml の値を決める事前校正

### Q10 `machine.toml` の運転時流量キャリブ設定で `calibration_file` が空のとき、`plan_paste_targets` はエラーを返すか。戻り値の中でどう表れるか。
- 要点: エラーにならない
- 要点: `PasteTargets.flow_calibration` が `None`（補正しない）

### Q11 `PasteTargets.params_for(pad)` が `None` を返すのはどんな pad か。その pad はどのパラメータで塗るべきか。
- 要点: 階層外（対応 Component が無い）pad
- 要点: machine 既定（`applicator.default_params`）で塗る

### Q12 `plan_paste_route` に、面積 0.45 mm² の pad 群 A と 1.0 mm² の pad 群 B（それぞれ同種類）を渡した。どちらの群から塗るか。群の中の順はどう決まるか。
- 要点: 面積の大きい B から
- 要点: 群の中は直前位置からの最近傍順（最初の群は `start`、既定 (0, 0) から）

## 保留問題
### H1 運転時流量キャリブの測定点を (10.0, 10.0) と (11.5, 10.5)、`crop_size_mm = 2.0` に設定した。ジョブの前計画（`plan_paste_targets`）の結果はどうなるか。理由も答えよ。
- 要点: `(None, 理由)` でエラー（ジョブは進まない）
- 要点: 両軸の距離（1.5 と 0.5）がどちらも crop 寸法 2.0 未満で撮影範囲が重なる

### H2 `draw_line` で既定の `max_fill_speed` を超える速度を試したい。どの引数をどう指定するか。吐出レート上限を外したいときは？
- 要点: `fill_speed` にその速度 [mm/sec] を渡す（None なら max_fill_speed）
- 要点: `rate_cap=math.inf` で cap 無効（None なら max_dispense_rate）

### H3 `dispense_mode = "area"` を指定した小さい pad で、インセット後の領域が空になった。`FillPlan.build` はどうなるか。
- 要点: area は成立しないので line を試す
- 要点: line も成立しなければ dot（代表点 1 点）

## ラウンド記録
### R0 執筆（生徒に読ませる 7 ファイル: 1478 → 1574 行）
- `__init__.py`: 通常塗布ジョブの流れ（1〜6）、flowcalib と paste_volume.runtime の区別、単位を追加
- `dataset/__init__.py`: 存在しない `dataset.capture` の記載を `pcbasm.pasting.capture` へ訂正（矛盾）、`reader` を追加（欠落）
- `flowcalib/__init__.py`: flowcalib 配下ではない testboard をサブモジュール一覧から外した
- `workflow.py`: モジュール概要に流量キャリブ、`plan_paste_targets` の Args/Returns、`disabled_count`・`params_for` の契約
- `route.py`: `PasteRouteStop` 属性、同面積時の並び、Args
- `session.py`: 変換の選び方（pad / 点 / 銅板）、`board_to_machine` の用途、`make_applicator` の前提
- `applicator.py`: with → retract → 塗布の順序と理由、`rotations_per_ul`、`apply` の空経路と引数
- `fill_path.py`: `for_pad` の line_direction の効く条件と Args
- `paste_volume/runtime.py`: 補正の算出式と向き
- 増加理由: 流れの概要と変換の選び方は既存文書に無く、読者の作業に必要（欠落）

### R1（1574 行、生徒に読ませるファイルの行数は変えていない）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q12 | 全問正解 | - | - |

- 生徒が読みにくかった箇所: なし
- 対象外の teacher からの指摘への対応: `params.py` の `PasteParams.paste_height` は「塗布面の Z 高さ」と書かれていて、機械座標の絶対 Z と読み違えやすかった（曖昧）。`config.py` の `paste_height` コメントと `resolve_paste_height` を実物で確認し、「基板表面からのノズル高さ（絶対 Z ではない）」に直した。`paste_height_mm` の docstring も同じ語にそろえた。`params.py` は生徒に読ませるファイルではないので、行数は変わらない
- 全問正解だったので、次は保留問題 H1〜H3 を出す

### 汎化確認（保留問題 H1〜H3、1574 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 正解 | - | - |

- H1 の解答は、内側の `plan_flow_calibration` の戻り値 `(None, 理由)` を答えた。`plan_paste_targets` はそのエラーをそのまま返すので（workflow.py）、要点を満たすと判定した
- 生徒が読みにくかった箇所: なし。改稿はしていない。このループは完了
