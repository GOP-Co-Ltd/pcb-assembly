# ノズルキャップ位置（nozzle cap parking）機能の実装計画

## Context

ペーストマシンはノズルが乾いて詰まるため、タスク終了時に「適当に PRESENT して開放」ではなく、**必ずノズルキャップ位置へ移動して終了**する必要がある。移動手順は「Z を 0 へ → キャップ XY へ → キャップ Z へ」（Homing は printer.cfg の homing_override で z→x→y のため起動時は安全、変更不要）。これはペーストマシン特有の動作で、将来の PnP マシンでは不要 → `machine.toml` に `machine_type` キーを導入して切り替える。

**ユーザー確定事項**：
- キャップ到達後は **M84 で脱力**する
- `machine_type` は**必須キー**（欠落 config はアクセス時 KeyError、不正値は ValueError）。既存 config はすべて `"paste"` を追記
- paste でキャップ未記録なら**警告して PRESENT にフォールバック**
- タスク終了時のキャップ移動は **PRESENT を完全置換**（キャップ位置は XYZ 丸ごと記録するので、Y を手前にして記録すれば基板取り出しも両立）

作業ブランチ: `feature/20260708/nozzle-cap-parking`（作成済み）。実装はエージェントチーム（spec-test-author × plan-implementer 並列）で行い、最後に gitlab-mr で MR を出す。

## 主要設計判断

| 判断 | 決定 | 根拠 |
|---|---|---|
| 終了時退避ロジックの置き場所 | 新モジュール `src/pcbasm/parking.py` の `park_or_present()` | HAL に config を渡すのは層違反、session.py は posctrl を import しており循環。config と hal に依存できる小モジュールが正解 |
| gcode ビルダー | `gcode.py` に純関数 `move_to_cap(x, y, z, *, velocity)`（float 引数） | gcode.py は config クラスを import しない現状を維持。終了時（+M400+M84）と手動移動（+M400）で共用 |
| XYZStage.move は使わない | 既存プリミティブ `gcode.move()` で合成（生文字列ではない） | `XYZStage.move` は未指定軸を**ビルド時点の現在位置で埋めて常に XYZ 全軸入りの G1 を生成**する（stage.py L222-239）。3 段シーケンスを組むと中間の XY 移動が `G1 X{cap} Y{cap} Z{旧Z}` になり「Z を先に 0 へ」が壊れる。安全に使うには 1 セグメントごとに send+M400 待ち（HTTP 3 往復ブロッキング）が必要で終了時経路に不適。`gcode.move()` は指定軸のみ出力するため `G1 Z0`→`G1 X Y`（Z ワードなし）→`G1 Z` が正しく作れる。可動域の最終バックストップは Klipper 本体（範囲外 G1 は "Move out of range" で send_gcode が例外化）。キャップ位置自体はジョグ（stage.move 経由で limits 検証済み）から記録される |
| 速度 | 20 mm/s（F1200）= PRESENT マクロと同速。`CAP_PARK_VELOCITY = 20.0` | 無人終了動作は実績速度を踏襲 |
| 手動「キャップ位置に移動」 | M84 **しない**（移動のみ） | 脱力すると homed が消え再ホーミングが必要。Relax ボタンが別にある |
| `Machine.nozzle_cap` 未定義時 | `NozzleCap \| None`（None 返却） | 「未記録が正常状態」+ refactor-conventions の None 返却原則 |
| クリーンアップ経路での machine_type 欠落/不正 | warn + `send_present_or_relax` フォールバック（例外にしない） | 3 呼び出し箇所すべて `__exit__`/finally/best-effort 経路。例外は元例外をマスクする |
| ジョブ中 WS ミラー | `machine_commands.py` にも `move_to_cap` を追加 | machine_control.js はジョブ中の操作を全部 WS に流すため、未対応だと「未知コマンド」で UX 破綻 |
| configs/test-fixture に `[nozzle_cap]` | **入れない** | conftest が test-fixture を 2 マシンにコピーするため、入れると `TestPresentOnTermination` の PRESENT/M84 ログ断言が全滅。必要なテストだけ toml へ追記 |
| `machine_type` の設定ページ編集 | ホワイトリストに**入れない** | マシン構造を規定するキーで UI から誤変更させない |

## 公開インターフェース（シグネチャ確定）

