# WebUI Phase 5: pasting ジョブ + 仕上げ

計画書 `memory/agents/implementation-planner/webui-phase5.md` を全レーン（A〜D）実装。
ユーザー決定（判断保留点 1〜4 は計画どおり）を反映。spec-test-author と並列実行し、
先方テスト（`tests/webui/jobs/test_pasting.py` ほか）全件に実装が整合
（先方メモ「実装側への修正要求: なし」）。

## 実装ファイル

- pcbasm: `geometry/sampling.py`（SamplingDiagnostics / sampling_diagnostics）、
  `visualization/height_render.py` 新設（render_height_plane / render_planned_points）、
  `pasting/applicator.py`（`PasteApplicator.from_config`）、`session.py`
  （make_applicator が from_config へ委譲）、各 `__init__.py` export
- scripts 追従: `height_plane.py`（可視化/診断を pcbasm 昇格版へ）、`loading.py` /
  `flow_calibration.py` / `toolhead_offset.py`（from_config 化）
- webui: `jobs/machine_commands.py` 新設（COMMAND_TIMEOUT / create_command_klipper /
  handle_machine_command）、`jobs/pasting.py` 新設（6 ジョブ + LOADING_STAGE +
  parse_loading_command）、`jobs/posctrl.py` 委譲リファクタ、`jobs/catalog.py`
  （default_catalog 15 件）、`jobs/manager.py`（pcbasm ログブリッジ）、
  `routers/pages.py`（pasting 6 feature + show_preview / loading コンテキスト）、
  `templates/pasting/job.html` + `templates/partials/loading_controls.html`、
  `static/js/job_console.js`（progress の listener 通知 + /artifacts/ ログリンク化）、
  `static/js/loading_controls.js`、`app.css`

## 計画外の判断ログ

1. **probe_gnd_down_adjust の ProbeGround 構築を遅延化**（計画書 §「ジョブ実装」は
   step 1 で構築としていたが、`ProbeGround` → `Servo.__init__` が
   `klipper.get_config()` で即時接続するため、Klipper 不通だと最初の prompt 前に
   FAILED してしまう。計画書 §4 のテスト仕様（prompt 往復後に FAILED）が正であり、
   初回送信時に遅延構築するクロージャ `make_ground()` に変更。finally の down(0)
   も同経路（構築失敗は「ダウン距離 0 への復帰に失敗」log になる）
2. **manager ログブリッジで pcbasm logger の level を一時的に INFO へ引き下げ**
   （計画書は handler attach のみ言及）。root 既定 WARNING のままでは INFO record が
   logger 段で落ちて handler に届かないため。effective level > INFO の場合のみ
   setLevel(INFO) し、finally で復元。ジョブは同時 1 本なので競合なし
3. posctrl の `_dispatch_reference_command`: 委譲後の `position.invalidate()` は
   「handle_machine_command が True を返した場合のみ」に変更（旧実装は未知
   コマンドでも invalidate していたが、キャッシュ更新のみで公開挙動は不変）
4. E2E の height_plane は **fill_coverage ではなく led_blinker を使用**
   （fill_coverage は TOP 銅箔ゾーンなしでサンプリング不能。spec-test-author の
   発見と同一。計画書 §5 step 3 の差し替え）

## 他 implementer への IF 変更通知

- `webui/jobs/posctrl.py` の `COMMAND_TIMEOUT` / `_create_klipper` は
  `webui/jobs/machine_commands.py` へ移動（`COMMAND_TIMEOUT` /
  `create_command_klipper` として公開）。posctrl 内の旧シンボルは削除済み。
  外部参照は無いことを grep で確認済み
- それ以外の公開 IF は計画書どおり（逸脱なし）

## 既知の制約・残課題

- **flow_calibration の prompt(number) 往復と loading の command 駆動ループは
  Klipper 不通ではスモーク不可**（`PasteApplicator.__enter__` の enable 送信で
  即 FAILED するため、ループ／質量 prompt に到達しない）。prompt(number) の
  往復機構自体は probe_gnd_down_adjust の E2E（-1 → 再 prompt → 1.5 → FAILED）で
  検証済み。ループ内挙動は spec-test-author の `@mark_hardware` テスト +
  計画書 §5 実機確認項目 1・2 でカバー（ユーザー実行）
- Apply の実書込はジョブ SUCCEEDED が前提のため pasting ジョブ経由では不可。
  代替として反映先 4 値（rotations_per_ul / toolhead.x,y / down_distance）を
  settings API で configs/test-fixture へ書込 → machine.toml 反映を確認 →
  git checkout で復元済み
- 計画書 §5 の実機確認項目 1〜7 はユーザー引き継ぎのまま

## docs-keeper への引き継ぎ

計画書 §6（Phase 3/4/5 集約リスト）をそのまま使用可。本実装による追加差分:

- 計画書 §5 E2E / spec の height_plane 例は led_blinker 基板に差し替えが必要
  （fill_coverage は TOP 銅箔ゾーンなし）
- probe_gnd_down_adjust: ProbeGround は遅延構築（prompt が Klipper 接続より先）
- pcbasm ログブリッジは worker スレッド + INFO 限定、logger level の一時引き下げあり

## E2E 結果（FakeCamera + 実 uvicorn、test-fixture = Klipper 不通）

- ページ巡回: pasting 6 ページとも job-console=1、preview-pane は paste_solder /
  height_plane / toolhead_offset のみ、loading-controls（data-loading-stage=
  "ローディング"・既定量 0.1・ボタン disabled 初期値）は 4 ページのみ、
  /pnp 200 + プレースホルダ、/dev /posctrl 無風 200
- probe_gnd_down_adjust: WS prompt(number, default=2.0) → -1 → log + 再 prompt →
  1.5 → FAILED + down(0) 復帰失敗 log + M84 警告 → machine-control 502（ロック解放）
- height_plane: 計画点 15 点（led_blinker）/ diagnostics log / confirm 待ち中の
  GET /artifacts/<id>/planned_points.png 200（PNG 復号可）→ confirm False で
  ABORTED / True で setup の Klipper 不通 FAILED。pcbasm ログブリッジ経由で
  「Board幅」「=== ホーミング (G28) ===」等の setup ログがコンソールに出ることを確認
- paste_solder / loading / flow_calibration / toolhead_offset: 即 FAILED +
  relax (M84) 失敗警告 + ロック解放（busy=false）
- requires_pcb: PCB 未選択（クリーン data dir）で 3 ジョブとも 400

## 検証結果

- make format: pass（全 hook Passed）
- make type: pass（0 errors）
- make test（非ハードウェア: `pytest tests -m "not hardware"`）: pass
  （1012 passed / 34 deselected。hardware 区分はユーザー実行）
