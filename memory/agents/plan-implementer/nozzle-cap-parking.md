# ノズルキャップ位置（nozzle-cap-parking）実装ノート

計画書: `memory/agents/implementation-planner/nozzle-cap-parking.md`（公開 IF はシグネチャどおり実装）

## 実装した内容

- toml: `configs/kurousagi|pd_china_frame|test-fixture/machine.toml`・`data/testing/machine.toml` の先頭（[klipper] の前）に `machine_type = "paste" # マシン種別: paste / pnp`。`machine_minimal.toml` は未変更。`[nozzle_cap]` はどの toml にも追加していない
- `src/pcbasm/config.py`: `MACHINE_TYPES` / `MachineType` / `NozzleCap`（attrs.frozen, x/y/z float）/ `Machine.machine_type`（欠落 KeyError・不正 ValueError）/ `Machine.nozzle_cap`（欠落 None）
- `src/pcbasm/gcode.py`: `CAP_PARK_VELOCITY = 20.0` / `move_to_cap(x, y, z, *, velocity=CAP_PARK_VELOCITY)` — `GCode("G90") + move(z=0.0) + move(x=,y=) + move(z=)`。XYZStage.move は不使用（計画書「G-code 列」の注のとおり）
- `src/pcbasm/parking.py`（新規）: `park_or_present(klipper, machine, *, warn=None, timeout=PRESENT_TIMEOUT)` — 欠落/不正 warn（"machine_type" 含む）→fallback、pnp→fallback、cap 未記録 warn（"nozzle_cap" 含む）→fallback、paste+cap→ move_to_cap + M400 + M84 を 1 回の send_gcode
- 呼び出し差し替え: `session.py PasteSession.__exit__`（docstring 更新）/ `posctrl/setup.py machine_session(klipper, machine)` 2 引数化（posctrl/__init__ の再エクスポートは名前不変で影響なし）/ `webui/jobs/manager.py _present_machine → _park_machine`（catch-all「タスク終了時の退避に失敗: {exc}」、"PRESENT"/"M84" 非含有）
- WebUI: `config_store.py` に nozzle_cap.x/y/z（float, mm、camera セクションの前に配置）/ `common.py SECTION_LABELS["nozzle_cap"]` / `state.py machine_type()`（broad except → None）/ `machine_control.py` action "move_to_cap"（未記録 ValueError→400）/ `machine_commands.py` case "move_to_cap"（未記録 log のみ）/ `routers/nozzle_cap.py`（新規 POST /api/pasting/nozzle-cap/record、machine_lock 外側・klipper_errors_to_502 内側・homed 検査 400・3 桁丸め・ロック解放後 publish_state_changed）/ `app.py` 登録 / `pages.py`（TABS 末尾に nozzle_cap・FEATURE_LABELS・FEATURE_TEMPLATES・_base_context に machine_type・_FEATURE_CONTEXT）/ `templates/pasting/nozzle_cap.html` / `static/js/nozzle_cap.js`（fetch/DOM のみ）/ `partials/machine_control.html` に `{% if machine_type == "paste" %}` の `#mc-move-to-cap` / `machine_control.js` に null ガード付きリスナー

## 計画外の判断ログ

1. **machine_type 不正時の ValueError 文言に "machine_type" を含めた**: `f"未知のマシン種別です: machine_type={value!r}"`。spec-test-author のテスト（`pytest.raises(ValueError, match="machine_type")`）が文言にキー名を要求しており、エラーがキー名を名指しするのは仕様として妥当と判断して実装側を修正（テストは未編集）
2. **【重要・事故】plain pytest で @mark_hardware テストを誤実行**: マーカー除外なしの `pytest tests/webui/...` により実機（kurousagi, port 7125）へ home / jog / relax / record が送信され、実機が動作した。副作用として record テストが `configs/kurousagi/machine.toml` に `[nozzle_cap]` x=0/y=0/z=0 を書き込んだ。計画書は「kurousagi に [nozzle_cap] は追加しない」と明記しているため**このハンクは除去済み**（現 diff は machine_type 追加のみ）。以後の検証は `-m "not hardware and not e2e"` のみで実施。**実機の現在状態（ホーミング/位置）はユーザー確認が必要**
3. nozzle_cap.py のタイムアウトは計画書の 10.0 を `STATUS_TIMEOUT = 10.0` 定数として定義（machine_control.py の MOVE_TIMEOUT と同じ流儀）
4. MACHINE_FIELDS 内の nozzle_cap.x/y/z の配置は reference_point.offsets の後・camera の前（計画書は位置未指定。settings ページのセクション順で幾何系設定に隣接させた）

## 他implementerへのIF変更通知（並列時）

なし（計画書のシグネチャから逸脱なし）

## spec-test-author への注意点

- 実機で先に動かした際、`TestMachineControlHardware::test_jog_unhomed_returns_502`（400 が返る）と `test_home_then_jog_round_trip`（jog が 400）が失敗した。ジョグ経路は `XYZStage.move`（limits 検証で ValueError→400）を通るため、unhomed 時に 502 になる仮定は実機の状態次第で崩れる可能性がある。ただし私の誤実行時は実機状態が不定だったため、断定はできない。ハードウェアテストはユーザー実行時に要確認
- それ以外の非ハードウェアテストは全パス（下記）

## 既知の制約・残課題

- 実機確認はユーザー残（計画書どおり）: キャップ位置の記録 → タスク終了時の駐機動作 → 「キャップ位置へ」ボタン → @mark_hardware テスト
- 誤実行の影響で実機が home 済み・原点付近に移動した可能性あり。次の実機操作前に状態確認を推奨

## 検証結果

- make format: pass
- make type: pass（pyright 0 errors）
- pytest -m "not hardware and not e2e": **1500 passed, 76 deselected**（make test 相当のフルグリーン化は合流フェーズ。ハードウェア/e2e はユーザー・合流時実行）