### `src/pcbasm/config.py`
```python
MACHINE_TYPES = ("paste", "pnp")
MachineType = Literal["paste", "pnp"]

@attrs.frozen
class NozzleCap:
    """ノズルキャップ位置の設定（マシン座標 [mm]）."""
    x: float
    y: float
    z: float

class Machine:
    @property
    def machine_type(self) -> MachineType: ...
        # トップレベルスカラーなので self._data から直接読む
        # 欠落 → KeyError / paste・pnp 以外 → ValueError
    @property
    def nozzle_cap(self) -> NozzleCap | None: ...
        # "nozzle_cap" not in self._data → None
```

### `src/pcbasm/gcode.py`
```python
CAP_PARK_VELOCITY = 20.0  # [mm/s] PRESENT マクロの F1200 と同速

def move_to_cap(x: float, y: float, z: float, *, velocity: float = CAP_PARK_VELOCITY) -> GCode:
    # G90 + move(z=0.0) + move(x=,y=) + move(z=)  ※M400/M84 は含めない
```

### `src/pcbasm/parking.py`（新規）
```python
def park_or_present(
    klipper: Klipper,
    machine: Machine,
    *,
    warn: Callable[[str], None] | None = None,
    timeout: float = PRESENT_TIMEOUT,
) -> None:
```
挙動: ① machine_type 欠落/不正 → warn（"machine_type" を含む文言）+ send_present_or_relax ② `!= "paste"` → send_present_or_relax ③ cap 未記録 → warn（"nozzle_cap" を含む文言）+ send_present_or_relax ④ paste + cap → `send_gcode(move_to_cap(...) + wait_for_done() + relax(), timeout=timeout)` を **1 回の send_gcode** で送る。

### 呼び出し 3 箇所の差し替え
- `src/pcbasm/session.py` `PasteSession.__exit__` → `park_or_present(self.klipper, self.machine)`（docstring も更新）
- `src/pcbasm/posctrl/setup.py` `machine_session(klipper)` → **`machine_session(klipper, machine)` に破壊的変更**（呼び出しは posctrl/__init__ 再エクスポートとテストのみ。src/scripts/ に .py なし）
- `src/webui/jobs/manager.py` `_present_machine` → `_park_machine` にリネームし `park_or_present(klipper, context.machine, warn=runtime.log, timeout=PRESENT_TIMEOUT)`。catch-all 文言は「タスク終了時の退避に失敗: {exc}」（**"PRESENT"/"M84" の語を入れない** — ログ断言の一意性のため）

### WebUI
- `src/webui/config_store.py`: `MACHINE_FIELDS` に `nozzle_cap.x/y/z`（float, mm）を追加
- `src/webui/routers/common.py`: `SECTION_LABELS["nozzle_cap"] = "ノズルキャップ"`
- `src/webui/state.py`: `machine_type(self) -> str | None`（focus_z() と同じ broad except → None パターン）
- `src/webui/routers/machine_control.py`: action Literal に `"move_to_cap"` 追加。`_build_gcode` に `case "move_to_cap":` — `state.machine().nozzle_cap` が None なら `ValueError("ノズルキャップ位置が未記録です")`（→400）、あれば `gcode.move_to_cap(...)`（共通末尾の `+ wait_for_done()` が M400 を付与）
- `src/webui/jobs/machine_commands.py`: `case {"type": "move_to_cap"}:` — `ctx.machine.nozzle_cap` None なら log のみ、あれば `send_gcode(move_to_cap + wait_for_done)`
- `src/webui/routers/nozzle_cap.py`（新規）: `POST /api/pasting/nozzle-cap/record`
  1. `machine_lock("nozzle-cap-record")`（外側）→ `klipper_errors_to_502()`（内側）→ `create_klipper(state, 10.0)`
  2. `homed_axes` に xyz が揃わなければ 400「全軸ホーミング後に記録してください」（M84 後の stale 座標記録防止）
  3. `XYZStage(klipper.readonly).get_position()` → 3 桁丸めで `ConfigStore.write_machine_settings`（machine_lock 内・request 経路パターン）
  4. ロック解放後 `publish_state_changed()` → 保存値 `{"x","y","z"}` を返す
