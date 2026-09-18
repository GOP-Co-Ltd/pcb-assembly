# 待ち時間・検出サンプリングの設定化と公開インターフェース整備

## 要求（ユーザー）

- 要所の settle time などを**設定ファイルから制御できる**ようにする。種類を列挙し、合成できる部分は合成する
- そのうえで**公開すべきインターフェース**を整える
- `OffsetObserver` のサンプル数 30 は多すぎるのではないか。設定可能にし、適切な既定値を与える
- `settle.move_sec` は 0.5 に統一してよい（ユーザー確認済み）

タクトタイムのフェーズ別計測（`memory/agents/implementation-planner/paste-tact-phases.md` の MR-B 以降）は
別タスク。本ブランチは MR-A 相当 + 検出サンプリングの設定化に絞る。

## 現状の問題（grep で確認）

- `settle_time` は 8 クラスのコンストラクタ引数だが、**構築側から渡している箇所が 1 つも無い**。
    既定値（0.5 / 1.0 / 0.0）が事実上の唯一の値で、機体ごとに変えられない
- うち 2 箇所はホスト側の `time.sleep`（`posctrl/setup.py:252` は名前すら無いハードコード、
    `pasting/toolhead_offset.py:480`）。残りは G4 dwell で、待ちの実装が 2 通りある
- `OffsetObserver` は 1 観測あたり `sample_count=30` フレームを撮る。カメラ 30 fps なら 1 観測 1 秒。
    構築側 2 箇所とも `sample_count` を渡していない

## 設計

### `[settle]`（新設・省略可）

```toml
[settle]
move_sec = 0.5   # ステージ移動後、撮影・計測に入るまでの静定待ち [sec]
probe_sec = 0.0  # PROBE 実行後の待ち [sec]
```

- A 群（移動後の静定）8 箇所を `move_sec` 1 個へ合成。すべて「ステージが止まってから像・機構が
    落ち着くまで」という同一現象で、8 箇所とも未調整の既定値。軸差で分ける根拠が現状に無いので
    先回りしない（必要になったら `z_move_sec` を足す）
- `probe_sec` は PROBE 実行後で別現象なので分ける。現状 0.0 = 無効
- `FlowCalibration.settle_seconds`（ペーストが広がるのを待つ）は**移さない**。工程そのものであり、
    既に設定 UI と塗布ページのフォームに載っている
- `prime_extra_delay` は吐出量に効くので合成しない
- `[tact]` には入れない。`[tact]` は「見積り専用で装置の動作に効かない」ことが契約

### `[detection]`（新設・省略可）

```toml
[detection]
sample_count = 10         # 1 観測で撮るフレーム数
minimum_sample_count = 5  # 有効検出がこれ未満なら観測失敗
```

- 30 → 10。平均の標準偏差は √3 ≒ 1.7 倍になるが、フレーム毎のばらつきが 0.02 mm 程度なら
    平均のばらつきは 0.006 mm 程度で、許容誤差（0.1 mm 前後）に対して十分小さい。撮影時間は 1/3
- `minimum_sample_count` の既定は 1 → 5。1 は「10 フレーム中 1 枚でも写れば採用」で品質ゲートとして
    機能していない。`toolhead_offset` は既に 5 を渡しており、それに揃える
- **実機確認が要る**: 基準点の検出が渋い機体では 10 中 5 に届かず失敗し得る。その場合は
    `minimum_sample_count` を下げるか `sample_count` を戻す

### 公開インターフェース

引数は float / int のまま（設定型を末端へ配らない）で、**既定値を外して必須化**する。既定値を残すと
「また誰も渡さない」が再発し、渡し忘れを `make type` で検出できない。単位を名前に入れる。

| クラス | 変更 |
| ------ | ---- |
| `XYPositionAdjustor` | `settle_time: float = 0.5` → `settle_sec: float`（必須） |
| `RegionAligner` | 同上 |
| `BoardTransformMeasurer` | 同上 |
| `OffsetTransformMeasurer` | 同上 |
| `ProbeExecutor` | `settle_time: float = 0.0` → `settle_sec: float`（必須） |
| `HeightPlaneMeasurer` | `move_settle_time: float = 0.5` → `settle_sec: float`（必須） |
| `PointCapturer` | `settle_time` 引数を削除し `session.machine.settle` から内部解決 |
| `OffsetObserver` | `sample_count: int = 30` / `minimum_sample_count: int = 1` → 必須 |

