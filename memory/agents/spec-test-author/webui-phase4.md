# WebUI Phase 4: posctrl ジョブ + pcbasm 表示責務分離

計画書 `memory/agents/implementation-planner/webui-phase4.md` §2 のシグネチャと
§4 のテスト観点を契約としてテストを記述した。plan-implementer がレーン A〜D を
同一ツリーで並行実装しており、**記述完了時点で全テストグリーン**
（`uv run pytest tests -m "not hardware" -q` → 928 passed / `uv run pyright tests/`
→ 0 errors）。

## 書いたテスト一覧

### pcbasm（追従 + 新設）

- `tests/pcbasm/posctrl/test_setup.py`（全面追従）
  - `TestOffsetObserver::test_returns_mean_mm_shift_transform_without_frame_sink` — frame_sink=None=表示なしで Transform 契約維持（正常系）
  - `TestOffsetObserver::test_success_delivers_one_annotated_frame_to_frame_sink` — observe 成功で注釈画像 1 枚（正常系）
  - `TestOffsetObserver::test_raises_on_detection_failure_without_sending_frame` — RuntimeError + フレームなし（異常系）
  - `TestMachineSession::test_sends_m84_on_exit` / `test_sends_m84_even_on_exception` — M84 のみピン（destroyAllWindows アサーション削除）
  - 削除: `mock_cv2` fixture、`window_name` 引数、カメラ Mock（→ FakeCamera）
- `tests/pcbasm/posctrl/test_render.py`（新設・src/pcbasm/posctrl/render.py ミラー）
  - `TestRenderLabel` — サイズ保持・非破壊・ラベル有無での緑画素増分ピン
  - `TestRenderEdgeMatch` — 合成エッジマスクで ROI 内 赤/緑/白枠/中心十字の画素色ピン、ROI 外は非着色、入力 3 配列の非破壊
  - `TestPadResultRenderer` — 実 CopperProjector + 実 CopperEdgeDetector（test_alignment.py の投影イディオム流用）。薄塗り α=0.35（R≈89）・想定輪郭赤・lines 白文字・検出エッジ緑・非破壊・**roi_polygons 空 → min_roi 中心 ROI 成立**（エッジケース）
- `tests/pcbasm/posctrl/test_alignment.py`（追記）
  - `TestPadAlignmentSession::test_align_delivers_edge_match_frames_to_frame_sink` — `from_calibration(result, frame_sink=...)` で align 中に合成フレーム到達。既存テストは window_name 不使用で無風確認済み
- `tests/pcbasm/posctrl/test_pad.py` / `tests/scripts/` — 無風確認（grep で window_name 不使用を確認。追従不要）

### webui

- `tests/webui/test_config_store.py`（追記、`TestMachineSettings` 内）
  - camera.calibration_file の read（既存値ピン）/ write（対象行のみ変更）/ 型不一致 UnknownFieldError。既存 `test_read_covers_every_whitelisted_key` がホワイトリスト追加を自動検証
- `tests/webui/test_preview.py`（追記）
  - `TestHoldCamera` — 0→1 start / 1→0 stop、stream と双方向のカウント共有、ネスト保持、カメラ初期化失敗の伝播
- `tests/webui/jobs/test_context.py`（追記）
  - `TestOpenCamera` — open_camera が capture 可能な Camera を貸し退出で解放、with 内例外でも解放
- `tests/webui/jobs/test_manager.py`（追記）
  - `_register` に `uses_machine` パラメータ追加（後方互換）
  - `TestRelaxOnTermination` — uses_machine=True で M84 失敗警告 + SUCCEEDED/FAILED 不変、uses_machine=False は relax 非試行。**relax → release の順のため log アサート前に busy_owner=None をポーリング**
