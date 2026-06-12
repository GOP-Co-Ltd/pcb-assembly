# posctrl リファクタ: 位置合わせ再利用化 + copper union 修正 + 実銅箔 ROI

承認済み計画（2026-06-12）。ユーザー確認事項:

- 照合失敗箇所（赤輪郭が実銅箔の内側）は**ベタ接続部**
- paste_solder への補正統合まで**今回実施**
- `pcbnew.ZONE_FILLER` の可用性は確認済み（available: True）

**原因確定（2026-06-12、実データ診断）**: GENS_Power_Section_5.kicad_pcb の F.Cu を診断した結果、zone fill は存在（12 ポリゴン）するが、**pad ポリゴンと zone fill の円弧近似の不一致で union 後に幅 0.0035〜0.005mm の極細 sliver 穴が 10 個残る**ことを確認。これが「実銅箔内部の偽エッジ（pad 輪郭の弧）」の正体。closing snap=0.01mm（2×m_MaxError）で全 sliver が消え、island も 31→30 と微小ギャップ 1 箇所が正しく橋渡しされることを実証済み。stale fill 仮説は副因に格下げ（zone 再 fill は堅牢化として残す）。

## Branch 1 — `fix/20260612/copper-zone-fill`（main から）

- `src/pcbasm/geometry/polygon.py`（新規）: `merge_islands(polygons: Sequence[Polygon], snap_mm: float) -> list[Polygon]`
  — `unary_union` 後に closing（`buffer(+snap).buffer(-snap)`）でヘアラインギャップを埋め、連結成分ごとの Polygon を返す。`geometry/__init__.py` に export
- `src/pcbasm/pcb/kicad.py`:
  - `copper` property の冒頭で zone を 1 回だけ再 fill: `pcbnew.ZONE_FILLER(self._board).Fill(self._board.Zones())`。失敗時はキャッシュへフォールバック + warning
  - zone の fill が空のままならレイヤーごとに `logger.warning`（診断）
  - `unary_union` + 手動 island 分割（旧 222-226 行）→ `merge_islands(polygons, snap_mm=2 * max_error_mm)`

テスト: `tests/pcbasm/geometry/test_polygon.py`（新規、merge_islands: 5µm ギャップ融合 / 0.2mm 分離 / 穴保存 / 空入力）、`tests/pcbasm/pcb/test_kicad.py` の既存 copper テストが再 fill 後も green であること。

コミット:
1. `feat(geometry): merge_islands ヘルパーを追加`
2. `fix(pcb): copper読み込み時にzoneを再fillしsnap unionを適用`

## Branch 2 — `refactor/20260612/pad-alignment-reuse`（Branch 1 にスタック）

### 公開 IF（シグネチャ確定）

`src/pcbasm/geometry/routing.py` — `sort_by_nearest` に key オーバーロード:

```python
@overload
def sort_by_nearest(positions: Iterable[Point3d], start: Point3d) -> list[Point3d]: ...
@overload
def sort_by_nearest[T](positions: Iterable[T], start: Point3d, *, key: Callable[[T], Point3d]) -> list[T]: ...
```

`src/pcbasm/geometry/polygon.py` — `transform_polygon(polygon: Polygon, transform: Transform) -> Polygon`（exterior/interiors 各頂点へ 2D Transform 適用）

`src/pcbasm/pcb/board.py` — `Pad.copper_polygon: Polygon` 追加。default `attrs.Factory(lambda self: self.polygon, takes_self=True)`（後方互換）。cattrs で不調なら `Polygon | None = None` + 使用側 fallback。
`src/pcbasm/pcb/kicad.py` — `pads` property で `GetEffectivePolygon(F_Cu/B_Cu)` も抽出し `copper_polygon` に設定（同じ原点正規化）。

`src/pcbasm/posctrl/pad.py` — `PadAligner.align` の ROI を `[p.copper_polygon for p in target.pads]` に変更（1 行）。conjugation・符号規約・`to_machine_transform`・`correction.py` は**不変**。

`src/pcbasm/posctrl/alignment.py`（新規）:

```python
@attrs.frozen
class ComponentAlignments:
    board_transform: Transform
    results: tuple[tuple[ComponentPads, PadAlignmentResult], ...]
    def result_of(self, designator: str) -> PadAlignmentResult | None: ...
    def corrected_board_transform(self, designator: str) -> Transform | None:
        # Compose([board_transform, machine_transform])（board_tour 実機検証済みの順序）
    def board_correction(self, designator: str) -> Transform | None:
        # C = T_b⁻¹∘M∘T_b = Compose([board_transform, machine_transform, board_transform.inverse()])

class PadAlignmentSession:
    @classmethod
    def from_calibration(cls, result: BoardCalibrationResult, window_name: str | None = None) -> Self: ...
        # TOP層copper・camera frame size・machine.paste_dispenser.pad_align から
        # CopperProjector / CopperEdgeMatcher / CopperEdgeDetector / PadAligner を構築
    def align(self, target: ComponentPads) -> PadAlignmentResult | None: ...
        # 照合失敗(RuntimeError)はログしてNone返却
    def corrected_projector(self, machine_transform: Transform) -> CopperProjector: ...
    @property
    def projector(self) -> CopperProjector: ...
    @property
    def edge_detector(self) -> CopperEdgeDetector: ...
```

計測ループ（移動順・print・Esc・overlay）はスクリプト側に残す。`posctrl/__init__.py` に export 追加。

### スクリプト

- `src/scripts/posctrl/board_tour.py`: 配線ブロック → `PadAlignmentSession.from_calibration`。try/except → None 分岐。nearest ソート 3 箇所を key 形式へ。`_tour_corrected_pads` は `session.corrected_projector(...)`。失敗 overlay も `copper_polygon`。
- `src/scripts/pasting/paste_solder.py`: `setup_board_calibration()` → `PasteSession.from_calibration(result)`（height_plane.py の前例）。**高さ計測の前に** `_align_components(result) -> ComponentAlignments`。塗布時は pad ごとに `board_correction(pad.designator)` を `transform_polygon(pad.polygon, c)` で適用（PasteApplicator 無変更）。未照合部品は無補正 + 警告 print。

### テスト

- `tests/pcbasm/geometry/test_polygon.py` 追記: `transform_polygon`（Shift/Rotation/Compose、穴付き、恒等）
- `tests/pcbasm/geometry/test_routing.py` 追記: key ソート / 同一座標が潰れない / key なし不変
- `tests/pcbasm/pcb/test_board.py` 追記: `copper_polygon` default / dict ラウンドトリップ / 旧形式後方互換
- `tests/pcbasm/pcb/test_kicad.py` 追記: 各 paste pad の `copper_polygon` が valid・非空・paste centroid を cover
- `tests/pcbasm/posctrl/test_alignment.py`（新規）: ComponentAlignments（result_of / corrected_board_transform = Compose([T_b, M]) / conjugation ピン `T_b(board_correction(b)) == M(T_b(b))`）、PadAlignmentSession（FakeCamera + mocker.Mock klipper/stage、align の None / 成功、corrected_projector の pixel_of）

### コミット

3. `feat(geometry): sort_by_nearestにkey引数とtransform_polygonを追加`
4. `feat(pcb): Padに実銅箔copper_polygonを追加`
5. `fix(posctrl): PadAlignerのROIをpaste apertureから実銅箔に変更`
6. `feat(posctrl): PadAlignmentSession/ComponentAlignmentsで配線と結果lookupを集約`
7. `refactor(scripts): board_tourをPadAlignmentSessionへ移行`
8. `feat(scripts): paste_solderにpad位置補正を統合`

## 検証・リスク

- 各ブランチ `make format && make type && make test-no-hardware` green（569 passed 基準）
- MR: Branch 1 → main、Branch 2 → Branch 1（マージはユーザー判断）
- 実機検証はユーザー実施（ベタ接続部品の align / board_tour / paste_solder）
- リスク: ZONE_FILLER の速度（読み込み時 1 回）/ closing snap 幅 0.01mm vs 実クリアランス ≥0.1mm / attrs.Factory(takes_self) × cattrs / マスク開口バイアス（未対応の残リスク）/ paste_solder のフロー順（位置合わせ→高さ計測）
