# はんだ塗布への運転時流量キャリブレーション組み込み

ブランチ `feature/2026-09-11/paste-solder-flow-calibration`。`solo-dev-cycle` + `code-reviewer`。

## 要求（ユーザー）

1. 現在の校正データを使い、はんだ塗布ジョブで流量キャリブレーションする
2. パージ点とは別に**流量キャリブレーション点**を設定可能に
3. 点の pre/post 画像サイズ 既定 2.0 mm 角・塗布量 既定 0.2 uL を machine.toml へ
4. 使う校正ファイルも machine.toml へ
5. これらははんだ塗布ページからも設定できること

## ユーザー確認で確定した 2 点

- **測定は複数点（既定 3）**。1 点だと点ごと残差 std 9〜11 %（うち約半分は吐出そのものの
  ばらつき）がそのまま補正値に乗り、補正でかえってずれる
- **点が未設定なら塗らない**（補正しない）。加えて**点数は 0 まで下げられる**こと（0 = 無効）

## 設計

### machine.toml `[paste_dispenser.flow_calibration]`

| キー | 既定 | 意味 |
| --- | --- | --- |
| `calibration_file` | `""` | 校正ファイル名（空 = 無効） |
| `amount_ul` | 0.2 | 1 点あたりの指令塗布量 |
| `crop_size_mm` | 2.0 | pre/post 画像の一辺 |
| `point_count` | 3 | 測定点数（0 = 無効） |
| `point_pitch_mm` | 3.0 | +X 方向の点間隔 |

`point_pitch_mm > crop_size_mm` を必須にする。crop は点を中心に ±crop/2 なので、
隣の点がこれより近いと隣のドットが crop に写り込んで最大連結成分が壊れる。

### 被覆域の下端を採用境界から外す（既定方針の実装）

`CubicVolumeModel.reliable_diameter_min_mm` = `min + 0.35 * (max - min)`。
実測（docs/paste-volume-diameter-calibration.md）で 2 校正の食い違いは 0.65 mm で 58 %、
0.75 mm で 21 %、0.85 mm で 9 %。下側 35 % を落とすと下限が 0.79〜0.83 mm になり、
食い違いが 10 % 程度まで下がる。既定の 0.2 uL は直径 ≈1.0 mm なので十分内側。
`DiameterVolumeEstimator(..., reliable_range_only=True)` で運転時だけ内側を使う
（校正の生成・検証は従来どおり被覆域全体）。

### 補正

g = Σ V_est / Σ V_cmd、`rotations_per_ul_new = old / g`、1/3〜3 倍で clamp。
採用 0 件なら補正しない。実行内だけの補正で machine.toml へは書かない。

### モジュール

- `pcbasm/config.py` — `FlowCalibration`（`PasteDispenser.flow_calibration`）
- `pcbasm/pasting/paste_volume/model.py` — `RELIABLE_RANGE_MARGIN` / `covers_reliably`
- `pcbasm/pasting/paste_volume/estimator.py` — `reliable_range_only`
- `pcbasm/pasting/paste_volume/runtime.py`（新） — 点配置の計画と補正の算出（純関数）
- `pcbasm/pasting/capture.py`（新） — `dataset/capture.py::DatasetCapturer` を
  `PointCapturer` として一般化して移設。dataset 収集も運転時も同じ撮影経路を使う
- `pcbasm/pasting/session.py` — `camera_point_target` に位置合わせ補正を渡せるように
- `pcbasm/pasting/applicator.py` — `adopt_rotations_per_ul`（HAL を開いたまま係数だけ差替）
- `pcbasm/pasting/workflow.py` — `PasteTargets.flow_calibration`
- `pcbasm/pasting/settings.py` / `persist.py` — `flow_calibration_point`
- `web/api/jobs/pasting/flow_calibration.py`（新） — 撮影 → 推定 → 補正の実行
- `web/api/routers/pasting.py` — `PATCH /api/pasting/pad-config/flow-calibration`
- `web/api/config_store.py` / `web/ui/layout.py` / `pages.py` / `paste_solder.html` — ページ設定

### 却下した案

- **applicator を作り直して新係数を反映**（`flowcalib/procedure.adopt` と同じ手）。
  AirPump OFF→ON の圧力変動が入る。補正したいものそのものを乱すので、HAL を開いたまま
  係数だけ差し替える
- **crop_size_mm / 点数を job の ParamSpec にも出す**。`initial_purge_ul` と同じく
  machine 設定の即保存フォームに一本化する（二重の入口を作らない）
- **補正値を machine.toml へ書き戻す**。要件書は「残りのパッドへ適用する」と定めており、
  実行内に閉じる

## 追加要求（実装中にユーザーから）

**「ついでに計測時のラベルに日付時刻を追加するようにしてください」**

`_default_label` はペースト・ノズル径・塗布高さだけだったので、同条件で採り直した
校正が同じ label になり WebUI の選択肢で見分けられなかった（実際に 2 件とも
`S3X70-E150DN / n0.30 / h0.20`）。`created_at` を地方時で足した。

保存名はラベルから畳むので、自動命名だけ `auto_calibration_path`（時刻を足さない）へ
分けた。従来の `calibration_path` は運転者が入力した名前用で、時刻を足す挙動のまま。

## 自己レビューで直した点