- `tests/webui/jobs/test_posctrl.py`（新設・本 Phase 主戦場）
  - `TestCatalog` — default_catalog の posctrl 4 ジョブ、flags（requires_pcb/uses_machine/accepts_commands）、params の default ピン
  - `TestOrthogonalityMetrics` — Identity/剛体 → (0,1,1)、shear tan2° → |誤差|=2°・scale_y=1/cos2°、剛体合成不変、対角行列スケール
  - `TestCameraCalibrationJob` — checkerboard.png（square_size=10mm, crop 400, ppm≈6.67）でフル完走（prompt → SUCCEEDED → artifacts PNG/JSON → Apply payload）、confirm=False → ABORTED、検出不能画像 → 警告 log + 再 prompt → ABORTED
  - `TestMachineJobsWithoutKlipper` — board_tour / orthogonality_test / reference_point_setup の graceful FAILED（port 7126 接続拒否）+ ロック解放 + M84 警告、PCB 未選択 ValueError
  - `TestPosctrlHardware`（`@mark_hardware`、ユーザー実行）— reference_point_setup の jog→record→Apply / quit→ABORTED、camera_calibration の実 Z 記録（square_size=1.5 の実チェッカーボード前提）、board_tour（"照合"）/ orthogonality_test（"軸間角"）の通し
- `tests/webui/routers/test_jobs.py`（追記）
  - `TestCameraCalibrationApplyFlow` — checkerboard TestClient で WS 完走 → `POST /api/jobs/last/apply` → configs/kurousagi の machine.toml 更新（コメント保持）+ JSON が `CalibrationResult.load` で読める
- `tests/webui/routers/test_pages.py`（追記 + 追従）
  - `TestPosctrlJobPages` — ジョブ 3 ページ（job-console / preview-pane / crosshair / param フィールド）、reference_point_setup（Record/Quit/専用 JS）
  - 追従: `test_other_features_keep_placeholder` → `/pasting/paste_solder` に変更（posctrl は全 feature 実装済みのため）
- `tests/webui/conftest.py`（追加、後方互換）
  - `CHECKERBOARD_CAMERA_IMAGE`（data/testing/checkerboard.png。新アセット不要）+ `checkerboard_camera_settings` / `checkerboard_camera_app` / `checkerboard_camera_client`

## 仕様根拠の対応表（要点）

- frame_sink=None=表示なし・window_name 全廃 → 計画書 §1 差分表（ユーザー決定 2026-06-12 の破壊的変更）
- machine_session M84 のみ → §1「finally は M84 のみ」
- render 3 関数の合成内容 → §2 render.py docstring（tour.py / pad._show / board_tour._show_pad_result の昇格）
- hold_camera / open_camera → §2 preview.py / context.py
- M84 finally の順序（status 確定 → relax → publish → release）→ §2 manager.py
- ジョブ定義表（params/flags）→ §2「ジョブ定義表」
- camera_calibration の Z best-effort（z_position=None・summary「未取得」）→ §2 + 判断保留点 1（ユーザー決定: best-effort）
- orthogonality_metrics の数値定義 → §2 OrthogonalityMetrics + 判断保留点 2。**符号は大きさのみピン**（物理的解釈が実機検証待ちのため `abs()==2.0`）
- Klipper 不通の graceful FAILED → §4「Klipper 不通の実挙動はモックなしで異常系結合に使える」

## 期待される失敗

なし（並行実装が記述完了時点で §2 契約どおり完成しており全グリーン）。

## 実装側に求める修正

なし。実装は計画書 §2 のシグネチャと一致していることをテストで確認済み。

## tests/helpers.py への追加

なし（既存 FakeCamera / mark_hardware / FixedImageCamera で充足。3rd-party
モックは不使用。OffsetObserver の detector のみ既存スタイル踏襲で mocker.Mock）。

## 検証結果

- `make format` — 通過（docformatter 自動修正後）
- `uv run pytest tests -m "not hardware" -q` — **928 passed**, 28 deselected
- `uv run pyright tests/` — 0 errors
- `@mark_hardware`（TestPosctrlHardware ほか）はユーザー実行。前提条件は
  クラス docstring に明記（Moonraker 7125 稼働・実カメラ・fill_coverage 基板
  セット・1 マス 1.5mm 実チェッカーボード）

## 補足（docs-keeper / ユーザー向け）

- checkerboard.png は 400x400 のため camera_calibration テストでは
  crop_width/height=400 を明示（既定 600 は画像をはみ出す）。実機の既定 600 は
  実カメラ解像度 1280x720 で問題ない
- render_edge_match の画素検証は中心十字（col 100±30）と重ならない座標を選ぶ
  必要がある（描画順: expected → detected → ROI 枠 → 十字）
