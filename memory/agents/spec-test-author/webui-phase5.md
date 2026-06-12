# WebUI Phase 5（pasting ジョブ + 仕上げ）仕様テスト

計画書 `memory/agents/implementation-planner/webui-phase5.md` §1〜2・§4 と
spec §6/§8/§10 pasting/§11 を契約としてテストを作成した。ユーザー決定（判断保留点
1〜4 は計画どおり）を反映済み。

**結果: plan-implementer の実装（未コミットの working tree）に対して全テスト green**。
`uv run pytest tests -m "not hardware" -q` → 1012 passed / 34 deselected、
`uv run pyright tests/` → 0 errors、`make format` → 全 hook Passed。
仕様 first で書いたが並行実装が先に収束したため、赤の引き継ぎ事項はない。

## 書いたテスト一覧

### 新設

- `tests/webui/jobs/test_pasting.py`（主戦場）
  - TestCatalog: pasting 6 ジョブの name / flags（requires_pcb / uses_machine /
    accepts_commands）/ float param の default・unit / interactive_loading(bool,
    False) / probe_gnd params=() / **全カタログ件数 15**（dev5+posctrl4+pasting6）
  - TestParseLoadingCommand: extrude/suck の符号、finish、amount 欠落・0・負・
    非数・未知 type・type なし → None、`LOADING_STAGE == "ローディング"` ピン
  - TestProbeGndDownAdjust: number prompt（default=2.0 = test-fixture の
    probe.down_distance）→ 負数で再 prompt → 正数で Klipper 不通 FAILED +
    down(0) 失敗 log + M84 警告 + ロック解放 / prompt 待ち abort → ABORTED +
    down(0) 失敗 log
  - TestHeightPlaneFrontFlow: confirm 待ち時点で `planned_points.png` が
    cv2 復号可能 + `/artifacts/` `min_clearance` を含む log + confirm(default=True,
    message に「計測します」) → False で ABORTED / True で setup の Klipper 不通
    FAILED
  - TestMachineJobsWithoutKlipper: paste_solder / toolhead_offset（PCB あり）、
    loading / flow_calibration → 即 FAILED + M84 警告 + ロック解放。
    requires_pcb 3 ジョブの PCB 未選択 ValueError
  - TestApplyTargetsWhitelisted: Apply 反映先 4 キー（rotations_per_ul /
    toolhead.x,y / probe.down_distance）の write_machine_settings 書込成立
  - TestPastingHardware（`@mark_hardware`、ユーザー実行）: loading の
    extrude→finish SUCCEEDED（summary「押出合計 … uL」）、loading 中の jog 受理、
    probe_gnd の確定→Apply、flow_calibration のフル往復（ダミー質量）と
    タール confirm False → ABORTED、height_plane フル（led_blinker 基板前提・
    artifacts ラベル「計測予定点」「ヒートマップ」）
- `tests/webui/jobs/test_machine_commands.py`
  - 未知 type → False / focus_z=None → log のみで True / jog の引数 ValueError →
    log + True / 認識 type（jog/home/move/relax/focus_z あり）は送信例外が伝播し
    ジョブ FAILED（test-fixture 接続拒否で検証。実送信は posctrl 実機テスト区分）

### 追記

- `tests/webui/jobs/test_manager.py` — TestPcbasmLogBridge: worker スレッドの
  `pcbasm.*` INFO log が record.log_lines に出る / 別スレッド由来は出ない /
  終了後は転送されない（detach）
- `tests/webui/routers/test_pages.py` — TestPastingJobPages（6 ページの
  job-console + job-form、preview-pane は 3 ジョブのみ・overlay 切替なし、
  loading-controls + `data-loading-stage="ローディング"` は 4 ジョブのみ、
  flow_calibration / toolhead_offset のフォーム項目）+ TestPnpPlaceholder。
  旧 `test_pasting_features_keep_placeholder`（「未実装」表示のピン）は Phase 5
  仕様で陳腐化したため削除し置換
- `tests/webui/routers/test_jobs.py` — TestPastingJobsOverWs: probe_gnd の
  WS prompt 往復（default=2.0 → -1 で新 prompt → 1.5 で failed → machine-control
  が 409 でなく 502 = ロック解放）/ height_plane 実行中（confirm 待ち）の
  `GET /artifacts/<id>/planned_points.png` 200 + PNG 復号
