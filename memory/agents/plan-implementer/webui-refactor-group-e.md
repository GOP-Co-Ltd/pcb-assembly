# Group E: テスト再編 + dead code 削除（plan-implementer 中間メモ）

ブランチ `refactor/20260707/webui-tests`（Group D に積む）。
マスター計画「Group E」節 + 設計メモ design_af47e3e9f67c85bfb.md Phase 3/5、
design_ae6a2d5954cdfa25a.md Phase 12 に基づく。

## コミット構成

1. `test(webui): 共有テストキットへヘルパ・fixture を集約`（E-1）
2. `test(webui): 層間重複テストを削除し e2e ファイルを再編`（E-2）
3. `refactor(webui): UI から到達不能な dead code を削除`（E-3、承認済み 4 件）

## collect 件数の推移

| 時点 | tests/webui | tests/e2e |
|---|---|---|
| Group E 着手前 | 588 | 41 |
| E-1 後（ヘルパ集約のみ、増減なし） | 588 | 41 |
| E-2 後（重複削除 −7 / −2） | 581 | 39 |
| E-3 後（dead code テスト削除 −8 / −1） | 573 | 38 |

（このほか tests/pcbasm/pasting/test_loading.py の 6 テストを E-3 で削除）

## E-2: 消したテスト → 担保先の対応表（MR 説明に転載する）

| 消したテスト | 守っていた検証 | 担保先 |
|---|---|---|
| routers/test_preview.py `TestPreviewStream::test_stream_returns_decodable_mjpeg_multipart` / `test_copper_stream_accepts_canny_query`（live-server 系、`live_server_url` fixture・`_read_stream` ごと削除） | MJPEG multipart 形式・フレーム shape・copper canny クエリ | e2e `TestPreviewOverRealHttp`（同アサートを移設。shape (720,1280,3) も吸収） |
| routers/test_preview.py `TestPreviewClients::test_state_reports_streaming_client_count` | ストリーム接続数の preview_clients 反映 | e2e `TestPreviewOverRealHttp::test_state_reports_streaming_client_count`（移設） |
| e2e `TestProbeGuideOverRealHttp`（2 テスト） | 較正手順文言（LOAD_CELL_CALIBRATE / SAVE_CONFIG / klipper3d.org）・probe_gnd_down_adjust の 404 化 | routers/test_pages.py `TestProbeGuidePage`（既存・同一アサート + タブ掲載確認） |
| e2e `TestPadConfigOverRealHttp::test_pad_config_get_patch_roundtrip` | GET 構造 → PATCH pads → 永続化 | routers/test_pasting.py `TestGetPadConfig` / `TestPatchPads`（同一 API の網羅）+ browser e2e（test_paste_solder_browser.py）の `wait_for_config` 経由の実 HTTP 経路 |
| e2e `TestGenerateRectPcbOverRealHttp`（実 pcbnew 生成 数十秒） | ジョブ完走・成果物生成・ページ job-form 表示・/artifacts 配信 | 実生成・寸法: jobs/test_pasting.py `TestGenerateRectPcb`（PcbFile で寸法まで検証、より深い）。/artifacts の実 HTTP 配信: 置換後の e2e `TestArtifactsOverRealHttp`（webui_data_dir 直置きファイルの GET）。job-form 表示: routers/test_pages.py のジョブページテスト |
| e2e `TestPreviewOverRealHttp::test_snapshot_returns_decodable_jpeg` | snapshot 200 + JPEG 復号 | routers/test_preview.py `TestSnapshot`（TestClient で成立する完全重複。E-3 で endpoint ごと削除予定） |
| routers/test_pasting.py `TestLoadingCalibration`（5 テスト → test_pasting_loading.py の 1+3 に圧縮） | 較正算術（volume/rpu/rate/accel の値）・丸め・null ゲーティング | tests/pcbasm/pasting/test_calibration.py `TestEstimateMassFlow`（算術・丸め・null を網羅、Group B で移送済み）。router 側は density 配線ピン（rpu≈1.89）+ null 透過の parametrize のみ維持 |
| jobs/test_pasting.py `test_loading_stage_is_pinned_for_template_and_js` / `test_menu_stage_is_pinned_for_template_and_js` | LOADING_STAGE / CALIBRATION_MENU_STAGE の実装・テンプレ・JS 文字列契約 | routers/test_pages.py の `data-loading-stage="ローディング"` / `data-loading-stage="キャリブレーションメニュー,ローディング"` アサート + e2e `TestDispenseCalibrationOverBrowser::test_menu_and_loading_controls_render` の DOM アサート（実効的な統合点。現存を確認済み） |
| jobs/test_pasting.py `test_total_job_count_covers_all_tabs`（len == 14） | ジョブ増減の検知 | タブ別 `== set(...)` テスト 3 本（test_catalog.py dev タブ / test_posctrl.py posctrl タブ / test_pasting.py pasting タブ。増減で必ず落ちる） |

## E-2: ファイル再編（削除ではなく移動）

- `test_pasting_route.py` + `test_pasting_fill_path.py` → `routers/test_pasting.py` の
  `TestPadConfigRoute` / `TestPadConfigFillPath` へ統合（`selected_client` / `_get_config` の
  3 重定義が自然消滅。C-2 分割後も route/fill-path エンドポイントは routers/pasting.py 定義
  のため統合先は test_pasting.py が 1 source 1 test に整合）
