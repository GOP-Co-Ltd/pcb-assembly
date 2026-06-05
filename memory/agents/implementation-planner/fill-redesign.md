# fill ロジック再設計 — Phase 0 契約メモ（公開IF確定）

承認済み計画: `/home/gop/.claude/plans/fill-adaptive-parasol.md`（正典）
引き継ぎ: `/home/gop/pcb-assembly/memory/agents/fill-redesign-handoff.md`
このメモだけで `spec-test-author`（tests/ 専用）と `plan-implementer`（src/ 専用）が
**並列・disjoint** に作業できることがゴール。シグネチャは全て確定値。曖昧は残さない。

---

## 0. 用語・座標系

- `Point2d` は `pcbasm.geometry` から（`from pcbasm.geometry import Point2d`）。
  - `.x` / `.y` を持つ。`(a - b).norm` でユークリッド距離、`a + dir * t` で線形補間。
  - `.to3d(z)` で `Point3d` 化（applicator 側で使用、fill_path は 2D のみ）。
- ポリラインは `list[Point2d]`。成分は 1 ポリライン。戻り値は成分の集合 `list[list[Point2d]]`。
- 「成分（component）」= `polygon.buffer(-inset)` を連結成分分解した各 `Polygon`。
- `Polygon` / `LineString` / `MultiLineString` は shapely。

---

## 1. 公開 IF: `build_paste_fill_path`（`src/pcbasm/pasting/fill_path.py`）

### シグネチャ（確定・破壊的変更）

```python
def build_paste_fill_path(
    polygon: Polygon,
    nozzle_diameter: float,
    *,
    bead_width_factor: float = 1.0,
    overlap: float = 0.0,
    boundary_margin: float = 0.0,
) -> list[list[Point2d]]:
    ...
```

- 旧: `(polygon, nozzle_diameter) -> list[Point2d]`。
- 新: 戻り値は **成分別ポリラインのリスト**。
- 第3引数以降は **キーワード専用**（`*` 区切り）。デフォルトで旧呼び出し（位置2引数）と互換。

### 数式（計画の数式定義をそのまま実装）

```
w (bead_width) = nozzle_diameter * bead_width_factor
line_spacing   = w * (1 - overlap)
inset          = boundary_margin + w / 2     # 面塗布領域 = polygon.buffer(-inset)
end_inset      = boundary_margin + w / 2     # 線塗布の両端内側補正
```

`bead_width_factor=1, overlap=0, boundary_margin=0` のとき
`line_spacing = nozzle_diameter`, `inset = nozzle_diameter/2` ＝ 旧ヒューリスティクスと幾何整合。

### 制御フロー（フォールバック階層が構造として現れる）

```python
def build_paste_fill_path(polygon, nozzle_diameter, *, bead_width_factor=1.0,
                          overlap=0.0, boundary_margin=0.0):
    # --- バリデーション（早期 raise / 不正形状は [] 返却）---
    if nozzle_diameter <= 0:
        raise ValueError(... "nozzle_diameter" ...)
    if not (0.0 <= overlap < 1.0):
        raise ValueError(... "overlap" ...)
    if boundary_margin < 0:
        raise ValueError(... "boundary_margin" ...)
    if bead_width_factor <= 0:
        raise ValueError(... "bead_width_factor" ...)   # w=0/負を防ぐ（計画の3条件に加え追加）

    if polygon.is_empty or not polygon.is_valid:
        return []

    w = nozzle_diameter * bead_width_factor
    line_spacing = w * (1.0 - overlap)
    inset = boundary_margin + w / 2.0
    end_inset = boundary_margin + w / 2.0

    # --- 面 → 線 → 点 ---
    if paths := _area_fill(polygon, line_spacing, inset):
        return paths
    if line := _line_fill(polygon, end_inset):
        return [line]
    return [_dot_fill(polygon)]
```

### バリデーション契約（spec-test-author が substring 検証に使う）

`raise ValueError` のメッセージに **必ず含める substring**（部分一致で検証する）:

