# WebUI Phase 1 実装ログ（plan-implementer）

入力: `memory/agents/implementation-planner/webui-phase1.md`（IF 契約、末尾「ユーザー決定の反映」込み）。
spec-test-author（`memory/agents/spec-test-author/webui-phase1.md`）と並列実行。

## 実装サマリ

計画書の公開インターフェース案どおりに以下を新規作成・変更した。

- `src/webui/`: settings.py / config_store.py / state.py / models.py / app.py / `__main__.py`
- `src/webui/routers/`: pages.py / machine.py / files.py / settings_api.py / machine_control.py / system.py
- `src/webui/templates/`: base / tab / feature / settings + partials（sidebar / machine_control / file_browser）
- `src/webui/static/`: app.css + js（app / machine_control / settings）
- `src/pcbasm/hal/klipper.py`: `Klipper.__init__` に後方互換 `timeout: float | None = None` を追加（既定挙動不変。webui からは status/E-STOP/RESTART 系 10.0 秒、machine-control 移動系 60.0 秒）
- `Makefile`: `webui-dev` / `webui` ターゲット追加
- `data/.gitignore`: `webui_state.json` 追記

## IF変更通知

- **`pcbasm.hal.Klipper.__init__(host, port, timeout=None)`**: 第 3 引数 `timeout` を追加（keyword 利用推奨）。既存呼び出しは無変更で従来挙動（無制限）。ユーザー決定済みの採用事項。

## 計画外の判断（計画書に明記されていなかった点）

1. **AppState / ConfigStore は lifespan ではなく `create_app()` 内で即時構築**。
   計画書は「lifespan で構築」だったが、`with` なしの `TestClient(create_app())` では lifespan が走らず、指定されたスモークコマンドが通らない。Phase 1 は破棄処理を要するリソース（カメラ等）が無いため即時構築が安全。Phase 2 でカメラ/FrameHub を持つ際に lifespan へ移す。
2. **`UnknownFieldError` / `BusyError` はグローバル exception handler で 400 / 409 に変換**（各ルーターで catch しない）。409 レスポンスは `{"detail": "...owner...", "owner": "<owner>"}` で detail に owner 文字列を含む。
3. **int 項目への float**: 整数値（5.0）は int に丸めて受理、非整数（5.5）は `UnknownFieldError`。bool は数値項目で拒否。
4. **`MotionUpdateResult` の restart=False 時**: `restart_ok=False, restart_error=None`（spec-test-author は `restart_requested` のみピン、整合）。
5. **`GET /api/files` の dir 列挙はフィルタなし**（dotdir 含む全 dir。計画書「dir 全部」を字義どおり実装）。entries は dir/file 混在の名前順。
6. **`select_machine` の未知マシン検証はロック取得前**（busy かつ未知名のときは ValueError=404 が優先）。
7. **`AppState.focus_z()` は broad `except Exception` で None 化**。calibration JSON の構造不一致時に cattrs が ExceptionGroup 系を投げるため網羅的に吸収（仕様は「ファイル欠落等は None」）。
8. **feature プレースホルダの Phase 表記は tab 単位**（dev=Phase 3, pasting=Phase 5, posctrl=Phase 2/4, pnp=将来）。
9. **`/api/klipper/status` / machine-control のエラー分類**: `httpx.HTTPError`（接続不能・HTTP エラー）と `RuntimeError`（send_gcode 拒否）に加え `KeyError`（printer.cfg 必須セクション欠落、`XYZStage.limits` 由来）も 502 に倒した。

## 検証結果（スコープ限定）

- `uv run pre-commit run --files <変更ファイル>` — ruff / ruff-format / docformatter 含め全フック pass
- `uv run pyright src/` — 0 errors
- `uv run pytest tests/pcbasm tests/scripts -m "not hardware" -q` — **595 passed**（既存テスト無風）
- スモーク: `TestClient(create_app())` で `/api/state` `/posctrl` → 200 / 200
- アドホック自己検証（/tmp、使い捨てスクリプト）48 項目全 PASS:
  ページ巡回・307・404 / 設定 GET/PUT のコメント保持（変更 2 行のみ）/ motion 行置換（1 行のみ・inline コメント保持）/ symlink_ok / restart 不達 → 200 + restart_ok=false / files traversal 400・404 / pcb 選択と `webui_state.json` 永続化 / port 7126 不達 → status 200 connected=false・relax 502・E-STOP 502 / jog distance 欠落 400 / ロック中 409（detail に owner）
- `tests/webui` は指示どおり未実行・未編集（spec-test-author 側で 91 passed 確認済みの報告あり）

## 残タスク（main / ユーザー向け）

- `make format && make type && make test` のフルラン + コミット（main）
- 実機確認（ユーザー）: kurousagi での homing / jog / move / relax / focus_z、motion 保存 → RESTART 反映、E-STOP 実停止、`@mark_hardware` 5 件
