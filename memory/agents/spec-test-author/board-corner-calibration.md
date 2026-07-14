# board-corner-calibration（基板4隅輪郭キャリブレーション + 基準点1点化）

計画書: memory/agents/implementation-planner/board-corner-calibration.md
ブランチ: feature/20260714/board-corner-calibration（plan-implementer と並列。
テスト記述完了時点で実装は作業ツリーに着地済み、下記すべて緑）

## 書いたテスト一覧

### tests/pcbasm/posctrl/test_board.py（新設）

- TestFitAffineTransform::test_recovers_rotation_scale_translation_exactly — 正常系
- TestFitAffineTransform::test_recovers_mirror_transform — エッジ（det<0、剛体前提の実装はここで割れる）
- TestFitAffineTransform::test_five_points_fit_by_least_squares — 正常系（和・一次モーメント 0 の対称ノイズ→LSQ 解は真値と厳密一致。先頭3点厳密解などの非 LSQ 実装が割れる設計）
- TestFitAffineTransform::test_fewer_than_three_points_raise_value_error — 異常系
- TestBoardTransformMeasurer::test_measure_recovers_known_transform_by_four_corner_servo[TOP_LEFT/BOTTOM_RIGHT] — 正常系（既知 6DOF 復元 + marker 誤差 0.4mm が結果に残らない並進 DOF ピン + 指令位置が4隅近傍のみ = T0 検証）
- TestBoardTransformMeasurer::test_measure_aborts_with_corner_name_when_a_corner_has_no_edges — 異常系（"top_right" 入り RuntimeError、4隅必須）
- @mark_hardware TestBoardCornerHardware（**実行禁止・ユーザー実機**）— setup_board_calibration 完走（Compose 契約）+ OrthogonalityMetrics（scale ±2% / axis <0.5°）。class-scoped fixture で実機通しは1回のみ、machine_session で退避

### tests/pcbasm/test_config.py（書換）

- TestCornerOffsets 削除、TestReferencePoint を新スキーマへ（to_point / offset_point / corner の TOML structure "bottom_right" / corner 行欠落→TOP_LEFT 既定）
- TestCorner::test_board_position_maps_dimensions_to_corner（4隅 parametrize: TL=(0,0), TR=(w,0), BL=(0,h), BR=(w,h)）
- TestBoardAlign（既定値 8 項目 / 節欠落→BoardAlign() / TOML 上書き+未指定は既定）
- TestMachine::test_load_config の reference_point 断言を offset=(0.0,-5.0), corner=TOP_LEFT へ
- TestGetMachineConfig._MINIMAL_TOML から [reference_point.offsets] 削除 → offset 行

### tests/webui/test_config_store.py（書換）

- TestReferencePointOffsets → TestReferencePointCornerAndOffset（corner round-trip / "center" 拒否 / offset float_pair round-trip / 不正ペア parametrize）
- TestBoardAlignFields（board_align.* 8 キーの round-trip parametrize）
- test_read_covers_every_whitelisted_key は MACHINE_FIELDS ループで自動追随（変更不要を確認）

### tests/webui/routers/test_settings_api.py（書換）

- test_get_returns_reference_point_corner_and_offset（value_type="corner"、値は CORNERS メンバー、offset は float_pair 2 要素）
- test_get_returns_board_align_fields（tolerance/search_window=float, blur_ksize=int）
- test_put_writes_corner / test_put_unknown_corner_returns_400
- test_put_writes_float_pair / test_put_invalid_float_pair_returns_400 を reference_point.offset へ書換
- test_put_writes_file_and_preserves_comments に board_align.tolerance を追加（changed 4→5 行）

### tests/e2e/test_browser_ui.py（書換・実行は make test-e2e）

- test_reference_point_offset_pair_autosave → reference_point.offset（単一 float_pair）
- test_reference_point_corner_select_autosave 新設（select 変更 → wait_machine_field("reference_point.corner", "bottom_right")）