| 不正入力 | 条件 | メッセージに含める substring |
|---|---|---|
| `nozzle_diameter` | `<= 0` | `"nozzle_diameter"` |
| `overlap` | `< 0` または `>= 1` | `"overlap"` |
| `boundary_margin` | `< 0` | `"boundary_margin"` |
| `bead_width_factor` | `<= 0` | `"bead_width_factor"` |

- substring は **引数名（半角・スネークケースそのまま）** を含めること。日本語説明文を併記してよいが
  引数名の半角表記は必ず残す（例: `f"overlapは[0,1)である必要があります: {overlap}"`）。
- 不正形状（`polygon.is_empty` または `not polygon.is_valid`）→ `[]`（例外を投げない）。
- バリデーションの評価順序は `nozzle_diameter → overlap → boundary_margin → bead_width_factor → 形状`。
  spec は 1 観点 1 引数で単独不正値を与え、他は既定値とするので順序依存テストは書かない方針。

### 戻り値契約（TestReturnType / 公開API契約ピン）

- 常に `list[list[Point2d]]`。
- 不正形状時のみ外側リストが空 `[]`。それ以外は **必ず 1 つ以上のポリラインを含む**
  （点フォールバックがあるため空にならない）。
- 各内側ポリラインは **1 点以上**の `Point2d`。点フォールバックは `[[rep]]`（内側1点）。
- 線フォールバックは `[[start, end]]`（内側2点・1ポリライン）。
- すべての要素が `Point2d` 型であること。

---

## 2. private ヘルパー（責務・シグネチャ 1 行契約）

### 新規（plan-implementer が実装）

| ヘルパー | シグネチャ | 責務（入出力） |
|---|---|---|
| `_area_fill` | `(polygon: Polygon, line_spacing: float, inset: float) -> list[list[Point2d]]` | `polygon.buffer(-inset)` の各連結成分（`_offset_components(polygon, inset)`）に `_outline_and_zigzag` を適用し、空でないポリラインを集めて返す。成分が無ければ `[]`。 |
| `_outline_and_zigzag` | `(component: Polygon, line_spacing: float) -> list[Point2d]` | 1 連結成分の「外周トレース＋内部ジグザグ（牛耕式・最長軸方向スキャンライン）」を 1 ポリラインに結合して返す。点が作れなければ `[]`。 |
| `_line_fill` | `(polygon: Polygon, end_inset: float) -> list[Point2d]` | `_generate_linear_path(polygon, end_inset)` をそのまま委譲（流用ヘルパーの薄いラッパ。実体は最長軸中心線2点）。 |
| `_dot_fill` | `(polygon: Polygon) -> list[Point2d]` | `rep = polygon.representative_point()` から `[Point2d(rep.x, rep.y)]`（必ず1点）を返す。空を返さない。 |

`_outline_and_zigzag` 内部の最小実装方針（計画 L62-70 準拠・作り込み禁止）:
1. 外周: `_ring_coords(component)`（`component.exterior` のポリライン）。
2. ジグザグ: `component.minimum_rotated_rectangle` から最長軸方向を取り、最長軸に垂直な
   スキャンラインを `line_spacing` 間隔で生成。各スキャンライン∩`component`（`LineString`/
   `MultiLineString`）区間を最長軸座標でソートし、行ごとに方向を交互反転して牛耕式に繋ぐ。
3. 外周 → 内部ジグザグの順で 1 ポリラインに結合（結合は最近傍接続）。
- 凹成分でジグザグ区間が割れて横切りが生じるケースは、`TestSegmentContainment` の検出に基づき
  **出たケースだけ局所対処**（その行を外周経由で繋ぐ／当該区間を成分として更に分離）。
  先回りの一般化はしない（CLAUDE.md シンプルさ最優先）。

### 流用（既存をそのまま再利用・シグネチャ変更なし）

