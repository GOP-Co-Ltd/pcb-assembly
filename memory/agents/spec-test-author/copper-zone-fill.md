# copper-zone-fill — Branch 1 仕様テスト（spec-test-author）

対象: `tests/pcbasm/geometry/test_polygon.py`（新規）
計画書: `memory/agents/implementation-planner/pad-alignment-reuse.md` の「Branch 1」

## 書いたテスト（`class TestMergeIslands`、全 6 件）

| テスト | 仕様根拠（計画書 Branch 1） |
| --- | --- |
| `test_ヘアラインギャップで隣接する矩形はsnapで1つのislandに融合される` | closing（buffer +snap/−snap）で 5µm ギャップを橋渡し → 1 island、面積は両矩形の合計以上 |
| `test_snapを超える実クリアランスで離れた矩形は分離したまま` | snap_mm=0.01 では 0.2mm の実クリアランスを橋渡ししない → 2 islands、合計面積保存 |
| `test_重なる矩形はunary_unionで1つのislandに融合される` | unary_union 経路。面積 = `lower.union(upper).area` |
| `test_穴付きpolygonの穴は融合後も保存される` | 穴付き Polygon 1 つ入力 → interior ring が 1 つ残り面積保存（計画書の簡易版構成を採用） |
| `test_sliver穴はsnapのclosingで除去される` | 実基板診断（GENS_Power_Section_5, F.Cu）: union 後に幅 0.0035〜0.005mm の sliver 穴が 10 個残り、closing snap=0.01mm で全消去を確認済み。幅 5µm × 2mm の interior ring → interior が消え、外形面積ほぼ保存（abs=0.02） |
| `test_空入力は空リストを返す` | `merge_islands([], snap_mm=0.01) == []` |

## 穴の保存/除去の境界（テスト 4 と 6 の対比）

- 広い穴（テスト 4: 2mm 角 ≫ 2×snap）→ 保存
- sliver 穴（テスト 6: 幅 0.005mm < 2×snap=0.02mm）→ 除去

## API 契約上の判断

- import は `from pcbasm.geometry import merge_islands`（パッケージレベル）。
  計画書に「`geometry/__init__.py` に export」とあるため、export 自体を契約としてピンした。
  既存テスト（`test_sampling.py` 等）のパッケージレベル import 慣例にも一致。

## 状況（2026-06-12 更新）

- 実装が着地し、全 6 テスト green。`make format` 全 Passed。
- テスト側の修正 2 件（いずれもテストの不備、実装は仕様通り）:
  - テスト 1: `>= pytest.approx(...)` は TypeError → 素の比較 `>= 合計 - 1e-9` に修正
  - テスト 3: closing は凹コーナーに半径 snap のフィレットを残し面積が
    `2×(1−π/4)×snap² ≈ 4.3e-5 mm²` 増える（closing に数学的に内在する挙動）。
    厳密一致は過剰だったため `abs=1e-3` に緩和

## 実装側への要求

- `merge_islands` を `pcbasm.geometry` パッケージから import 可能にする（`__init__.py` export）。
- closing 後も入力にあった穴（snap_mm より十分大きい interior）は保存されること
  （closing は外側 buffer → 内側 buffer なので、大きな穴は埋まらない想定）。