- loading 較正は C-2 の `routers/pasting_loading.py` 分割に合わせ
  `routers/test_pasting_loading.py` として独立（1 source 1 test）
- ブラウザ系テスト（TestPromptDialogOverBrowser / TestSettingsOverBrowser /
  TestLoadingOverBrowser / TestDispenseCalibrationOverBrowser）を
  `tests/e2e/test_browser_ui.py` へ移動。`test_webui_e2e.py` は HTTP/WS/MJPEG 純化、
  `test_paste_solder_browser.py` は pad editor 専用のまま
- httpx のみの `test_runtime_params_script_is_loaded` は `test_webui_e2e.py` の
  `TestDispenseCalibrationPage` として残置（browser マーカー対象外を維持）

## 計画外判断ログ

- **e2e snapshot テストの削除タイミング**: 計画上は E-3（endpoint 削除）に伴う削除だが、
  TestPreviewOverRealHttp の統合再編（E-2）時点で routers `TestSnapshot` と完全重複のため
  E-2 で削除した。endpoint 自体と routers 側テストは E-3 で削除。
- **jobs/test_dev.py の `_answer_prompts`**: 共有 `answer_next_prompt` を kind 依存応答に
  拡張せず、job_demo の prompt 順序（confirm → number）が固定であることを利用して
  「confirm=True → number=値」の 2 呼び出しに畳む `_answer_demo_prompts` ローカル薄ラッパー
  とした（全トランスポート共通抽象を作らない方針に整合）。
- **e2e `_select_led_blinker` の統一**: test_webui_e2e.py のベタ書き 3 箇所は単一ファイル
  copy（`led_blinker.kicad_pcb` 直下）だったが、共有ヘルパは copytree 版
  （`led_blinker/led_blinker.kicad_pcb`）に統一。選択パスが変わるが各テストは
  live_server ごとに隔離 tmp のため挙動同値。
- **register_gated への `hidden` 引数追加**: routers/test_jobs.py・test_system.py の旧実装が
  hidden=True で登録していたため、挙動維持のため共有関数に hidden kwarg を持たせた。
  test_system.py の「while True + sleep(0.01) + checkpoint」ジョブは register_gated の
  「gate.wait(0.02) + checkpoint」と abort 挙動が同値のため gated へ置換（gate は未使用）。
- **reset endpoint 削除に伴う import テストのセットアップ代替**: 計画の選択肢は
  「board_store 状態初期化を fixture（tmp_path 複製）で代替」だったが、既存の公開 API
  `PATCH /pasting/pad-config/node` の `clear` で override を消す方が単純かつ公開経路のみで
  完結するため、routers `test_import_restores_saved_override` と browser e2e
  `test_saved_override_file_can_be_imported` の両方を clear パッチ + 「overrides から
  消えたことの確認」に置換した（import が復元することの検証意図は不変）。
- **PreviewService.snapshot メソッドも削除**: 承認リストは endpoint
  `GET /api/preview/snapshot` だが、唯一の呼び出し元が該当 endpoint で、削除により
  src 側 orphan になるためメソッドと unit テスト
  （test_preview.py `TestSnapshot::test_snapshot_returns_jpeg_and_releases_hub`）も
  併せて削除した（CLAUDE.md「自分の変更で生じた orphan は消す」）。
  `_encode_jpeg` / `hold_camera` は mjpeg_stream 経路で使用中のため残置。

## E-3: 削除した dead code と参照テスト

| 削除対象 | src | 参照テスト |
|---|---|---|
| `POST /api/pasting/pad-config/reset` | routers/pasting.py（PasteSettingsModel / base_override_from_config import も不要化） | routers/test_pasting.py `TestReset`。reset を setup に使っていた 2 テストは clear パッチへ置換 |
| `GET /api/preview/snapshot` + `PreviewService.snapshot` | routers/preview.py / preview.py | routers/test_preview.py `TestSnapshot`（4）、test_preview.py unit（1）、e2e snapshot（E-2 で削除済み） |
| `GET /api/machines` + `GET /api/machine`（`MachinesResponse` モデル含む。PUT /api/machine と /api/state は残置 — app.js が PUT を使用、マシン一覧はサーバレンダ） | routers/machine.py | routers/test_machine.py（2）、e2e `test_machines_lists_fixtures`（1） |
| `pcbasm.pasting.loading.interactive_loading`（CLI 用 input() ベース。モジュールごと削除、`__init__` の export も除去。webui の paste_solder param "interactive_loading" は別物で残置） | pcbasm/pasting/loading.py | tests/pcbasm/pasting/test_loading.py（ファイルごと、6 テスト） |

削除後の参照確認: `grep -rn "snapshot|/api/machines|pad-config/reset|interactive_loading" src tests`
で残存参照ゼロ（webui ジョブ param の interactive_loading と PUT /api/machine を除く）。
`grep -rn '</content>' src tests` もヒットなし。

## 検証結果

- E-1 後: `make format` / `make type` / `make test-no-hardware`（1481 passed）グリーン、
  collect 件数一致（588/41）、`make test-e2e` 41 passed
- E-2 後: `make format` / `make type` / `make test-no-hardware`（1474 passed）グリーン、
  `make test-e2e` 39 passed
- E-3 後: `make format` / `make type` / `make test-no-hardware`（1460 passed）グリーン、
  `make test-e2e`（最終コミット後に実行、結果は完了報告に記載）
- `make test`（hardware）は計画どおり未実行（実機確認はユーザー分担）