| ヘルパー | 現在地 | 用途 |
|---|---|---|
| `_offset_components` | `fill_path.py:217` | `(polygon, depth) -> list[Polygon]`。`buffer(-depth)` を連結成分分解。`_area_fill` が `depth=inset` で使用。 |
| `_ring_coords` | `fill_path.py:243` | `(polygon) -> list[Point2d]`。`_outline_and_zigzag` の外周トレースに使用。 |
| `_generate_linear_path` | `fill_path.py:162` | `(polygon, end_inset) -> list[Point2d]`。`_line_fill` が委譲。長軸≤`2*end_inset` で `[]`。`end_inset<0` で `ValueError`（公開側で既に弾くため通常到達しない）。 |

### 撤廃（plan-implementer が削除。spec 側は `TestSpiralBranch` を削除）

`_generate_spiral_path` / `_spiral_one_component` / `_walk_rings_outward` /
`_estimate_ring_spacing` / `_truncate_ring` / `_rotate_ring_to_nearest` /
`_connect_nearest` / `_sort_components_by_seed`（計 8 個）。

> 補足: `_line_fill` は `_generate_linear_path` の薄いラッパ。実装者判断で
> `_line_fill` を別関数にせず `_generate_linear_path` を直接呼んでもよい（その場合は
> 公開フローの `if line := _generate_linear_path(polygon, end_inset):` とする）。
> **テストは公開 API 経由でフォールバック挙動を検証するため、`_line_fill` の有無は
> spec-test-author に影響しない**（private ヘルパーは直接テストしない方針）。

---

## 3. セグメント内包契約（最重要・テストの軸）

`spec-test-author` が `TestSegmentContainment` で検証する不変条件。**公開 API 経由**で検証する。

各成分ポリライン `poly`（= 戻り値の各内側 `list[Point2d]`）について:

1. **隣接セグメント内包**: `poly` の隣接 2 点 `poly[i], poly[i+1]` が作る
   `LineString([(p.x,p.y) for p in (poly[i], poly[i+1])])` が
   `polygon.covers(line_string)` を満たす（元ポリゴン内に収まる＝パッド外を横切らない）。
   - `polygon` は **build_paste_fill_path に渡した元ポリゴン**（buffer 前）。
   - 浮動小数誤差対策として、検証側は `polygon.buffer(EPS).covers(...)`（`EPS` は微小・
     parametrize 由来でなく定数）を許容してよい。許容 `EPS` の値は spec 側の裁量
     （推奨 1e-9〜1e-6。ジグザグ端点が外周に乗るため境界一致を covers が拾えるよう微小 buffer）。
2. **成分間に面上接続線が無い**: 戻り値が複数ポリライン（複数成分）の場合、**成分間を繋ぐ
   セグメントは戻り値に含まれない**（= 各成分は独立した内側リスト。成分間移動は applicator が
   空中 lift→travel→plunge で行うため fill_path は連結線を出さない）。
   - 検証観点: 「成分が 2 つ以上に割れる凹形（L字・ダンベル）で、戻り値の外側リスト長 ≥ 2」かつ
     「各内側ポリラインが単独で内包条件 1 を満たす（成分跨ぎの長い横断セグメントが存在しない）」。

parametrize 対象: L字・ダンベル（凹形）を最低 2 形状。ノズル径は成分分割を誘発する値。
しきい値（EPS）は spec 側で定数化、形状は parametrize。

---

## 4. consumer IF 変更通知（plan-implementer 向け配線契約）

### 4-1. `config.py` `PasteDispenser`（`src/pcbasm/config.py:26`, `attrs.frozen`）

末尾（デフォルト付き）に 3 フィールド追加。**TOML 後方互換のため必ず末尾・デフォルト付き**。
既存末尾 `prime_extra_delay: float = 0.0` の **後ろ**に並べる。

```python
    prime_extra_delay: float = 0.0  # 既存
    bead_width_factor: float = 1.0  # ビード幅係数 w = nozzle_diameter * bead_width_factor
    overlap: float = 0.0            # ジグザグ行間オーバーラップ [0,1)
    boundary_margin: float = 0.0    # 外周マージン [mm]
```

### 4-2. `session.py` `make_applicator`（`src/pcbasm/session.py:107`）

`cfg = self.machine.paste_dispenser` から 3 値を `PasteApplicator` へ追加で渡す:

```python
            bead_width_factor=cfg.bead_width_factor,
            overlap=cfg.overlap,
            boundary_margin=cfg.boundary_margin,
```

### 4-3. `applicator.py` `PasteApplicator.__init__`（`src/pcbasm/pasting/applicator.py:68`）

kw 引数 3 つ追加（デフォルト付き・`_` private 属性で保持）。`prime_extra_delay` の近傍に並べる:

```python
        bead_width_factor: float = 1.0,
        overlap: float = 0.0,
        boundary_margin: float = 0.0,
```
```python
        self._bead_width_factor = bead_width_factor
        self._overlap = overlap
        self._boundary_margin = boundary_margin
```

### 4-4. `applicator.py` `_fill` の成分ループ化（`src/pcbasm/pasting/applicator.py:196`）

新しい `_fill` の構造（`build_paste_fill_path` が `list[list[Point2d]]` を返すことに追従）:

```python
def _fill(self, polygon: Polygon) -> None:
    components = build_paste_fill_path(
        polygon,
        nozzle_diameter=self._nozzle_diameter,
        bead_width_factor=self._bead_width_factor,
        overlap=self._overlap,
        boundary_margin=self._boundary_margin,
    )
    if not components:
        self._logger.warning("フィルパスが空です。スキップします。")
        return

    total_amount = polygon.area * self._ul_per_mm2   # ← 面積ベース維持（計画準拠）
    prime_time = _trapezoidal_time(self._retraction, self._dispense_rate, self._dispense_accel)

    for raw in components:
        path = Path(p.to3d(self._paste_height) for p in raw).transformed(self._transform)
        sequence = FillSequence(
            path=path,
            total_amount=total_amount / len(components),   # ← 決定事項A（均等配分）
            retraction=self._retraction,
            extra_amount=self._dispense_rate * self._prime_extra_delay,
            dispense_rate=self._dispense_rate,
            dispense_accel=self._dispense_accel,
            retraction_rate=self._retraction_rate,
            retraction_accel=self._retraction_accel,
            prime_time=prime_time + self._prime_extra_delay,
            lift_height=self._lift_height,
            travel_speed=Speed.rate(1.0),
        )
        self._klipper.send_gcode(sequence.to_gcode(self._stage, self._paste_dispenser))
```

- 各成分は独立した `FillSequence`。`FillSequence.to_gcode` が先頭点上空への travel→下降
  （plunge）→吐出→retract→`lift_height` 上昇を 1 本に組むため、**成分間の lift→travel→plunge は
  FillSequence の連続送信だけで自然に実現**される（applicator 側で追加の移動 GCode は不要）。
- `FillSequence.path` は空でないこと前提。`build_paste_fill_path` は各内側ポリラインが
  1 点以上を保証するため、`raw` が空のポリラインを返さない（点フォールバックは1点）。
  - 1 点ポリライン（点塗布）の場合 `path.length()==0` → `FillSequence.fill_speed()` が `None` →
    吐出時間ぶん待機で点吐出する（既存 fill_sequence の挙動。変更不要）。

### 決定事項A（**total_amount の成分配分** — 最重要・曖昧を残さない）

> **結論: 成分ごとの `FillSequence.total_amount` = `(polygon.area * ul_per_mm2) / len(components)`（成分数による均等配分）。**

根拠と検討:
- 計画の確定事項は「`total_amount` は **面積ベース維持**（`polygon.area * ul_per_mm2`）。
  margin による薄塗りは人間が実機評価。塗布面積追従は本タスク外」（plan L21, L77）。
  → **総量は元 polygon 面積で決まる**ことが正典の制約。成分ごとの内訳は計画が指定していない。