- `src/webui/app.py`: nozzle_cap router を登録
- `src/webui/routers/pages.py`: `TABS["pasting"]` に `"nozzle_cap"` 追加 / `FEATURE_LABELS["nozzle_cap"] = "ノズルキャップ位置の設定"` / `FEATURE_TEMPLATES[("pasting","nozzle_cap")] = "pasting/nozzle_cap.html"`（非ジョブページ）/ `_base_context` に `"machine_type": state.machine_type()` 追加 / `_FEATURE_CONTEXT["nozzle_cap"]` に現在値を渡す provider
- `src/webui/templates/pasting/nozzle_cap.html`（新規）: 説明＋手順＋現在値表示（None なら「未記録」）＋記録ボタン
- `src/webui/static/js/nozzle_cap.js`（新規・thin）: 記録ボタン → POST → 返却値で表示更新 + toast のみ
- `src/webui/templates/partials/machine_control.html`: `{% if machine_type == "paste" %}` で「キャップ位置に移動」ボタン（`#mc-move-to-cap`）
- `src/webui/static/js/machine_control.js`: null ガード付きで `sendControl({action: "move_to_cap"})`（ジョブ中 WS/アイドル REST の振り分けは既存 sendControl が処理）

## G-code 列（正確な合成）

タスク終了時（paste + cap 記録済み、1 回の send_gcode、timeout=30s）:
```
G90
G1 Z0.0 F1200.0
G1 X{cap.x} Y{cap.y} F1200.0
G1 Z{cap.z} F1200.0
M400
M84
```
中間 M400 は不要（Klipper のモーションキューが順序保証）。M400 は M84 直前の 1 箇所のみ。手動移動は M84 を除いた列。`move()` は `f"X{x}"` 生出力のため `Z0.0`/`F1200.0` 書式（テストの厳密比較に反映）。

**注: `XYZStage.move` は意図的に使わない。** 同メソッドは未指定軸をビルド時の現在位置で埋めて常に全軸入り G1 を返すため、3 段合成すると中間セグメントが `G1 X{cap} Y{cap} Z{旧Z}` となり Z 先行の意味が壊れる（回避には 1 段ごとの send+M400 待ちが必要）。既存プリミティブ `gcode.move()`（指定軸のみ出力）で合成し、範囲外はKlipper 本体の "Move out of range" エラーがバックストップになる。

## 設定ファイル編集

先頭（`[klipper]` の前）に `machine_type = "paste" # マシン種別: paste / pnp` を追加:
- `configs/kurousagi/machine.toml` / `configs/pd_china_frame/machine.toml` / `configs/test-fixture/machine.toml` / `data/testing/machine.toml`
- `data/testing/machine_minimal.toml` は**変更しない**（machine_type 欠落 KeyError テストの素材）
- `configs/*/printer.cfg` の PRESENT マクロは削除しない（pnp・フォールバックで使用）
- kurousagi / pd_china_frame に `[nozzle_cap]` は追加しない（実機でユーザーが記録する）

## API 契約

| エンドポイント | 成功 | エラー |
|---|---|---|
| `POST /api/pasting/nozzle-cap/record` | `200 {"x","y","z"}`（3 桁丸め保存値） | 409 busy / 502 Klipper 不達 / 400 全軸未ホーミング |
| `POST /api/machine-control` `{"action":"move_to_cap"}` | `200 KlipperStatus`（既存契約） | 400 キャップ未記録 / 409 / 502 |
| `GET/PUT /api/settings/machine` | fields に `nozzle_cap.x/y/z` 追加（未記録は value=None） | 既存契約 |
| job WS `{"type":"move_to_cap"}` | 移動実行。未記録ならログのみで消化 | 既存契約 |

## テスト一覧（spec-test-author 担当、tests/ のみ）