- `calibration_file` をジョブ関数の引数から `FlowCalibrationPlan` の属性へ畳んだ。
  plan だけで「どこに何をどう塗って何で推定するか」が完結する
- `condition_mismatch` を `CalibrationConditions.mismatches` へ移した。運転時は
  `DatasetSession` を持たないので、値ベースの比較を共有しないと文言が二重化する
- settings.js の空欄は「未入力なので保存しない」だったため、校正ファイルの
  `<select>` で「補正しない」を選んでも何も起きなかった。`data-allow-empty` を
  足して空文字を明示的に送る

## 検証

`make format` / `make type`（0 errors）/ `make test-no-hardware`。
実機確認はユーザー。`view_count` 相当の実測（測定点 3 点での総体積誤差）が残課題。

---

## 追加要求（2026-09-11、実機で触った後のフィードバック）

ユーザーの指摘 4 件。

1. **測定位置を 1 点ずつ設定できるようにする** — 起点 + `point_count` + `point_pitch_mm` の
    自動配置をやめ、基板ごとに座標の**並び**を持つ
2. **パージ位置が未設定だと測定位置が表示されない**。自動解決したパージ位置も描く
3. **流量キャリブレーション点には置けない範囲がある**ので点線の四角で囲って見せる
4. **設定がページ上部（測定位置）と下部（machine 設定）に割れていて分かりにくい**。
    上部へ統合し、増えた項目が収まるようレイアウトを組み直す

### 段階 1（計画）の裁定

- **`FlowCalibration.point_count` / `point_pitch_mm` を削除**する。点数は基板側の
    測定位置の個数そのものになるので machine.toml に持つ意味が消える。
    `enabled` は `bool(calibration_file)` だけになる
- **`PasteSettingsModel.flow_calibration_point` → `flow_calibration_points: tuple[Point2d, ...]`**。
    永続 key も `flow_calibration_points`（`[[x, y], ...]`）。0 個 = 補正しない
- **点線の四角は crop 領域そのもの**（一辺 `crop_size_mm`、点が中心）。
    「置けない範囲」の実体は *隣の点の crop に写り込む* ことなので、
    **crop の四角どうしが重ならないこと**を規則にする。
    これは旧 `point_pitch_mm > crop_size_mm` と厳密に同値で、図にすればそのまま読める
- **重なりの判定は保存時ではなく計画時**（`plan_flow_calibration`）に置く。
    レビュー must-fix 3 と同じ理由で、crop 寸法を先に変えただけで保存が 400 になるのを避ける。
    PATCH が撥ねるのは「有限座標か」「基板外形の内側か」だけ
- **`build_initial_purge` の 400 を廃止**する。パージ設定が不正だと pad-config 全体が
    400 になり、pad editor ごと死んで測定位置も出なくなる（要求 2 の主因）。
    流量キャリブレーション側と同じく `error` をレスポンスに載せる
- **マーカーは plan ではなく保存済みの座標から描く**。従来は校正ファイル未設定だと
    `points` が `None` になり、設定した測定位置が図に出なかった
- **レイアウトは pad editor のツールバーへ 1 箱に統合**し、横 1 本のストリップにする。
    pad panel に背の高いフォームを置くと基板 SVG が viewport から外れて E2E が落ちた
    （却下済みの案）ので、高さを増やさない形にする

### 却下した案

- **PATCH で重なりを 400 にする** → crop 寸法を先に変えた瞬間に保存不能になる
- **点線の四角を「置ける領域」（外形の内側）にする** → 外形は既に実線で描いてある
- **測定位置を machine.toml に持たせる** → 基板ごとに違うので基板設定が正しい置き場所

### 段階 4（自己レビュー）で判明したこと

- **`build_initial_purge` の 400 が「測定位置が出ない」主因**だった。保存済みのパージ座標が
    基板外に出ると pad-config が 400 になり、pad editor 全体が「pad 設定を取得できません」で
    止まる。座標を直す手段まで失う。流量キャリブレーションと同じく `error` に載せて返す形へ
- **マーカーは plan ではなく保存済み座標から描く**。従来は校正ファイル未設定だと
    `points` が `None` になり、設定した測定位置が図に出なかった
- **レイアウトは高さが全部**。E2E `test_pad_svg_contains_visible_polygons` は
    「最初の pad が 720px の viewport に収まる」を要求する。元々の余裕は約 38px しか無く、
    上部へ箱を足すだけで落ちる。実測して次の 3 つで約 190px 削った:
    1. 流量キャリブレーションの箱をラベル横置きの横帯にする（196 → 117 px）
    2. 初回パージのツール群も横帯にし、`grid-column: 1 / 3` で「設定を書き出し」と同じ行へ
    3. auto しきい値フォームも横帯にする（64 → 32 px、枠は付けない）
    さらに `.pad-viewer { max-height: min(40rem, 55vh) }` で、画面が低いときは基板図自体を
    縮める。最終的に pad 下端 638px（余裕 82px、変更前より広い）
- **docformatter が日本語 docstring を 2 通りに壊した**。1 つは従来どおりの「。 」型、
    もう 1 つは**全角 1 文字を行跨ぎで分断してファイルを非 UTF-8 にする**型
    （`理由` → `理 由`、`校` → 不正バイト列）。`make format` が
    「stream did not contain valid UTF-8」で落ちるのがその合図。
    対処は同じで**1 文 1 段落・要約行を短くする**