- 候補比較:
  - (a) **均等配分** `total/len(components)`: 実装が最小。`sum == polygon.area*ul_per_mm2` が
    `len` に依らず厳密成立（浮動小数の丸めのみ）。成分が 1 個（大多数のパッド）のとき
    旧挙動と完全一致（`total_amount = polygon.area*ul_per_mm2`）。
  - (b) 面積按分 `total * (component_inset_area / sum_inset_area)`: より「正しい」が、
    各成分の buffer 後面積を別途算出する必要があり、build_paste_fill_path は面積を返さない
    （ポリラインのみ）。applicator から成分面積を再計算するには戻り値設計の拡張が要る＝
    計画外の複雑化。塗布面積追従が本タスク外である以上、過剰。
  - (c) パス長按分: 牛耕式パス長は被覆面積の代理に過ぎず、(b) 以上に間接的で根拠が弱い。
- **採用 (a) 均等配分**。理由: (1) 「総量＝元面積ベース」の正典制約を最小コードで厳密に満たす、
  (2) 単一成分（圧倒的多数）で旧挙動と完全一致＝回帰リスク最小、(3) 多成分の内訳精緻化は
  「塗布面積追従＝本タスク外」に該当するため投機的実装を避ける（CLAUDE.md 原則2）。
- 実機での多成分薄塗り/厚塗りの是非は人間が評価（計画通り）。不十分なら別タスクで (b) へ。

**この決定で spec-test-author（test_applicator.py 追従）と plan-implementer の期待が一致する:**
- 単一成分パッド: `FillSequence.total_amount == polygon.area * ul_per_mm2`（旧と同値）。
- N 成分パッド: 各 `FillSequence.total_amount == polygon.area * ul_per_mm2 / N`、
  かつ N 本の `FillSequence` が生成される（＝ `send_gcode` が N 回呼ばれる）。

---

## 5. テスト観点の列挙（spec-test-author 向け。**テストは書かない**。観点のみ）

対象: `tests/pcbasm/pasting/test_fill_path.py` 全面改訂 ＋ `tests/pcbasm/pasting/test_applicator.py` 追従。
区分: fill_path は **unit**（HW非依存・モック不要・実 `Polygon` 直接・`@mark_hardware` 不要）。
公開 API 経由・`class TestXxx` 集約・Arrange/Act/Assert・例外は substring 検証・ミラーレイアウト。
**しきい値は parametrize で導出**（ハードコードしない。形状・ノズル径・overlap・margin をパラメータ化）。

| テストクラス | 何を検証するか | parametrize の軸 |
|---|---|---|
| `TestFallbackHierarchy` | 形状×ノズル径で 面/線/点 を誘発。大矩形→面（複数点ポリライン・外周＋ジグザグ）、細長矩形→線（1ポリライン2点）、極小パッド→点（1ポリライン1点）。**点でも外側リスト空でない**。 | (形状, ノズル径) → 期待段（area/line/dot） |
| `TestAreaFill` | 戻り値に外周ポリラインが存在、ジグザグが最長軸方向に走る、被覆が概ね均一（パス buffer の被覆率がしきい以上）。 | 矩形/roundrect サイズ、ノズル径 |
| `TestSegmentContainment`（最重要） | §3 の不変条件: 各成分ポリラインの隣接セグメント `LineString` が `polygon.covers`（微小 EPS buffer 許容）。複数成分時は外側リスト長≥2 かつ成分跨ぎ横断セグメント無し。 | L字・ダンベル（最低2形状）、成分分割を誘発するノズル径 |
| `TestCoverage` | `overlap` を上げると `line_spacing` が縮み、ジグザグ行間隔が縮む（行数増 or 隣接スキャン間距離が小さくなる）。しきいは `line_spacing = w*(1-overlap)` から導出。 | overlap ∈ {0.0, 0.25, 0.5}、固定形状・ノズル径 |
| `TestExteriorMargin` | `boundary_margin>0` のとき、面塗布の全頂点・全セグメントが元ポリゴン外周から **`boundary_margin` 以上**内側（= `polygon.exterior.distance(point) >= boundary_margin - EPS`、または `polygon.buffer(-boundary_margin).covers(...)`）。margin による領域縮小を確認。 | boundary_margin ∈ {0.0, 0.1, 0.3}、固定形状 |
| `TestInvalidInput` | §1 バリデーション表の各不正値が `ValueError`（substring: `nozzle_diameter`/`overlap`/`boundary_margin`/`bead_width_factor`）。空ポリゴン・不正ポリゴン → `[]`。 | (引数, 不正値, 期待substring) |
| `TestReturnType`（公開API契約ピン） | 戻り値が `list[list[Point2d]]`。正常時は外側≥1・各内側≥1点・全要素 `Point2d`。`@pytest.mark.api_contract` を付すか `tests/pcbasm/test_api_contract.py` 新設（spec 裁量）。 | 代表形状（面/線/点）各1 |

