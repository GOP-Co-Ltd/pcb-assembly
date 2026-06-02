# phase5: hal.Probe → ServoGroundProbe リネーム

ブランチ: `refactor/20260527/phase5-cleanup`
担当: plan-implementer（クラスリネーム）
並行作業: spec-test-author が `tests/pcbasm/pasting/test_fill_path.py` を担当（こちらは未編集）

## タスク

`src/pcbasm/hal/probe.py` の `class Probe`（サーボ GND タッチ式 HAL、`ProbeSensor`+`ProbeGround` 統合）を
`ServoGroundProbe` にリネーム。クラス名のみ変更、ロジック・引数・振る舞いは不変。

## 編集ファイル（hal.Probe 参照のみ追従）

1. `src/pcbasm/hal/probe.py`
   - `class Probe:` → `class ServoGroundProbe:`
   - docstring Example の `probe = Probe(...)` → `ServoGroundProbe(...)`
   - `__init__` docstring `"""Probeを初期化する."""` → `"""ServoGroundProbeを初期化する."""`
2. `src/pcbasm/hal/__init__.py`
   - `from .probe import Probe` → `from .probe import ServoGroundProbe`
   - `__all__` の `"Probe"` → `"ServoGroundProbe"`
3. `src/pcbasm/pasting/probe.py`
   - import `Probe` → `ServoGroundProbe`
   - 型注釈 `probe: Probe` → `probe: ServoGroundProbe`
4. `src/scripts/pasting/paste_solder.py`
   - import `Probe` → `ServoGroundProbe`
   - `probe = Probe(...)` → `ServoGroundProbe(...)`
5. `src/scripts/pasting/pasting_height_plane.py`
   - import + 構築 `Probe(...)` → `ServoGroundProbe(...)`
6. `src/scripts/pasting/pasting_toolhead_offset.py`
   - import + 構築 `Probe(...)` → `ServoGroundProbe(...)`
7. `tests/pcbasm/hal/test_probe.py`
   - import `Probe, ProbeGround, ProbeSensor` → `ProbeGround, ProbeSensor, ServoGroundProbe`
   - 構築 3 箇所 `Probe(...)` → `ServoGroundProbe(...)`
   - アサーション・ロジック・テストクラス構成は不変（`TestProbe` クラス名は test grouping のため据え置き）

## 触っていない（意図的な非参照ヒット）

- `src/scripts/pasting/paste_solder.py:76` — コメント `# Probe / HeightPlaneMeasurer初期化`（コード参照ではないセクション見出し、最小スコープ方針で据え置き）
- `src/scripts/pasting/pasting_height_plane.py:104` — matplotlib の `label="Probe points"`（可視化ラベル文字列、クラス参照ではない）
- `ProbeSensor` / `ProbeGround` / `ProbeExecutor` — 別クラス、リネーム対象外

## config.Probe 不変の確認

`config.Probe`（probe 設定 dataclass、`[probe]` セクション）は一切変更していない。
リネーム前後で grep 結果が同一:

- `src/pcbasm/config.py`: 43 (`class Probe:`), 302 (`def probe(self) -> Probe:`), 304 (`_get_config("probe", Probe)`)
- `tests/pcbasm/test_config.py`: 13, 279, 290, 305, 315, 327

## grep 検証結果

```
grep -rnw "Probe" src/pcbasm/hal/ src/pcbasm/pasting/ src/scripts/ tests/pcbasm/hal/
```
→ hal.Probe 由来の旧名 `Probe` 残存ゼロ。残るのは上記 2 件の非参照ヒットのみ
（`ProbeSensor/ProbeGround/ProbeExecutor` は対象外）。

```
grep -rnw "Probe" src/ tests/ | grep -v "ProbeSensor\|ProbeGround\|ProbeExecutor"
```
→ config.Probe（6 行）+ 非参照 2 件のみ。hal.Probe シンボル参照は消滅。

## make 結果

- `make format`: PASS（ruff / ruff-format / docformatter / codespell など全 Passed）
- `make type`: PASS（pyright 0 errors）
  - warning 2 件は `tests/pcbasm/pasting/test_fill_path.py`（spec-test-author 担当・並行作業中）の
    private 使用警告で、本リネームとは無関係
- `make test-no-hardware`: 472 passed, 15 deselected（`@mark_hardware` 分）
  - `tests/pcbasm/hal/test_probe.py` 単体: 11 passed（hardware mark なし、全て実行・通過）

## IF 変更通知

公開 IF の変更は「クラス名 `Probe` → `ServoGroundProbe`」のみ。
シグネチャ（引数 `klipper, servo_name, revolution_distance, down_distance`）・メソッド
（`probe()`, `get_last_z_result()`）は不変。spec-test-author の担当範囲（test_fill_path.py）には影響なし。

## コミット

未実施（Claude main が実施予定）。
