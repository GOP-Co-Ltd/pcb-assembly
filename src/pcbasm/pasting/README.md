# pasting

ペースト塗布のドメインロジック。計算・機械手順は本パッケージに置き、ユーザー対話・進捗・
中断・成果物保存は `web/api/jobs/pasting/` が担う（`pcbasm` は `web.*` を import しない）。

## 構成

| モジュール               | 役割                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `params.py`              | 塗布パラメータ 8 項目の単一ソース。`PasteParams` / `PasteParamsPatch` / `PASTE_PARAM_FIELDS`（UI 列順・ラベル・入力種別）/ `validate_param_values` / `DispenseSettings`                                                                                                                                                                                                                    |
| `settings.py`            | 基板別の階層 override（L0–L4）。`PasteSettingsModel` / `resolve_pad_settings` / `select_enabled_pads` / override 集計                                                                                                                                                                                                                                                                      |
| `persist.py`             | 基板別 override JSON の codec（schema v1、legacy 変換込み）                                                                                                                                                                                                                                                                                                                                |
| `fill_path.py`           | pad 1 枚の塗布経路生成。`FillPlan` / `FillPlan.build` / `FillPlan.for_pad`                                                                                                                                                                                                                                                                                                                 |
| `fill_sequence.py`       | 1 経路の G-code ビルダー `FillSequence`（prime → 吐出 → retract）                                                                                                                                                                                                                                                                                                                          |
| `applicator.py`          | ディスペンサー HAL の駆動 `PasteApplicator` / `build_applicator`。`apply` / `deposit_at` / `draw_line` / `load_rotations`                                                                                                                                                                                                                                                                  |
| `dispense.py`            | HAL 非依存の塗布実績集計 `DispenseSummary`（収集 schema が参照する。`applicator.py` から再公開）                                                                                                                                                                                                                                                                                           |
| `route.py`               | 有効 pad の塗布順路 `plan_paste_route`                                                                                                                                                                                                                                                                                                                                                     |
| `sweep.py`               | 掃引点列の等間隔分割 `sweep_schedule`（flowcalib と dataset が共用）                                                                                                                                                                                                                                                                                                                       |
| `initial_purge.py`       | 初回パージ位置（座標）の解決                                                                                                                                                                                                                                                                                                                                                               |
| `height.py` / `probe.py` | 銅箔上のプローブ計測と高さ面 `HeightPlaneMeasurer` / `plan_probe_points` / `ProbeExecutor`                                                                                                                                                                                                                                                                                                 |
| `capture.py`             | board 座標の点をカメラ中心へ置いて固定寸法で切り出す `PointCapturer`（dataset 収集と運転時キャリブレーションの共通経路）                                                                                                                                                                                                                                                                   |
| `session.py`             | 塗布ワークフローの HAL 配線 `PasteSession`（Board 計測結果 → 高さ計測・位置合わせ・pad 別変換・銅板の一括変換・applicator 構築）                                                                                                                                                                                                                                                           |
| `workflow.py`            | 装置非依存の前計画 `plan_paste_targets`                                                                                                                                                                                                                                                                                                                                                    |
| `alignment.py`           | 塗布向けの位置合わせ合成 `PasteCorrection` / `refine_pad`                                                                                                                                                                                                                                                                                                                                  |
| `toolhead_offset.py`     | カメラ–ノズル間オフセット計測 `ToolheadOffsetProcedure` / `ToolheadOffsetResult` / `ToolheadOffsetDiagnostics`                                                                                                                                                                                                                                                                             |
| `flowcalib/`             | 流量キャリブレーション。`params`（ジョブ既定値）/ `flow`（質量 → rotations_per_ul、レート・速度掃引の数理）/ `lines`（線配置と掃引計画）/ `procedure`（銅板・transform・applicator を束ねる機械手順）                                                                                                                                                                                      |
| `testboard/`             | テスト塗布基板の KiCad 生成。`config`（設定 DTO と検証）/ `catalog`（footprint 検索とパッド種解決）/ `layout`（パッド packing）/ `generator`（preview・`.kicad_pcb` 生成のファサード）                                                                                                                                                                                                     |
| `paste_volume/`          | 点塗布の円直径から塗布量を推定する校正。`detect`（pre/post 差分 → Otsu → 面積等価直径）/ `aggregate`（view 中央値）/ `model`（切片 0 固定の 3 次）/ `calibration`（校正ファイル schema v1）/ `estimator`（公開 prediction API）/ `fit`（1 session → 計測 → フィット → 診断）/ `evaluate`（校正 × session → 誤差レポート）/ `runtime`（運転時の測定点配置と `rotations_per_ul` 補正の算出） |
| `dataset/`               | ペースト塗布画像 dataset の収集（銅板のセル格子へ点塗布）。`plan`（セル格子・量スイープ・view・事前検証）/ `metadata`（metadata.json DTO・codec、schema v3）/ `writer` / `reader`（完成 session の列挙と読み出し）/ `recorder`。撮影は `capture.py`、切り出しは `pcbasm.vision.crop`                                                                                                       |

`__init__.py` は docstring のみで re-export しない。消費側はサブモジュールを直接 import する
（`import pcbasm.pasting` が cv2 / pcbnew を引き込まない契約を `tests/test_package.py` で固定）。
固定しているのは re-export しないことであって、サブモジュール自身が重い依存を持たないことではない。

塗布量推定は `paste_volume/` の円直径の 3 次近似ひとつ。Raspberry Pi の WebAPI
プロセスでそのまま動く（cv2 / numpy は base 依存）。

`dispense.py` は HAL 非依存の値だけを置く。収集 schema（`dataset/metadata.py`）を学習側から
読むとき `pcbasm.hal`（picamera2 を要求する）を引き込まないため、`DispenseSummary` を
`applicator.py` から分離してある。

## 依存方向

```
web/api/jobs/pasting/*  →  pasting.{session,workflow,alignment,flowcalib.procedure,dataset.*,toolhead_offset}
                              ↓
                       pasting.{applicator,fill_path,fill_sequence,params,settings,route,height}
                              ↓
                       pcbasm.{config,geometry,pcb,posctrl,vision,hal}
```

- 汎用の計算幾何は `pcbasm.geometry`、KiCad 汎用処理は `pcbasm.pcb`（`units` / `footprint`）に置く
- 永続 JSON（基板別 override / dataset metadata / テスト塗布基板 document）は `schema_version` で分岐し、
    旧版は純関数 `_migrate_vN` で新版 dict へ写してから structure する。形状を変えない限り版は上げない

## 規約

| 対象           | 規約                                                                                                                                                                                            |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 接頭辞         | `Paste*` = 塗布ドメイン名詞、`Dispense*` = 吐出ダイナミクス、`Fill*` = 経路と G-code、`Flow*` = 流量キャリブ                                                                                    |
| 単位           | μL は常に `_ul`（`amount_ul`, `volume_ul`）。密度は `density_mg_per_ul`（TOML キー `solder_paste_density` は config プロパティで橋渡し）。mm は幾何値では無記、JSON DTO と KiCad 境界だけ `_mm` |
| 検証           | \`validate\_\<対象>() -> str                                                                                                                                                                    |
| 値オブジェクト | `attrs.frozen` + tuple。新規 ABC / Protocol は作らない                                                                                                                                          |
| ログ           | `logging.getLogger(get_class_module_path(cls))`。複数回機械を動かすメソッドだけ `checkpoint` コールバックを受ける                                                                               |

位置合わせは `posctrl` を利用する。