構築側（`posctrl/setup.py` / `posctrl/alignment.py` / `pasting/session.py` /
`pasting/toolhead_offset.py`）はすべて既に `Machine` を持っているので、`pcbasm` が `web` を
import しない制約は自然に守られる。

ホスト側 `time.sleep` 2 箇所は `GCode.wait()` へ寄せ、待ちの実装を 1 通りにする。

## 計測との関係

`[settle]` の待ちは G4 dwell なので、ホスト側のストップウォッチでは移動時間と分離できない。
フェーズ別計測（別タスク）では「装置稼働」に入る。静定待ちの合計は
`move_sec × 移動回数 + probe_sec × probe 点数` の**理論値**として出す。

## 段階

- **2 テスト**: `[settle]` / `[detection]` の既定・読み込み・検証、WebUI 設定フォームの露出
- **3 実装**: 下記
- **4 自己レビュー + code-reviewer**
- **5 ドキュメント**: `data/config-templates/README.md` に運用値の節

## 実装の実際

- 8 クラスの引数を必須化した結果、**渡し忘れが `make type` で 23 件挙がった**（すべてテスト側）。
    既定値を外す設計の狙いどおりに効いている
- `XYPositionAdjustor` / `BoardTransformMeasurer` / `OffsetTransformMeasurer` は
    既定値付き引数の後ろに必須引数を置けないため `settle_sec` を keyword-only にした
- `PointCapturer` は `session.machine.settle.move_sec` から内部解決（引数を落とした）。
    構築側 3 箇所（flow_calibration / purge_check / paste_volume_calibration）は無変更で済む
- `toolhead_offset` の `_DETECTION_MIN_FRAME_DETECTIONS = 5` は `[detection]` に置き換えたので削除
- ホスト `time.sleep` 2 箇所を `GCode.wait` へ寄せ、`toolhead_offset.py` の `import time` が不要になった
- WebUI 設定フォームには `settle` / `detection` セクションとして出る（`SECTION_LABELS` に追加）

## 実機で確認が要る（ユーザーへ申し送り）

1. `settle.move_sec = 0.5` への統一。基準点移動と `toolhead_offset` はこれまで 1.0 だったので**短くなる**。
    最初の円検出がぶれないか
2. `detection.minimum_sample_count = 5`（旧既定 1）。10 枚中 5 枚で検出できない機体では
    観測が失敗する。渋ければ下げる
3. `detection.sample_count = 10`（旧 30）。位置合わせの再現性が落ちていないか

## code-reviewer 1 巡目（request-changes）への対応

| 指摘 | 内容 | 対応 |
| ---- | ---- | ---- |
| M1 | `make format` が通らない（`data/config-templates/README.md` の新設テーブル未整形） | 修正。**整形を走らせた後に同ファイルを編集していた**のが原因で、pass と誤報告した |
| M2 | 「machine.toml の値が G4 として届く」テストが無く、`move_sec` と `probe_sec` を**取り違えても全テストが通る** | `TestSettleWiring` を追加（撮影 = P800 / プローブ = P300 / 高さ計測 = 両方）。必須引数化は「渡し忘れ」しか守らないという指摘は正しい |
| S1 | docstring の Example が必須引数を欠き、そのままでは `TypeError` | 3 ファイルを修正 |
| S2 | WebUI から `sample_count < minimum_sample_count` を保存でき、後で `Machine.detection` が読めなくなる | `_validate_detection_counts` を追加。片方だけ編集できるので既存 TOML とマージしてから検証する |
| S3 | 正整数検証の実装が 4 箇所に散った | config_store の 2 ブロックを既存 set へ統合 |
| S4 | 品質ゲートを 5 倍厳しくしたのに基準点の観測は `max_attempts=1` のままで、塗布痕側（3 回）と非対称 | 基準点側にも再取得を持たせ、定数を `posctrl` の公開定数（`DETECTION_MAX_ATTEMPTS` / `DETECTION_RETRY_SEC`）へ寄せて両者で共有 |
| S5 | 30 → 10 の根拠（フレーム毎のばらつき）が実測として残らない | 観測成功時に有効検出数と標準偏差を INFO ログへ出す |
| S6 | `[detection]` が基準円と塗布痕という性質の違う 2 つに効くことが書かれていない | docstring と README に明記 |

nit は「TOML 経由では cattrs が 1.5 を 1 に丸めるので float は弾かれない」等。実害が無く、
設定 UI 側は int 型で受けるので据え置いた。
