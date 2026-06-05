# fill ロジック再設計 — spec-test-author メモ（Phase 1）

唯一の共有契約: `memory/agents/implementation-planner/fill-redesign.md`
対象: `tests/pcbasm/pasting/test_fill_path.py`（全面改訂）、
`tests/pcbasm/pasting/test_applicator.py`（追従）。`src/` は一切触っていない。

## 結論（状態）

- `make format` / `make type` / `make test-no-hardware` すべて green。
- `plan-implementer` が並列で `src/pcbasm/pasting/fill_path.py` を新 IF に置換済み
  （`build_paste_fill_path(...) -> list[list[Point2d]]`・牛耕式ジグザグ・成分分割）。
  そのため spec first だが fill 系テストは**実装が追いついており全 pass**。
  単独で確認した範囲では実装は契約メモ §1〜§4 を満たしている。

## 追加 / 改訂した test クラス

### test_fill_path.py（全面改訂・旧 TestSpiralBranch / TestLinearBranch 削除）

| クラス | 検証内容 | しきい値導出 |
|---|---|---|
| `TestFallbackHierarchy` | 大矩形→面（多点）/ 細長→線（1ポリライン2点）/ 極小→点（1ポリライン1点・空でない） | 形状×ノズル径を parametrize。`buffer(-w/2)` の空判定で段を前提化 |
| `TestAreaFill` | 外周ポリライン存在（点数>4）/ ジグザグが最長軸方向 / 被覆率>0.6 | bead 幅=nozzle 径・行間=nozzle 径から導出。被覆は path.buffer(w/2)∩polygon/area |
| `TestSegmentContainment`（最重要・§3） | 各隣接セグメント LineString が `polygon.buffer(EPS).covers(...)`。割れる形は外側≥2 かつ成分跨ぎ横断セグメント無し | EPS=1e-6 定数。形状 parametrize |
| `TestCoverage` | overlap↑ で行間隔=w*(1-overlap) 縮小（中央 gap が期待値近傍 rel=0.5）・行数単調増 | overlap∈{0,0.25,0.5}。固定形状・ノズル径 |
| `TestExteriorMargin` | margin>0 で全頂点が `buffer(-margin)` 内・外周距離≥margin-EPS・margin>0 は margin=0 より外周から離れる | margin∈{0.1,0.3} parametrize |
| `TestInvalidInput`（§1 表） | nozzle≤0 / overlap∉[0,1) / margin<0 / bead_factor≤0 → ValueError（substring=引数名）。空/不正ポリゴン→`[]` | (引数,不正値) parametrize |
| `TestReturnType`（契約ピン） | `list[list[Point2d]]`・正常時 外側≥1・各内側≥1点・全要素 Point2d・有限座標 | 面/線/点の代表形状 parametrize |

### test_applicator.py（成分ループ追従・旧螺旋前提を削除）

| クラス | 検証内容 |
|---|---|
| `TestSingleComponentPad` | 単一成分→send_gcode 1回・塗布吐出量 = retraction + area*ul_per_mm2 |
| `TestMultiComponentPad` | 細首ダンベル（2成分）→send_gcode 2回・各 total = area*ul/N・総和 = area*ul（決定事項A）|
| `TestEmptyFallback` | 空/不正ポリゴン→warning & skip（send_gcode 0回）|
| `TestDispenseProtocol` | プライム+吐出 sync=False / リトラクション sync=True / context で enable/disable |
| `TestInitValidation` | retraction_accel_factor≤1.0 → ValueError |
| `TestCalibrate` | rotate_revolutions 委譲 + send_gcode 1回 |

## pin / しきい値導出の方針（重要）

- **EPS = 1e-6 定数**（モジュール先頭 `_EPS`）。§3 の covers 判定・外周距離判定に共用。
  ジグザグ端点が外周に乗るため微小 buffer で境界一致を拾う。
- **total_amount の検証経路**: applicator は `FillSequence` を直接公開しないため、
  `FillSequence.to_gcode` が発行する `dispenser.pushpull(amount, sync=False)` の
  `amount = retraction + extra_amount + total_amount`（既定 extra=0）から total を復元。
  `_dispense_amounts()` ヘルパーで sync=False の pushpull 量のみ抽出して検証。
- **成分分割を誘発する形状**: 契約メモ §3 は「L字・ダンベル」を例示するが、
  幾何的に **L字（くびれ無し）は buffer(-inset) で成分分割しない**（縮むだけ）。
  既存ダンベル（くびれ幅 1.2）も nozzle≥1.4 でしか割れない。
  → 成分分割テストには **細首ダンベル（くびれ幅 0.3）を水平/垂直 2 形状**自作し、
  既定ノズル径 0.34（inset 0.17 > 0.15）で 2 成分に割れることを前提に組んだ。
  `_split_dumbbell_h` / `_split_dumbbell_v`。test_applicator の `_DUMBBELL_NECK_03` も同じ。
  `_concave_dumbbell`（くびれ 1.2・割れない凹形）はセグメント内包の素材としてのみ使用。
- `@pytest.mark.api_contract` は **付与していない**。`pyproject.toml` の `--strict-markers`
  下で `api_contract` が未登録のため、付けると collection error になる。`tests/` のみ編集の
  原則を守り、`TestReturnType` を通常クラスとして契約をピン（契約メモ §5 spec 裁量の範囲）。
  → marker を使いたい場合は plan-implementer/docs-keeper が pyproject.toml に登録要。

## モック方針

- `Klipper` / `PasteDispenser` / `XYZStage` を `mocker.Mock()` で fake（既存 applicator
  テストの確立パターンを踏襲）。いずれも自前 HAL（`src/pcbasm/hal/`）なので testing-strategy 準拠。
- `Klipper` は厳密には ABC ではなく Moonraker REST 具象だが、プロジェクト所有 HAL のため fake 可。
- 3rd-party 表面（httpx / shapely 内部 / cv2 等）・内部 private 関数は一切モックしていない。
- 実 `Polygon`（shapely）を直接生成。fill_path は HW 非依存 unit、`@mark_hardware` 不要。

## 実装側への要求 / 未解決点

- 現時点で **実装側修正要求は無し**（全テスト pass・契約メモと整合）。
- 留意（plan-implementer / docs-keeper 向け）:
  - `api_contract` marker を将来使うなら `pyproject.toml [tool.pytest.ini_options] markers`
    への登録が必要（本 spec では未使用なので blocker ではない）。
  - 被覆率しきい 0.6 は「概ね均一被覆」の下限ガード。被覆率の精緻な最適化は
    本タスク外（計画 L100 の可視化は dev script 側）。実装が大幅に被覆を落とすと検出される。
  - overlap の行間隔検証は牛耕端の折り返し誤差を見込み `rel=0.5` と緩め。
    実装で line_spacing 式（w*(1-overlap)）から逸脱すれば中央 gap がしきいを外れる。
