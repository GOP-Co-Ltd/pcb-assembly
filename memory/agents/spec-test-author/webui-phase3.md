# WebUI Phase 3: ジョブ実行基盤 + dev タブ（仕様テスト）

計画書: `memory/agents/implementation-planner/webui-phase3.md`
spec: `docs/webui/specification.md` §6, §8, §9, §10 dev タブ, §11, §12 Phase 3

## 結果サマリ

**全テストグリーン**（plan-implementer の実装が並行完了済みの状態で検証）:

- `uv run pytest tests/webui tests/pcbasm tests/scripts -m "not hardware" -q` → **873 passed, 23 deselected**
- `uv run pyright tests/` → 0 errors
- `make format` → 全フックパス

## 書いたテスト一覧

### レーン A（pcbasm 昇格）

- `tests/pcbasm/pcb/test_generate.py`（新設）
  - `TestGenerateGridPcb` — tests/scripts から移設（pcbnew モック方式温存、
    monkeypatch 先を `pcbasm.pcb.generate.pcbnew` に変更）。2x2/3x3/1x1 の
    Add 回数・SaveBoard 呼出・出力 dir 作成・reference 連番
  - `TestFillCoverageBoard` — **実 pcbnew** で build → save → PcbFile 読み戻し。
    paste pad 数 = 8・designator 集合・outline 60x40・凹形 3 種の非凸性・
    線パッドの細さ・点パッドの極小性をピン
- `tests/scripts/test_generate_grid_pcb.py`（書換）— `TestMainArgparse` のみ残し
  argparse → pcbasm 委譲を検証（`TestGenerateGridPcb` は上記へ移設）
- `tests/pcbasm/test_visualization.py`（追記）— `TestRenderPcb` /
  `TestRenderFillPaths`: 実 fixture（fill_coverage.kicad_pcb）から tmp_path へ
  PNG 出力 → cv2 で読めてサイズ > 0。既存 polygon テストは無風（import 不変）

### レーン B（webui ジョブ基盤）

- `tests/webui/test_state.py`（追記）`TestAcquireReleaseMachine` —
  acquire/release 分離、**別スレッドからの release**、BusyError(owner)、
  machine_lock との同一ロック共有
- `tests/webui/jobs/conftest.py`（新設）— store/state(fake camera)/preview/
  catalog/make_manager（終了時 shutdown）/wait_until（ポーリング、sleep 固定値
  アサート禁止の徹底）。`tests/webui/conftest.py` に `real_pcb_path` fixture を
  後方互換で追加（fill_coverage.kicad_pcb を pcb_root へコピー）
- `tests/webui/jobs/test_catalog.py` — register 重複 ValueError / get KeyError /
  list(tab, hidden 含む) / validate_params（default 充填・int→float・
  整数 float→int・choice・bool 排他・未知キー・必須欠落）/ default_catalog の
  dev 5 ジョブピン（requires_pcb / hidden / accepts_commands / uses_machine）
- `tests/webui/jobs/test_context.py` — 合成ジョブ経由で ctx の公開挙動:
  params 検証済み / pcb_path 絶対パス・None / machine / artifacts_dir =
  data/webui/<job_id> / log / progress / **frame → mjpeg_stream のオーバーライド
  優先（マゼンタ画像の画素検証、override_ttl=60 で決定的に）** / checkpoint
  no-op / next_command timeout None
- `tests/webui/jobs/test_manager.py` — 状態遷移（SUCCEEDED/FAILED+トレースバック
  ログ/ABORTED）/ start の KeyError・ValueError・requires_pcb / prompt 4 kind
  往復 + 型不一致 ValueError で prompt 維持 + id 不一致 / prompt・next_command
  待機中 abort 即時 / request_abort False（ジョブ無し・終端後）/ command 往復・
  "type" 必須・accepts_commands=False 拒否 / log_capacity=3 リングバッファ /
  排他（二重 start・select_machine・終端後解放）/ 直近 1 件（旧 artifacts_dir
  削除）/ Apply（取得・mark_applied・discard 冪等・FAILED・payload 無し・
  新ジョブで無効化）/ shutdown 冪等
