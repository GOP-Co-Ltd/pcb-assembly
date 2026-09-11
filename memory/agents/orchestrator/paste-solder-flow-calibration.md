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
