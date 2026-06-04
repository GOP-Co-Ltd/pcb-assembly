# Path / Motion 再設計 — 全体地図

段階的に進める再設計の「迷子防止用」一枚地図。各フェーズの実装が進んでもここを見れば目標形と現在地が分かる。

## 目的

`geometry/trajectory.py` が **経路（純幾何）と運動（速度・タイミング）を融合**していたのを解く。これが `pasting`/`posctrl` の見通しの悪さの根本だった。

- before: `Move.v` → `Trajectory`(可変builder) → `Waypoint.v` → `G1 F<v>`。applicator は経路長のためにダミー速度で Trajectory を組み、`with_velocity()` で全点を作り直していた。
- after: geometry は速度を持たない `Path`。速度は `Speed` 値オブジェクトで hal 境界に渡す。塗布の時間調停は pasting の `FillSequence` が持つ。送信は従来どおり `Klipper` のみ。

## レイヤ境界

```
geometry（純幾何 / 速度なし）   hal（汎用ハード抽象）              pasting（塗布ドメイン）        送信
──────────────────────       ─────────────────────────       ──────────────────────       ──────
Path(points)                  Speed.absolute / Speed.rate      FillSequence                  Klipper
  .length()                   XYZStage.to_gcode(path, speed)     .to_gcode(stage, dispenser)   .send_gcode
  .transformed(t)             XYZStage.move(x,y,z, speed,         接近→下降→prime同期吐出       （唯一の
sort_by_nearest (routing)       relative)                          →リトラクト→上昇 を1本に       送信者）
                              Limits.contains(point, feed)
                              PasteDispenser.pushpull(...)
```

## 責務マップ

| 関心事                        | 置き場所               | 理由                               |
| ----------------------------- | ---------------------- | ---------------------------------- |
| 経路（順序付き点列）・距離    | `geometry.Path`        | 純粋な空間の幾何                   |
| 巡回最適化 `sort_by_nearest`  | `geometry.routing`     | 同上（速度と無関係）               |
| 速度の表現（絶対/割合）       | `hal.Speed`            | max_velocity に対する解決が必要    |
| 部分/相対座標の解決・制限検証 | `hal.XYZStage`         | 現在位置・可動域を知るのは stage   |
| prime/dispense の時間調停     | `pasting.FillSequence` | 塗布ドメインの知識（hal の責務外） |
| G-code の送信                 | `hal.Klipper` のみ     | 単一 I/O 点・アトミック送信        |

## velocity flow（before → after）

```
before:  Move(v) ─┐
                  ├→ Trajectory(可変, 相対解決, 速度state) → Waypoint(v) → stage.to_gcode → G1 F<v>
  applicator: ダミー initial_velocity=1.0 で距離取得 → with_velocity(length/dispense_time) で全点再構築

after:   Path(速度なし) ──→ FillSequence が feed = Speed.absolute(length/dispense_time) を算出
                          └→ stage.to_gcode(path, speed) が speed.resolve(max_velocity) → G1 F<feed>
         単発移動:  stage.move(x,y,z, speed=Speed.absolute(v) | Speed.rate(0..1))
```

## 目標 API（確定シグネチャ）

```python
# geometry/path.py
@attrs.frozen
class Path:
    points: tuple[Point3d, ...] = attrs.field(converter=tuple)
    def length(self) -> float                       # 多角線長。<2点で0.0
    def transformed(self, transform: Transform) -> Path
    # __len__ / __iter__ / __getitem__

# hal/speed.py
@attrs.frozen
class Speed:
    @classmethod
    def absolute(cls, mm_per_s: float) -> Self
    @classmethod
    def rate(cls, fraction: float) -> Self          # 0<=fraction<=1
    def resolve(self, max_velocity: float) -> float

# hal/stage.py (XYZStage)
def to_gcode(self, path: Path, *, speed: Speed) -> gcode.GCode
def move(self, x=None, y=None, z=None, *, speed: Speed | None = None,
         relative: bool = False) -> gcode.GCode     # speed=None → max
# Limits.contains(point: Point3d, feed: float) -> bool

# pasting/fill_sequence.py
@attrs.frozen
class FillSequence:
    # path + 吐出量/レート/prime_time/lift/travel_speed
    def fill_speed(self) -> Speed | None            # length/dispense_time（0長でNone）
    def to_gcode(self, stage: XYZStage, dispenser: PasteDispenser) -> gcode.GCode
```

## フェーズ進捗

expand → migrate → contract。各段階を `make format && make type && make test-no-hardware` で緑に。merge と実機検証はユーザー。

- [x] **P0** 本ドキュメント（全体地図）
- [x] **P1** 基盤値オブジェクト追加（`geometry.Path` / `hal.Speed`）— 加算的・無破壊
- [x] **P2** `XYZStage.move` + `Speed` 追加、単点呼び出しを `to_gcode(Move...)` → `stage.move(...)` に移行（legacy `to_gcode` は applicator に隔離）
- [x] **P3** `pasting.FillSequence` 新設、`to_gcode(path, speed)` 最終形に置換、`applicator._fill` 書き換え、`Trajectory`/`Move`/`Waypoint` 削除、`sort_by_nearest` を `routing.py` へ移設
- [ ] **P4**（別フェーズ）スクリプトのセッション化（`PasteRun`/`MachineSession`）で配線重複を吸収

## スコープ外（非ブロッキング）

- `pasting/fill_path.py` の螺旋再帰（複雑な幾何アルゴリズム自体の問題、別トラック）
- transform `Compose` の合成順序の不透明さ（velocity と直交、別途）
