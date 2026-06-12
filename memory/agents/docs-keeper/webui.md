# docs-keeper: WebUI Phase 1〜5 完了時のドキュメント整備

入力: `memory/agents/plan-implementer/webui-phase{3,4,5}.md`、
`memory/agents/implementation-planner/webui-phase{4,5}.md`（§4 再設計・§6 引き継ぎリスト）

## 整備内容

- `README.md` — WebUI セクション追加（`make webui` / `make webui-dev`、port 8080、
  env: FAKE_CAMERA / CONFIGS_ROOT / DATA_DIR、全量は settings.py 参照、仕様書リンク）
- `CLAUDE.md` — プロジェクト概要に `src/webui/` / `src/scripts/` の 1 行、
  開発コマンドに `make webui` / `make webui-dev` を追記
- `configs/README.md` — `test-fixture/` の説明 1 行（E2E 用フィクスチャ、git checkout で復元）
- `docs/webui/specification.md` — 実装確定差分を反映:
  - §1/§2: pcbasm 改修の表現を「最小改修 2 点」→「表示分離（破壊的変更）+ 昇格 API」に更新
  - §4 全面改稿: frame_sink=None=非表示・window_name 全廃・window_sink・
    machine_session / `PasteSession.__exit__` の cv2 削除・posctrl/render.py・
    draw_detected_circle 昇格 + 昇格 API 一覧（visualization / height_render /
    pcb.generate / sampling_diagnostics / `PasteApplicator.from_config` /
    `Klipper.__init__` timeout 引数）
  - §5: models.py / fake_camera.py / jobs/machine_commands.py をツリーに追加、
    env に DATA_DIR / FAKE_CAMERA_IMAGE 追加
  - §6: status 小文字注記、artifacts 自動削除（新ジョブ開始で前回分削除）、
    M84 finally（uses_machine、best-effort、timeout 5s）、pcbasm ログブリッジ +
    /artifacts/ リンク化、hidden ジョブ job_demo、`JobContext.open_camera`
    （hold_camera と参照カウント共有）、machine-control に gcode アクション、
    `GET /api/stage/limits`、WS error イベント
  - §8: `POST /api/jobs/last/discard`、apply 成功時の state_changed 発行
  - §9: API 表に discard / stage/limits / gcode 追記
  - §10: camera_calibration の Z best-effort + calibration_file Apply、
    reference_point の focus_z、orthogonality の結果数値定義（軸間角ずれ deg +
    scale X/Y）、height_plane の artifacts 方式（output 廃止・非永続）、
    loading のコマンド schema（extrude/suck/finish、量は数値フィールド）+
    stage「ローディング」ゲート、flow_calibration の confirm 中止口、
    probe_gnd の number+confirm ループ + down(0) 復帰
  - §12: Phase 1〜5 完了（2026-06-12）、実機確認はユーザー残

## 検証

`uv run pre-commit run mdformat --files <4 ファイル>` Passed（初回で自動整形 → 再実行で pass）。
git commit は未実施（ユーザー判断）。

## ユーザーへの提起事項（docs では未処理）

- `PasteSession.setup` は呼び出し元ゼロのまま温存中。削除するかはユーザー判断
  （implementation-planner webui-phase5 §6 の指摘）
