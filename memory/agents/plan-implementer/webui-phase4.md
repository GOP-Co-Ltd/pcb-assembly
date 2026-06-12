# plan-implementer: WebUI Phase 4（posctrl ジョブ + pcbasm 表示責務分離）

計画書: `memory/agents/implementation-planner/webui-phase4.md`
ブランチ: `feature/20260612/webui-phase4`（コミットはユーザー判断）

## 実装サマリ

計画書の §1〜§3 をそのまま実装。公開 IF は計画書のシグネチャどおり（逸脱なし）。

- pcbasm: `FrameSink`（vision/image.py）、`draw_detected_circle` 昇格（vision/overlay.py）、
  `posctrl/render.py` 新設（render_label / render_edge_match / PadResultRenderer）、
  setup/pad/alignment/session の frame_sink 化 + cv2 GUI 依存全廃、`window_sink` は tour.py、
  `machine_session` / `PasteSession.__exit__` は M84 のみ
- scripts 6 本追従（board_tour / orthogonality_test / reference_point_setup /
  paste_solder / toolhead_offset / height_plane）
- webui: `camera.calibration_file` ホワイトリスト、`PreviewService.hold_camera()` 公開、
  `JobBridge.hold_camera` + `JobContext.open_camera()`、manager の M84 finally
  （`RELAX_TIMEOUT=5.0`、uses_machine のみ、best-effort）、`jobs/posctrl.py`（4 ジョブ +
  `orthogonality_metrics`）、pages.py の `_JOB_TEMPLATES` 一般化、
  `partials/job_form.html` 抽出 + `posctrl/job.html` + `posctrl/reference_point_setup.html` +
  `static/js/reference_point_setup.js`

## IF 変更通知

なし（計画書の確定 IF どおり）。

## 計画外の判断（計画に明記がなかった点の解釈）

1. **reference_point_setup の `focus_z` コマンド対応**: 計画の dispatch 列挙は
   jog/home/move/relax/record/quit だが、マシン操作パネル（ジョブモード）は
   `{"type":"focus_z"}` も送ってくる。ジョブ内でロード済みの
   `calibration.z_position` へ移動する形で対応（None なら log のみ）。
2. **`PadResultRenderer` の roi_polygons 空対応**: `CopperProjector.roi_of` は
   空リストで ValueError になるため（コアは無改造の方針）、空の場合は
   「画像中心に min_roi サイズの ROI」を renderer 側で構築。pixel/mm は
   board 単位ベクトルの投影長から導出（`_centered_roi`）。
   spec-test-author の test_render.py の「空でも min_roi で ROI 成立」と整合。
3. **`orthogonality_metrics` の軸間角**: `atan2(|cross|, dot)` による
   [0,180] deg のなす角 − 90。鏡映を含む board_transform でも符号が破綻しない。
4. **OffsetObserver**: `frame_sink=None` のときは注釈用の追加 capture 自体を
   行わない（従来は表示用に 1 フレーム余分に消費していた）。
5. **scripts の destroyAllWindows 配置**: board_tour / orthogonality_test は
   main の try/finally（board_tour は本体を `_run_tour()` へ抽出して finally を付与）、
   paste_solder は main 全体を try/finally 化、toolhead_offset は正常系末尾に追加
   （計画の表どおり「追加」のみ。異常時はプロセス終了でウィンドウは閉じる）。
   height_plane は計画どおり frame_sink 化のみ。
6. **camera_calibration の検出失敗ループ**: 失敗時は log を出して prompt から
   再ループ（計画どおり）。成功フレームは `ctx.frame(image)` に加えて
   コーナー描画済み `vis` も配信。

## spec-test-author との同期

- 並列作業で先方のテスト（test_render / test_setup / test_posctrl / test_preview /
  test_context / test_manager / test_jobs / test_pages / test_config_store /
  test_alignment / conftest）が随時 push されたが、最終時点で
  `tests/pcbasm tests/scripts`（628 passed）/ `tests/webui`（299 passed）全グリーン。
  途中 test_render が 1 件 fail に見えたのは並行編集中の一時状態（単独再実行でパス）。
- テストファイルは一切編集していない。

## 検証結果

- `uv run pyright src/` → 0 errors
- `uv run pre-commit run --files <変更 src 全部>` → 全フックパス（docformatter の
  自動整形を取り込み済み）
- `uv run pytest tests/pcbasm tests/scripts -m "not hardware" -q` → 628 passed
- `uv run pytest tests/webui -m "not hardware" -q` → 299 passed
- 自前スモーク（/tmp/smoke_phase4.py、TestClient + FakeCamera=checkerboard）:
  posctrl 4 ページのマーカー / camera_calibration の WS prompt 往復 → SUCCEEDED →
  artifacts（PNG+JSON、CalibrationResult.load 可）→ Apply で
  configs/test-fixture へ machine.toml 更新 + JSON 書込（実行後 git checkout で復元）/
  2 回目 Apply 409 / reference_point_setup・board_tour の Klipper 不通 FAILED +
  relax 失敗 log + ロック解放（machine-control が 409 でなく 502）/
  prompt 待ち abort → ABORTED

## docs-keeper への引き継ぎ

計画書 §6-7 のとおり（spec §4 全面改稿、calibration_file ホワイトリスト、
open_camera / hold_camera、M84 finally、orthogonality 数値定義、Z best-effort、
§10 posctrl 表の details）。