- `tests/pcbasm/geometry/test_sampling.py` — TestSamplingDiagnostics:
  既知正方形の数値ピン（point_count=4 / min_clearance=2.0 / hull_ratio=0.18）、
  複数島の min、空 → None、2 点（凸包=線分）→ ratio 0.0、outline 面積 0 の防御
- `tests/pcbasm/test_visualization.py` — TestHeightRender:
  render_planned_points（4 点 + 凸包不成立の 2 点境界ケース）/
  render_height_plane（非退化 6 点の合成 HeightPlane。rank=6 を事前検証済み）が
  led_blinker fixture で cv2 復号可能な PNG を出力
- `tests/pcbasm/pasting/test_applicator.py` — TestFromConfig: retract / apply の
  dispenser 呼び出しが手動 kwargs 構築と一致、config の retract_accel_factor<=1.0
  で ValueError
- `tests/webui/conftest.py` — `COPPER_PCB_FIXTURE` / `copper_pcb_path` fixture を
  後方互換で追加（led_blinker を pcb_root へコピー）

### tests/scripts

追従なし。pasting 系 scripts には既存テストが無く、import 可能性のみのテストは
規約上書かない（testing-strategy「書かない」リスト）。scripts の from_config /
可視化昇格の回帰は実機確認項目 7（計画書 §5）でカバー。

## テストで確定させた契約（実装・テンプレート・JS が従うべき値）

1. `LOADING_STAGE == "ローディング"`（pasting.py / `data-loading-stage` 属性 /
   loading_controls.js の 3 箇所契約。テンプレート側もピン済み）
2. param unit 文字列: `uL` / `mm` / `rev` / `rev/s` / `rev/s^2`（計画書の表記）
3. probe_gnd の終了時 down(0) best-effort 失敗 log は「ダウン距離」を含む
4. height_plane: 成果物ファイル名 `planned_points.png`、confirm message に
   「計測します」、diagnostics log に「min_clearance」、リンク log に
   `/artifacts/`、artifacts ラベル「計測予定点」「ヒートマップ」
5. loading summary は「押出合計 … uL」を含む
6. probe_gnd 初回 prompt default = machine.probe.down_distance
7. `default_catalog()` の総件数 = 15

## 計画書からの逸脱・発見事項

- **fill_coverage.kicad_pcb には TOP 銅箔ゾーンが無い**（pads 8 のみ）。
  test-fixture の probe 設定（min_radius=0.7, min_samples=6）では
  `sample_points_in_polygons` が候補 0 で ValueError → height_plane が即 FAILED。
  height_plane 系テストは `data/testing/led_blinker/led_blinker.kicad_pcb`
  （TOP 銅箔 7 島、15 点サンプル可）を使用。**計画書 §5 E2E 手順 3（height_plane
  を fill_coverage で実行）はこのままでは失敗するので led_blinker に差し替えが
  必要**。実機確認項目 5 も同様。
- 計画書 §4 は `tests/pcbasm/visualization/test_height_render.py` 新設としていたが、
  既存配置（`tests/pcbasm/test_visualization.py` 単一ファイル）に合わせて追記した
  （ユーザー指示どおり）。
- 計画書の「合成 HeightPlane（既知 3〜5 点）」は実装上不可能
  （HeightPlane は非退化 6 点以上が必須）→ 6 点で作成。
- _run_loading_loop はステージ表示直後に滞留コマンドを drain するため、実機テスト
  では stage 確認後 1 秒 settle してから submit する（test_pasting.py の
  `_wait_loading_stage_and_settle`。drain と submit の競合はジョブ側では防げない）。
- paste_solder / toolhead_offset の実機通し pytest は書いていない（プレビュー目視・
  ペースト状態依存のため WebUI 手動 E2E = 計画書 §5 引き継ぎ 3・6 に委ねる。
  test_pasting.py の hardware クラス docstring に明記）。

## 実装側への修正要求

なし（並行実装が上記契約すべてと整合していることをテストで確認済み）。