`test_applicator.py` 追従観点（成分ループ・新戻り値型）:
- 単一成分パッド: `FillSequence` 1 本、`total_amount == polygon.area * ul_per_mm2`、`send_gcode` 1 回。
- 複数成分パッド（凹形）: `FillSequence` N 本（N = 成分数）、各 `total_amount == polygon.area*ul_per_mm2 / N`、`send_gcode` N 回。
- 空フォールバック（不正/空ポリゴン → `build_paste_fill_path` が `[]`）: warning ＆ skip（`send_gcode` 0 回）。
  - ※ 正常パッドは点フォールバックで必ず非空。`[]` は不正/空ポリゴン入力時のみ。
- モック方針: `Klipper`（自前 HAL ABC）は fake 可。`send_gcode` 呼び出し回数・各 `FillSequence` の
  `total_amount` を検証できる粒度で。3rd-party 表面のモックは禁止（`testing-strategy` 準拠）。
- 撤廃: 旧 `TestSpiralBranch`（および螺旋前提のケース）を削除。

---

## 6. dev script 追従通知（実装は Phase 1・plan-implementer 担当）

`src/scripts/dev/fill_path_simulate.py:225` `_build_paths`:

```python
def _build_paths(pads, nozzle_diameter) -> list[list[Point2d]]:
    return [build_paste_fill_path(pad.polygon, nozzle_diameter) for pad in pads]
```

- 戻り値型が `list[Point2d]` → `list[list[Point2d]]` に変わるため、`_build_paths` の戻り値は
  **「パッド × 成分」の二重リスト** `list[list[list[Point2d]]]` になる（型注記・描画ループを追従）。
- 描画ループ（各 pad の path を 1 本として描く箇所）を **成分単位**で回すよう変更。
- 計画 L100: 被覆率（`path.buffer(w/2).area / polygon.area`）可視化を追加。
- 要件網羅 PCB フィクスチャ `data/testing/fill_coverage/fill_coverage.kicad_pcb` 作成も Phase 1。
- **本メモではシグネチャ確定のみ。dev script の実装詳細は plan-implementer の実装ノートで詰める。**

---

## 7. Phase 1 作業境界（disjoint・並列起点）

| agent | 触る範囲 | 触らない範囲 |
|---|---|---|
| `spec-test-author` | `tests/pcbasm/pasting/test_fill_path.py`（全面改訂）、`tests/pcbasm/pasting/test_applicator.py`（追従）、（任意）`tests/pcbasm/test_api_contract.py` | `src/` 一切 |
| `plan-implementer` | `src/pcbasm/pasting/fill_path.py`（全置換）、`src/pcbasm/config.py`、`src/pcbasm/session.py`、`src/pcbasm/pasting/applicator.py`、`src/scripts/dev/fill_path_simulate.py`、`data/testing/fill_coverage/`（新規） | `tests/` 一切 |

両者の唯一の共有契約 = **本メモ**。シグネチャ・戻り値型・バリデーション substring・total_amount 配分
（§4 決定事項A）・セグメント内包契約（§3）がすべて確定しているため、相互参照なしに並列実装できる。

合流時の検証: `make format && make type && make test-no-hardware`。実機検証は人間（kurousagi 等）。

## 参照
- 計画（正典）: `/home/gop/.claude/plans/fill-adaptive-parasol.md`
- skill `refactor-conventions` / `testing-strategy`（unit区分・モック禁止範囲・契約ピン例外）
- `memory/agents/fill-redesign-handoff.md`（セッション引き継ぎ・設計判断ログ）
