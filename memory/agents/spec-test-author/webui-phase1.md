# WebUI Phase 1 仕様テスト

入力: `memory/agents/implementation-planner/webui-phase1.md`（IF 契約）+ `docs/webui/specification.md` §5/§6/§8/§9/§11。
`tests/webui/` は `src/webui/` を 1 対 1 ミラー。`configs/test-fixture/` も本タスクで新設（取り決めどおり spec-test-author 側で作成）。

## 作成したフィクスチャ

- `configs/test-fixture/machine.toml` — kurousagi ベース、`[klipper] port = 7126`（非リッスン）、コメント多数（コメント保持テスト素材）。`bead_width_factor` / `boundary_margin` は**意図的に未定義**（欠落キー → None の素材）。`pcbasm.config.Machine` でロード可能なことを確認済み
- `configs/test-fixture/printer.cfg` — kurousagi から `[printer]` / `[stepper_x/y/z]` / `[manual_stepper paste_dispenser]` を抜粋、コメント付き
- `configs/test-fixture/ov9281_test_fixture.json` — CalibrationResult 形式、`z_position: -25.0`。`CalibrationResult.load` 互換を確認済み

## 共有 fixture（tests/webui/conftest.py）

- `configs_root` — tmp_path に test-fixture を **"kurousagi" と "test-fixture" の 2 マシン**としてコピー（既定選択を計画書どおり kurousagi にしつつ、両方 port 7126 で実機 Moonraker に触れない）
- `webui_settings` / `app` / `client`（lifespan 込み TestClient）/ `appstate`（`app.state.appstate`、ロック直接取得用）
- `pcb_root` — boards/sample.kicad_pcb・notes.txt・docs/・top.kicad_pcb・root 外に outside.kicad_pcb
- `real_settings` / `real_client` — 実 configs（kurousagi, port 7125）。`@mark_hardware` 専用、ユーザー実行

## 書いたテスト一覧（91 non-hardware + 5 hardware）

- `tests/webui/test_settings.py::TestSettingsFromEnv` — 既定値 / env 上書き / 非数値 PORT → ValueError
- `tests/webui/test_config_store.py`
  - `TestListMachines` — machine.toml を持つ dir のみ sorted
  - `TestMachineSettings` — 型どおり読出 / 全ホワイトリストキー網羅 / 欠落キー None / 書込反映 / 変更対象外行 byte 同一 / 値行 inline コメント保持 / 欠落キーの追加書込 / 未知キー・型不一致・int 項目に 5.5 → UnknownFieldError / 未知マシン → FileNotFoundError
  - `TestMotionSettings` — printer.cfg 読出（max_velocity 50 等）/ 対象行のみ書換 / 対象行なし → UnknownFieldError / 未知キー
  - `TestSymlinkPointsTo` — 真 / 他マシン指し → 偽 / リンク不存在 → 偽
- `tests/webui/test_state.py`
  - `TestMachineSelection` — default_machine 初期選択 / default 不在は list_machines()[0] / 永続化と復元 / 未知 → ValueError / 壊れ JSON・未知マシン入り state file → フォールバック
  - `TestPcbSelection` — 相対パス保存と復元 / root 外・拡張子・不存在 → ValueError
  - `TestMachineLock` — busy_owner 設定解除 / 競合 BusyError(owner) / 解放後再取得 / ロック中 select_machine・select_pcb → BusyError
  - `TestMachineConfig` — machine().klipper.port == 7126 / focus_z == -25.0 / calibration 欠落・z_position null → None
- `tests/webui/routers/test_pages.py::TestPages` — `/` → 307 `/posctrl` / 4 タブ 200 + E-STOP・machine-control・mainsail_url マーカー / `/posctrl/reference_point_setup` / `/settings` / 未知タブ・feature → 404
- `tests/webui/routers/test_machine.py` — `TestStateApi`（全フィールド、ロック中 busy/busy_owner）/ `TestMachineApi`（一覧・GET・PUT 切替・未知 404・busy 409 + owner）
- `tests/webui/routers/test_files.py` — `TestFilesApi`（dir + *.kicad_pcb のみ・名前順・サブ dir・traversal 400・不存在 404）/ `TestPcbFileApi`（state 反映・拡張子 400・root 外 400・不存在 404・busy 409）
- `tests/webui/routers/test_settings_api.py` — `TestMachineSettingsApi`（全項目 GET・欠落 None・PUT 実ファイル反映 + コメント保持・未知 400・busy 409）/ `TestMotionSettingsApi`（GET 値 + symlink_ok 偽/真・restart なし PUT・**restart=True 不達 → 200 + restart_ok=false + restart_error**・未知 400・busy 409）
- `tests/webui/routers/test_machine_control.py` — 400（jog axis/distance 欠落、focus_z z_position なし〔tmp の calibration JSON 差替〕）/ 409（detail に owner）/ 502（home・relax、port 7126 実接続）/ `@mark_hardware`: relax 200・relax 後の未ホーミング jog 502（M84 が homed をクリア）・home → jog ±0.1 往復・limits 超過 move 400
- `tests/webui/routers/test_system.py` — status 不達 → 200 + connected=false + error / E-STOP 不達 → 502 / **E-STOP はロック非経由**（busy 中でも 409 にならず 502 まで到達）/ `@mark_hardware`: 実 status の position・homed_axes
- `tests/webui/test_app.py::TestCreateApp` — create_app 起動 + /api/state / BusyError → 409 JSON / /static/app.css 配信

3rd-party（httpx / tomlkit / Moonraker）のモックなし。Moonraker 不達系は port 7126 への実接続。`tests/helpers.py` への追加なし（Phase 1 は HAL fake 不要）。

## 期待される失敗

なし。plan-implementer の並行実装が完走しており、**91 non-hardware テスト全 pass**（hardware 5 件は deselect、ユーザー実行待ち）。

## IF変更通知 / 計画書で未確定だった点のピン留め（実装と合意済みの挙動）

1. `ConfigStore` の未知マシン → **FileNotFoundError** をピン（計画書は「適切な例外」のみ）
2. `AppState.select_pcb` は **pcb_browse_root からの相対 Path** を受ける前提でテスト
3. `GET /api/files` はテスト側で常に `path` クエリを明示（root は `path=""`）。entries は dir/file 混在で名前順をピン
4. タブページのマシン操作パネルマーカーは文字列 `"machine-control"`（spec §11 E2E の grep と同一）
5. `MotionUpdateResult` の restart=False 時は `restart_requested is False` のみピン（`restart_ok` の値は未規定のため非検証）
6. int 項目（blur_ksize）への float は **非整数値 5.5 のみ** UnknownFieldError をピン（5.0 の受理可否は未規定のまま）
7. hardware: `test_jog_unhomed_returns_502` は relax（M84）が Klipper の homed 状態をクリアする性質を前提

## 実装側に求める修正

なし（全テスト green）。

## 検証結果

- `uv run pytest tests/webui -m "not hardware" -q` — **91 passed, 5 deselected**
- `uv run pyright tests/webui` — 0 errors
- `uv run pre-commit run --files <作成ファイル>` — 全フック pass（docformatter の整形適用済み）
- `@mark_hardware` 5 件（machine_control 4 + system 1）はユーザーが実 Moonraker で実行