### tests/webui/routers/test_pages.py（最小追随・依頼リスト外だが偽赤回避）

- :116 data-pair-key を reference_point.offset へ（1行）
- test_settings_page_groups_fields_by_section の「基準点 / コーナーオフセット」→「基板コーナー照合」（SECTION_LABELS["board_align"]）

### tests/webui/jobs/test_posctrl.py

- TestPosctrlHardware の docstring 前提を「アンカーコーナーのマーカー1個 + 基板4コーナーが視野/可動域内」へ（コード変更なし）

## 仕様根拠の対応表（計画書の節 → テスト）

- 「Config スキーマ」Corner 文字列値化 + board_position → TestCorner / TestReferencePoint
- 「Config スキーマ」BoardAlign 既定値・節欠落可 → TestBoardAlign
- 「新計測器」fit_affine_transform（6DOF LSQ、3点未満 ValueError、Compose 返却）→ TestFitAffineTransform
- 「新計測器」measure(marker_pos): T0=Shift(marker−offset−corner.board_position)、基準点座標は最終変換に残らない、コーナー名入り RuntimeError で即中止 → TestBoardTransformMeasurer
- 「WebUI 設定ページ」value_type "corner"（CORNERS membership）+ offset float_pair + board_align.* 8 キー → config_store / settings_api / e2e
- 「検証」ユーザー実機確認 ①② → TestBoardCornerHardware（書くだけ）

## テスト設計上の判断

- **closed-loop カメラ**: 固定画像列（FakeCamera）だとキャプチャ回数・収束反復回数を暗黙にピンしてしまうため、測定器の結合テストは「現在のステージ位置から基板の見えを描画する」自前 HAL Camera Impl `_BoardSceneCamera`（test_board.py ローカル、単一用途なので helpers へは置かない）で行った。投影規約 pixel = center + ppm*(stage − T_true(b)) は test_copper.py の符号ピンと同一。エッジ検出・照合・投影は実装（実 OpenCV）を使用。3rd-party モックなし
- **XYZStage Mock**: test_position.py の mocker.Mock 流儀 + move() 指令で get_position が追従する位置シミュレーション（サーボ収束の再現に必須）
- **tolerance=0.12**: 合成描画は 1px(=0.1mm) 量子化のため既定 0.05mm では量子化残差で収束しない。事前シミュレーション（実 detector/matcher パイプライン）で 2 反復収束・fit 誤差 ≤0.14mm を確認済み → 4隅写像の断言は abs=0.3
- 例外メッセージは substring（"top_right"）のみ検証

## 期待される失敗 / 実装側に求める修正

- なし。テスト記述完了時点で並列実装が作業ツリーに着地しており、対象スイート全緑:
  - tests/pcbasm/test_config.py + tests/pcbasm/posctrl（not hardware）: 90 passed
  - tests/webui/{test_config_store,routers/test_settings_api,routers/test_pages}.py: 全緑
  - tests/e2e/test_browser_ui.py: 収集 OK（実行は make test-e2e で別途）
- 前提にした fixture 内容（実装側と一致確認済み）: data/testing/machine.toml に
  `corner = "top_left"` / `offset = [0.0, -5.0]` / [board_align] 全キー、
  configs/test-fixture・kurousagi に corner/offset/[board_align] 行（設定エディタは既存行編集のみ）

## tests/helpers.py への追加

- なし（_BoardSceneCamera は test_board.py ローカル。単一用途のため）

## 検証結果

- make format: pass
- uv run pytest tests/pcbasm/test_config.py tests/pcbasm/posctrl -m "not hardware": 90 passed
- webui 対象 3 ファイル: 74+ passed（test_pages 含む）
- @mark_hardware（TestBoardCornerHardware / TestPosctrlHardware）は**未実行**（実機はユーザー）
- 実写画像フィクスチャ（data/testing/board_corner/）は画像未入手のため今回見送り（計画書の実写回帰はフォローアップ）