- `tests/pcbasm/test_config.py`: `TestMachineType`（paste 読取 / minimal.toml 欠落→KeyError / 不正値→ValueError）、`TestNozzleCap`（欠落→None / 読取）
- `tests/pcbasm/test_gcode.py`: `TestMoveToCap`（`["G90","G1 Z0.0 F1200.0","G1 X10.0 Y20.0 F1200.0","G1 Z3.5 F1200.0"]` 厳密一致・M400/M84 非含有 / velocity=30.0→F1800.0）
- `tests/pcbasm/test_parking.py`（新規）: `TestParkOrPresent` — klipper は mocker.Mock（自前 HAL）、Machine は tmp_path の実 toml。paste+cap→send_gcode 1 回・末尾 M400,M84・fallback 不呼出 / cap なし→warn に "nozzle_cap"+fallback / pnp→fallback / machine_type 欠落・不正→warn に "machine_type"+fallback
- `tests/pcbasm/posctrl/test_setup.py`: `TestMachineSession` を 2 引数化へ改修（park_or_present が正常/例外時とも 1 回呼ばれ例外は伝播、を mocker.patch でピン）
- `tests/webui/test_config_store.py`: `TestNozzleCapFields`（欠落→None / write→reread round-trip）
- `tests/webui/routers/test_machine_control.py`: `TestMoveToCap`（未記録→400 / toml に追記後→502 =バリデーション通過の証明）
- `tests/webui/routers/test_nozzle_cap.py`（新規）: `TestRecordNozzleCap`（7126 不達→502 / busy→409 / `@mark_hardware` で記録永続化・relax 後 400）
- `tests/webui/routers/test_pages.py`: `TestNozzleCapPage`（サイドバー表示 / 記録ボタンあり・job-console なし / 「未記録」表示）、`TestMachineControlCapButton`（paste で表示 / machine_type 行削除で 500 にならずボタン非表示）
- `tests/webui/jobs/test_manager.py`: `TestPresentOnTermination` 既存 3 テストは fixture が cap 未記録のためフォールバックで green のはず（要確認）。追加: cap 追記後→ログに "退避に失敗" を含み "PRESENT" を含まない
- `tests/webui/jobs/test_machine_commands.py`: move_to_cap 未記録→log+True / 追記後 7126 不達→例外伝播
- `tests/e2e/`: settings 応答に `nozzle_cap.x` / `GET /pasting/nozzle_cap` 200 + 記録ボタン（軽く）

## リスク・注意点

1. conftest は test-fixture を kurousagi/test-fixture の 2 マシンにコピー → `machine_type = "paste"` 追加で全 webui テストが paste 経路になるが、`[nozzle_cap]` を repo fixture に入れない限りフォールバックで既存断言は維持
2. `Klipper.get_config/get_macros` は functools.cache（インスタンス毎）→ 終了経路は毎回新規 Klipper を作る現行実装を維持
3. 記録エンドポイントの 502 は書込み前に発生、machine_lock は with で解放。`klipper_errors_to_502` は machine_lock の**内側**（BusyError を 502 に巻き込まない、machine_control.py と同順）
4. `tests/webui/routers/test_settings_api.py` の fields 断言は MACHINE_FIELDS 参照のため自動追従（要確認のみ）
5. 駐機は M400 込みで HTTP がブロック → PRESENT_TIMEOUT=30s で十分（20mm/s × 可動域）
6. ジョブ中 WS の `ctx.machine` はジョブ開始時 snapshot → ジョブ中に記録した cap は次ジョブから有効

## 実行フロー（エージェントチーム）

1. ✅ ブランチ `feature/20260708/nozzle-cap-parking` 作成済み
2. implementation-planner: 本計画を `memory/agents/implementation-planner/nozzle-cap-parking.md` に固定化
3. **spec-test-author × plan-implementer を並列起動**（公開 IF はシグネチャ確定済み。tests/ と src/+configs/ で disjoint。fixture toml 編集は implementer 側）
4. 合流: `make format && make type && make test-no-hardware` — spec の意図どおり pass/fail か確認、不整合は該当 agent 再呼び出し
5. code-simplifier → 再検証
6. E2E: `make test-e2e`（skill webui-e2e 参照）
7. docs-keeper（README/docstring 最小保守）
8. コミット（feat(pcbasm)/feat(webui) 等に分割、1 コミット 1 関心事）→ skill gitlab-mr で MR 作成

## 検証

- `make format && make type && make test-no-hardware`（ハードウェアなし全テスト）
- `make test-e2e`（実 uvicorn の HTTP/WS 通し）
- `make webui-fake` で塗布タブ「ノズルキャップ位置の設定」ページと操作パネルのボタン表示をブラウザ確認（Claude 自身で実サーバー E2E まで行う）
- **実機確認はユーザー残**: ①実機でキャップ位置を記録 ②タスク終了時に Z0→XY→Z→脱力 の駐機動作 ③「キャップ位置に移動」ボタン ④`@mark_hardware` テスト
