# fill-path-integration: 計画書

## タスク概要

`src/pcb_assembly/geometry/fill.py` を `src/pcb_assembly/control/pasting/fill_path.py` に統合し、付随する demo を実 PCB データ駆動の CLI スクリプト `fill_path_simulate.py` に作り直す。

## 動機

- `geometry/fill.py` の中身（spiral / linear / concentric_rings）は実体としてはんだペースト塗布専用ロジックであり、「汎用幾何」モジュールにそぐわない
- `generate_concentric_rings` は production 未使用（テストでのみ参照）
- 現 `fill_path_demo.py` はハードコード多角形を 2×3 で描く玩具デモで、実 PCB ワークフローと繋がっていない

## ブランチ

`refactor/20260527/fill-path-integration` （main から分岐済み）

## 公開 IF（シグネチャレベル）

統合後の `src/pcb_assembly/control/pasting/fill_path.py` の API は以下で確定。並列実装の前提となる。

```python
# public — 既存と同じ
def build_paste_fill_path(polygon: Polygon, nozzle_diameter: float) -> list[Point2d]: ...

# private（_ prefix）— 既存 generate_spiral_path / generate_linear_path をリネーム、引数は不変
def _generate_spiral_path(
    polygon: Polygon, line_spacing: float, initial_inset: float
) -> list[Point2d]: ...

def _generate_linear_path(polygon: Polygon, end_inset: float) -> list[Point2d]: ...
```

`generate_concentric_rings` は削除。

## 設計上の決定（ユーザー合意済）

1. `generate_concentric_rings` と `_concentric_rings_one_component` を削除
2. `generate_spiral_path` / `generate_linear_path` を `_` prefix で private 化（fill_path.py 内ヘルパー扱い）
3. fill_path_simulate.py 新設、起動: `python -m pcb_assembly.scripts.fill_path_simulate <pcb_file> ...`
4. CLI 引数: `pcb_file`（positional） / `--nozzle-diameter`（default 0.4） / `--output`（default: pcb と同 dir に `<stem>_fill_path.png`） / `--layer`（`top` | `bottom`, default `top`）
5. 1 枚のボード全体ビュー（`extract_pcb.py` のスタイル踏襲）
6. fill_path_simulate.py にはテストを追加しない（手元実行で確認）
7. エージェント運用: planner（本書）→（spec-test-author × plan-implementer 並列）→ fill_path_simulate 用 plan-implementer 単独 → code-simplifier → docs-keeper

## 影響範囲

| 場所                                                                                                               | 変更                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| [src/pcb_assembly/geometry/fill.py](../../src/pcb_assembly/geometry/fill.py)                                       | 削除（内容は fill_path.py へ移送、concentric 系は削除）                                                                               |
| [src/pcb_assembly/geometry/__init__.py](../../src/pcb_assembly/geometry/__init__.py)                               | 3関数の import / `__all__` エントリ削除                                                                                               |
| [src/pcb_assembly/control/pasting/fill_path.py](../../src/pcb_assembly/control/pasting/fill_path.py)               | fill.py の実装を取り込み、spiral/linear を `_` prefix 化                                                                              |
| [src/scripts/fill_path_demo.py](../../src/scripts/fill_path_demo.py)                                               | 削除                                                                                                                                  |
| `src/scripts/fill_path_simulate.py`                                                                                | 新設                                                                                                                                  |
| [tests/pcb_assembly/geometry/test_fill.py](../../tests/pcb_assembly/geometry/test_fill.py)                         | spiral/linear テストクラスを test_fill_path.py へ移送、concentric テストは削除、ファイル削除                                          |
| [tests/pcb_assembly/control/pasting/test_fill_path.py](../../tests/pcb_assembly/control/pasting/test_fill_path.py) | spiral/linear のテストクラスを統合（private は `from pcb_assembly.control.pasting.fill_path import _generate_spiral_path` で import） |

## fill.py ヘルパー処置

fill_path.py へ移送（private ヘルパー扱い）:
`_truncate_ring`, `_rotate_ring_to_nearest`, `_offset_components`, `_ring_coords`, `_spiral_one_component`, `_sort_components_by_seed`, `_walk_rings_outward`, `_estimate_ring_spacing`, `_connect_nearest`

削除: `_concentric_rings_one_component`

## Phase 構成

### Phase 2: spec-test-author × plan-implementer 並列

両者は `tests/` と `src/` で disjoint。1 メッセージで並列起動する。

**spec-test-author の作業範囲**:

- `tests/pcb_assembly/control/pasting/test_fill_path.py` に spiral / linear テストクラスを追加
    - 元: `tests/pcb_assembly/geometry/test_fill.py::TestGenerateSpiralPath`, `TestGenerateLinearPath`
    - 移送先のテストクラス内で `_generate_spiral_path` / `_generate_linear_path` を private 経由で import
    - 既存 `TestBuildPasteFillPath` は維持
- `tests/pcb_assembly/geometry/test_fill.py` を削除（concentric テストクラスを含めて削除）
- src/ には一切触らない（実装は plan-implementer が並列で進める）
- 完了後、tests/ 全体が「実装が完成すれば pass する状態」になっていることを `memory/agents/spec-test-author/fill-path-integration.md` に記録

**plan-implementer の作業範囲**:

- `src/pcb_assembly/control/pasting/fill_path.py` に `geometry/fill.py` の中身（concentric 系を除く）を取り込み、`generate_spiral_path` → `_generate_spiral_path`、`generate_linear_path` → `_generate_linear_path` にリネーム
- `build_paste_fill_path` 内の呼び出しも新名へ更新
- `src/pcb_assembly/geometry/fill.py` を削除
- `src/pcb_assembly/geometry/__init__.py` から 3 関数の import と `__all__` エントリを削除
- `src/scripts/fill_path_demo.py` を削除（後段で fill_path_simulate.py を新設）
- tests/ には一切触らない
- 完了後、`memory/agents/plan-implementer/fill-path-integration.md` に実装ノートを記録

**合流チェック**:

```bash
make format && make type && make test-no-hardware
```

不整合発覚時は該当 agent を再呼び出し。

**Phase 2 commit**:

```
refactor(pasting): geometry/fill.py を fill_path.py へ統合し private 化
```

### Phase 3: plan-implementer 単独（fill_path_simulate.py 新設）

- `src/scripts/fill_path_simulate.py` を新設（`extract_pcb.py` がテンプレ）
- argparse: `pcb_file`, `--nozzle-diameter` (default 0.4), `--output` / `-o`, `--layer` (`top`/`bottom`, default `top`)
- `render_fill_paths(outline, pads, paths, nozzle_diameter, output_path)`:
    - PCB outline（白破線）
    - paste pad 多角形（top: 緑 / bottom: 赤、`extract_pcb.py` 配色）
    - 各 pad の fill path:
        - **ノズル塗布幅 halo**: `LineString(path).buffer(nozzle_diameter / 2)` の半透明青ポリゴン → `polygon_with_holes_patch` で描画
        - 中心線: 細い青実線
        - 始点: 赤丸
        - 方向: 始点→次点に矢印
    - タイトル: ボードサイズ / pad 数 / nozzle 径
    - 凡例 / y 軸反転（KiCAD 座標系）
- **注意**: matplotlib の `linewidth` は points 単位なので halo は必ず shapely.buffer（mm）で
- 手元検証:
    ```bash
    uv run python -m pcb_assembly.scripts.fill_path_simulate \
        data/testing/led_blinker/led_blinker.kicad_pcb --nozzle-diameter 0.4 --layer top \
        -o /tmp/led_blinker_fill_path.png
    uv run python -m pcb_assembly.scripts.fill_path_simulate \
        data/PnpTest/PnpTest.kicad_pcb --nozzle-diameter 0.4
    uv run python -m pcb_assembly.scripts.fill_path_simulate --help
    ```
- 完了後、`memory/agents/plan-implementer/fill-path-simulate.md` に実装ノート

**Phase 3 commit**:

```
feat(scripts): fill_path_simulate を新設し実 PCB データで fill 経路を可視化
```

### Phase 4: code-simplifier

- 統合後の fill_path.py / fill_path_simulate.py を見直し
- 公開 IF は不変、テストグリーン維持
- 変更あれば `memory/agents/code-simplifier/fill-path-integration.md`、commit `refactor(pasting): fill_path 周辺の整理`

### Phase 5: docs-keeper

- fill_path.py の module docstring と各関数 docstring を統合後の責務に合わせて更新
- fill_path_simulate.py の module docstring / argparse help を整える
- geometry モジュール側に fill 関連の言及が残っていれば除去
- 変更あれば `memory/agents/docs-keeper/fill-path-integration.md`、commit `docs(pasting): 統合後 fill_path 関連 docstring 整備`

## 並列化前提条件（パターン A）チェック済

- ✅ 公開 IF シグネチャ確定
- ✅ tests/ と src/ disjoint
- ✅ ハードウェアリソース同時アクセスなし
- ✅ tests/helpers.py の編集なし

## 検証コマンド

```bash
make format
make type
make test-no-hardware
```

最終的に `make run`（format → test → type）グリーンで完了。