- `tests/webui/jobs/test_dev.py` — 実 PcbFile / 実 pcbnew / 実 matplotlib:
  extract_pcb 5 artifacts（PNG cv2 可読）/ fill_path_simulate summary「成功」/
  generate_grid_pcb pads=divisions² / make_fill_coverage_pcb 読み戻し /
  job_demo（prompt 2 回 → apply payload canny_low=応答値、fail=True FAILED、
  command_phase jog エコー → quit）/ requires_pcb 未選択 ValueError

### レーン B/C（ルーター + ページ）

- `tests/webui/routers/test_jobs.py`（新設）— REST: 201 JobSummary（default
  充填）/ 404 / 400（型不一致・未知キー・PCB 未選択）/ 409 二重 start /
  current（null→running→succeeded）/ abort（200・prompt 待ち即時・ジョブ無し
  409・終端後 409）/ /api/state の job ブリーフ / 排他波及（machine-control・
  PUT machine・PUT settings/machine・PUT pcb-file 全部 409）/ Apply（machine.toml
  へ canny_low 書込 + コメント保持・二重 apply 409・discard 冪等・ロック保持中
  409→解放後 200）/ WS（job_demo 通しで job_status/log/progress/prompt/
  prompt_resolved + waiting_input 経由 + apply_available / abort メッセージ /
  **error イベント後も接続維持** / command エコー → quit / マシン切替で
  state_changed）/ /artifacts 配信 + `%2e%2e` traversal 404
- `tests/webui/routers/test_system.py`（追記）— E-STOP が Moonraker 不達
  （502）でも実行中ジョブを ABORTED にする / GET /api/stage/limits 不達 502 +
  `@mark_hardware` で min<max
- `tests/webui/routers/test_machine_control.py`（追記）`TestGcodeAction` —
  gcode 欠落 400 / 空文字 400 / 不達 502 / `@mark_hardware` M400 200
- `tests/webui/routers/test_pages.py`（追記）`TestDevJobPages` — dev 4 ページの
  job-console + ジョブ名 / fill_path_simulate のパラメータ名 5 種 /
  klipper_status の gcode・limits / job_demo がサイドバーに出ない

## 仕様根拠の対応（要点）

- 状態遷移・直近 1 件・リングバッファ → spec §6「排他とライフサイクル」+ 計画書 JobManager 節
- prompt 型検証・respond ValueError で未解決維持 → 計画書 context.py 節末尾
- abort 即時（prompt/next_command）+ checkpoint → spec §6 JobContext
- 全ジョブが machine ロック → 計画書「設計判断」表 2 行目
- Apply/Discard・冪等・新ジョブで無効化 → spec §8 + 計画書 Apply 節
- WS error イベント / discard / stage/limits / gcode アクション → 計画書「設計判断」（spec 未規定 → docs-keeper が追記予定）
- 終端 job_status 発行 → ロック解放の順 → 計画書「設計判断」最終行
  （テストでは解放を `wait_until(busy_owner is None)` で待ち、順序自体は固定しない）

## 期待される失敗 / 実装側に求める修正

**なし**。実装は並行完了しており、全テストが現実装でグリーン。

## テスト側で判断・修正した点（実装との突合せ）

1. **fill_coverage の paste pad 数は 7 ではなく 8**。計画書の表からは
   footprint 7 個だが、CONCAVE_L（custom pad）はアンカー極小円が L 字 paste と
   非連結のため PcbFile が 2 polygon = 2 Pad に分割する。既存 fixture
   `data/testing/fill_coverage/fill_coverage.kicad_pcb` も 8 pads であることを
   確認し（挙動不変の昇格が契約）、テストの期待値を 8 + designator 集合に修正。
2. 旧 `TestGenerateGridPcb::test_print_summary`（stdout の print 検証）は
   移設しなかった。ライブラリ昇格後の print は契約に含めない判断
   （限界価値テスト）。実装が print を残す/消すのは自由。
3. 3rd-party モックは pcbnew のみ（既存先例どおり許容）。Moonraker / cv2 /
   time.sleep のモックなし。同期待ちはすべて Event ゲート + ポーリング /
   WS 受信駆動。

## tests/helpers.py への追加

なし（既存 fixture と `tests/webui/conftest.py` の追加 fixture
`real_pcb_path` で足りた）。

## 検証結果

- `uv run pytest tests/webui tests/pcbasm tests/scripts -m "not hardware" -q`
  → 873 passed, 23 deselected（hardware）
- `uv run pyright tests/` → 0 errors
- `make format` → 全フックパス
