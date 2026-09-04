# pcbasm namespace メソッド化 第 2 弾（見送り 3 件の実装）

!200（`memory/agents/plan-implementer/pcbasm-namespace-methods.md`）で
「確信度は高いが diff 過大」として見送った 3 件を実装した。branch は
`refactor/2026-09-04/pcbasm-namespace-methods-2`。

## 実装した 3 件

| commit | 移動 | 呼び出し元 (src/tests) | diff |
|---|---|---|---|
| 1620c79 | `build_pad_hierarchy` → `PadHierarchy.build` | 9 / 40 | 13 files, +103 / -105 |
| 404ae25 | `build_fill_plan` → `FillPlan.build`、`build_pad_fill_plan` → `FillPlan.for_pad` | 10 / 35 | 6 files, +159 / -156 |
| 251e0fb | `gcode` の 7 ファクトリ → `GCode` の classmethod | 46 / 3 | 26 files, +197 / -207 |

移動後のシグネチャ:

```python
class PadHierarchy:
    @classmethod
    def build(
        cls,
        components: Sequence[Component],
        pads: Sequence[Pad],
        *,
        shape_quantum: float = 0.01,
    ) -> Self: ...

class FillPlan:
    @classmethod
    def build(
        cls,
        polygon: Polygon,
        nozzle_diameter: float,
        *,
        dispense_mode: DispenseMode,
        auto_line_aspect_ratio: float,
        auto_area_short_side_factor: float,
        bead_width_factor: float = 1.0,
        overlap: float = 0.0,
        boundary_margin: float = 0.0,
    ) -> Self: ...

    @classmethod
    def for_pad(
        cls,
        polygon: Polygon,
        *,
        config: PasteDispenserConfig,
        params: PasteParams,
        line_reference: Point2d | None = None,
    ) -> Self: ...

class GCode:
    @classmethod
    def homing(cls, x: bool = False, y: bool = False, z: bool = False) -> Self: ...
    @classmethod
    def move(
        cls,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        velocity: float | None = None,
    ) -> Self: ...
    @classmethod
    def wait(cls, seconds: float) -> Self: ...
    @classmethod
    def wait_for_done(cls) -> Self: ...
    @classmethod
    def present(cls) -> Self: ...
    @classmethod
    def firmware_restart(cls) -> Self: ...
    @classmethod
    def relax(cls) -> Self: ...
```

いずれも本体は無改変で、`return <Class>(...)` を `return cls(...)` に、
戻り値注釈を `Self` に変えただけ。後方互換 alias は作っていない。

## 判断ログ

- **`Self` 戻り値。** 先行分と同じく `typing.Self` を使う。`grouping.py` には
  `from __future__ import annotations` が無いので、クラス本体で自身の名前を
  書ける `Self` が必要（`fill_path.py` / `gcode.py` は future annotations あり
  だが表記を揃えた）。
- **`gcode` module 修飾の import を全廃した。** ユーザーの意図は
  「`from pcbasm.gcode import relax` のような関連の見えない import を抑止する」こと。
  `from pcbasm import gcode` + `gcode.wait_for_done()` は禁止対象ではないが、
  ファクトリが全部 classmethod になると `GCode` を import せざるを得ないので、
  1 ファイル内に `from pcbasm import gcode` と `from pcbasm.gcode import GCode` が
  同居する（`probe.py` と `klipper.py` は元からそうなっていた）。
  同居を避けるため、書き換えた src 側 22 ファイルは `from pcbasm.gcode import GCode`
  に統一し、同ファイル内の `gcode.GCode` 注釈と `gcode.PRESENT_MACRO` も
  直接 import に揃えた。
  `tests/pcbasm/posctrl/test_{position,offset,board,aligner}.py` の
  `from pcbasm import gcode` はファクトリを呼ばず `gcode.GCode` 型注釈だけなので
  そのまま残した（消えたシンボルを指していない）。
- **`PRESENT_MACRO` の位置。** `GCode.present()` から参照するので、module 末尾から
  `GCode` 定義の直前へ移した。公開名・値は不変。
- **テストクラス名の追随。**
  `TestBuildPadHierarchy*` → `TestPadHierarchyBuild*`（4 クラス）、
  `TestBuildPadFillPlan` → `TestFillPlanForPad`、
  `TestHoming` / `TestMove` / `TestWait` / `TestWaitForDone` / `TestPresent` /
  `TestFirmwareRestart` / `TestRelax` → `TestGCode*`。
  gcode 側は関数名自体は method 名として残るが、bare な `TestRelax` が
  module-level 関数を指すように読めるので `GCode` 接頭辞を付けた。
- **docstring 参照。** `fill_path.py` の module docstring と `for_pad` docstring の
  `:func:`build_fill_plan`` / `:func:`build_pad_fill_plan`` を
  `:meth:`FillPlan.build`` / `:meth:`FillPlan.for_pad`` / `:meth:`build`` に更新。
  `src/pcbasm/pasting/README.md` の fill_path.py 行も更新した。
- **循環 import は発生しなかった。** どれも同一 module 内のクラスへの移動で、
  依存の向きは変わっていない。

## 旧名の残存確認

`build_pad_hierarchy` / `build_fill_plan` / `build_pad_fill_plan` および
`gcode.homing` などの module 修飾呼び出しは `src/` `tests/` `scripts/` `docs/` で
grep 0 件。`memory/agents/**` の過去の調査記録には旧名が残るが、当時の記録なので
更新しない（先行分と同じ扱い）。

## 事故ログ

commit 1620c79 の直後、この worktree が別 agent（orchestrator）の cwd 持ち越しで
`main` へ checkout され `git pull --ff-only` された。branch は
`refactor/2026-09-04/pcbasm-namespace-methods-2` に戻され commit も無事だったが、
未コミット変更を抱えていれば失っていた。orchestrator へは通知済み。

## 検証結果

- make format: pass（2 回連続で無変更）
- make type: pass（0 errors）
- make test-no-hardware: pass（2758 passed / 140 deselected。件数は変わらず）
