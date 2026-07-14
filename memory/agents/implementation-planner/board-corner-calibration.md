# 基板4隅輪郭検出による board transform キャリブレーション + 基準点1点化

## Context

現在の board transform（基板座標→機械座標）キャリブレーションは、マウンター上の基準点マーカー3点（top_left 必須 + 2点、`CornerOffsets` が ≥3 を強制）を `BoardTransformMeasurer` で実測し 2×2 アフィンを解く方式。しかし基板はマウンター上で微妙にずれるため、マーカー基準では基板自身との整合が取れない。そこで:

1. **基準点は1点に緩和**（top_left/top_right/bottom_left/bottom_right のどれでも指定可。TL に置けない機体があるため）。役割は (a) カメラ回転計測ターゲット、(b) 視覚サーボのウォームアップ、(c) 4隅へカメラを運ぶための**粗並進の初期推定のみ**に縮小 — 最終変換には基準点の座標は一切残らない
2. **最終の board transform は並進含む6DOFすべてを基板自身の四つ角の輪郭（外形エッジ）検出から決定**。投影した設計外形と Canny エッジの chamfer 照合を4隅で行い、4対応点の最小二乗フィットで並進・回転・スケールを一括推定

ユーザー決定（確認済み）: **一本化**（3点マーカー法は削除）／**4隅必須**（1隅でも失敗なら即中止）／**一括実装**（調整プレビューページ含め synthetic テストで実装、実機調整はユーザー）。

board_transform の出力契約 `Compose([Matrix2d, Shift])` を維持するため、下流（PasteSession / pasting / board_tour / orthogonality_test / PadAlignmentSession）は無変更。

## 設計要点

### 1. Config スキーマ（src/pcbasm/config.py）

```toml
[reference_point]
corner = "top_left"     # アンカーコーナー（省略時 top_left）
x = 10.7                # そのマーカーのおおよその機械座標 [mm]
y = 6.7
target_diameter = 3.0
offset = [2.5, -2.5]    # 基板コーナー → マーカー のオフセット [mm]（marker = corner + offset）

[board_align]           # 基板コーナー照合（トップレベル節、全項目に既定値あり・節欠落可）
tolerance = 0.05        # 各コーナーサーボの収束許容 [mm]
max_correction = 2.0    # 照合ずれ上限、超過は誤マッチ棄却 [mm]
search_window = 1.5     # 探索窓 片側幅 [mm]
edge_length = 2.0       # コーナーから含める外形エッジ長 = ROI片側幅 [mm]
theta_range = 2.0       # 回転探索 片側範囲 [deg]
canny_low = 100.0
canny_high = 200.0
blur_ksize = 5
```

- `Corner(Enum)` を文字列値化（cattrs が TOML 文字列を直接 structure）+ `board_position(width, height) -> Point2d` 追加（TL=(0,0), TR=(w,0), BL=(0,h), BR=(w,h)）。webui 検証用 `CORNERS` タプル（`DISPENSE_MODES` 前例）
- **削除**: `CornerOffsets` クラス全体（≥3 制約 config.py:248-252 含む）、`ReferencePoint.offsets`、`ReferencePoint.get_reference_position()`
- `BoardAlign` attrs クラス（既定値コード側）+ `Machine.board_align` プロパティ（節欠落時は既定値、`nozzle_cap` の欠落判定パターン）
- 互換シムなし。configs/{kurousagi,test-fixture,pd_china_frame}/machine.toml + data/testing/machine{,_minimal}.toml を同一コミットで移行（`[reference_point.offsets]` 削除 → `corner`/`offset` 追記、`[board_align]` 全キー追記 ※webui 設定エディタは既存行編集のみのため行が必要）

### 2. 新計測器（src/pcbasm/posctrl/board.py 全面書き換え）

クラス名 `BoardTransformMeasurer` は維持（責務同一、export/import 変更最小化）。**PadAligner で実機実証済みのパターンを完全再利用**し、correction.py の符号規約には一切触れない:

```
measure(marker_pos: Point2d) -> Compose   # marker_pos = マーカーへのサーボ収束位置
  (1) T0 = Shift(marker_pos − offset − corner.board_position(w,h))   # 並進のみ・恒等回転
  (2) CopperProjector(polygons=[outline.polygon], board_transform=T0, ...)
  (3) 4隅巡回（アンカー先頭の矩形順）: 各コーナー b_i で
      - 指令位置 s_i = T0(b_i) へ移動、投影アンカーを s_i に固定（再投影しない: 収束の必須則）
      - 期待コーナー pixel は構成上厳密に画像中心 → ROI = 中心の固定正方形（片側 edge_length·ppm）
      - CopperPadObserver（既存、pad.py:65 — 撮像→CopperEdgeDetector→match_rigid→Transform、
        max_offset_mm=board_align.max_correction で誤マッチ棄却）
      - XYPositionAdjustor（既存、position.py — adjust() は残差補正済み収束位置を返す）
      → m_i = adjust() 戻り値。失敗（非収束/照合不能/上限超過）はコーナー名付き RuntimeError で即中止
  (4) fit_affine_transform(board_points, machine_points) -> Compose([Matrix2d, Shift])
      np.linalg.lstsq: design=(n,3)[bx,by,1], target=(n,2)[mx,my] → matrix=params[:2].T, shift=params[2]
      残差・OrthogonalityMetrics（scale/axis）をログ出力
```

- `fit_affine_transform(board_points, machine_points) -> Compose` はモジュールレベル純関数（3点未満 ValueError）
- サーボ方式採用の根拠: 収束位置がそのまま m_i（カメラ空間→機械座標の符号変換を board.py で再構成しない）、画像中心で読むため ppm の Z ずれ誤差が混入しない
- `match_rigid` の per-corner θ スイープは T0 恒等回転とのミスマッチを吸収（L字腕の回転並進バイアス除去）
- chamfer は観測側 distance transform のため、ベベル二重エッジ・クランプ輪郭・マーカー円などの余剰観測エッジは正解位置のスコアを悪化させない（偽極小は search_window + max_correction で棄却）
- 削除: `_select_corners`、3点法 measure 本体、`_get_reference_position`、`_measure_corner`、`adjust_reference`/`move_velocity_ratio` 引数
- 無変更で維持: `OffsetTransformMeasurer`（カメラ回転2点法）、`XYPositionAdjustor`、`OffsetObserver`+`CircleDetector`、posctrl/copper.py・pad.py・correction.py 全体、vision/ 全体（pixel 空間のみ規約維持）

### 3. setup_board_calibration（src/pcbasm/posctrl/setup.py:98-225）

計測フェーズのみ差し替え（シグネチャ・`BoardCalibrationResult` 不変 → 呼び出し側無変更）:
マーカー移動ログを `corner.value` 表記に → OffsetTransformMeasurer（無変更）→ `marker_pos = position_adjustor.adjust()` → 新 `BoardTransformMeasurer(..., board_align=machine.board_align, frame_sink=frame_sink).measure(marker_pos)`。frame_sink は CopperPadObserver 経由でコーナー照合の注釈フレームを配信（WebUI は既存の ctx.frame → PreviewService.submit_override 経路で追加実装ゼロ）。

### 4. WebUI（src/webui/）

- **統合「輪郭調整」ページ**（既存 copper_detection をリネーム・拡張。新ページは追加しない）:
  - feature key `copper_detection` → `contour_tuning` にリネーム（TABS / FEATURE_LABELS「輪郭調整」/ FEATURE_TEMPLATES / コンテキストプロバイダ / テンプレートファイル名 / 参照テストを追随）
  - **検出対象ドロップダウン**: 「銅箔検出」（overlay=copper、params=`paste_dispenser.pad_align.*`）/「コーナー検出」（overlay=board、params=`board_align.*`）。モード定義（overlay 種別・保存キー prefix・各パラメータ現在値）は**サーバが Jinja コンテキストで両モード分を提供**（埋め込み JSON or data 属性）。JS はモード切替時にストリーム URL 差し替え・フォーム値の入替・保存 PUT のみ（ドメイン知識・キー名のハードコードを JS に置かない — thin-wrapper）
  - **調整値はモードごとに canny_low / canny_high / blur_ksize の3つ**（エッジマスク表示に効くもの。PadAlign / BoardAlign 双方に同名フィールドあり）。ライブ反映のため `/api/preview/stream` に `blur_ksize` クエリパラメータを追加（奇数・範囲検証はサーバ側、不正は 422）。search_window 等の照合パラメータは表示に効かないため settings ページのまま
  - **保存は選択モードの全パラメータ（3キー）を 1回の `PUT /api/settings/machine` で一括書込**
  - `OverlayKind` に `"board"` 追加（preview.py:27）、`_build_renderer` に case 追加 — `machine.board_align` から params を読み **既存 `_CopperRenderer` を再利用**（Canny 緑マスクのみ。投影外形は T_b 未計測のため描かない。照合の注釈フレームはジョブ実行中に frame_sink → submit_override 既存経路で配信）
- **設定ページ**: config_store.py に value_type `"corner"` 追加（`_coerce` で `CORNERS` membership、dispense_mode 前例）。MACHINE_FIELDS: `reference_point.corner`（corner）+ `reference_point.offset`（float_pair）+ `board_align.*` 8項目を追加、`reference_point.offsets.*` 4件を削除。common.py に `SECTION_LABELS["board_align"]` + `CORNER_LABELS`（日本語、settings select と reference_point_setup 表示で共用）。settings.html に corner select 分岐（dispense_mode 同形）、settings.js は文字列型判定に `"corner"` 1語追加のみ
- **reference_point_setup ジョブ**（jobs/posctrl.py:149-200）: 書込キー不変（`reference_point.x/y`）。docstring/ログ/Apply ラベルとテンプレ文言をアンカーコーナー表記へ（コンテキストで `anchor_label` をサーバ解決）。コーナー選択はジョブ UI に置かず settings 専用。reference_point_setup.js 無変更
- 不整合（corner 変更 + offset 未調整）は保存時に通り load 時 ValueError → 既存の graceful FAILED 挙動で許容（複合検証を UI に複製しない）

## テスト計画（testing-strategy 4区分 / refactor-conventions 準拠）

- **unit**:
  - tests/pcbasm/test_config.py: `TestCornerOffsets` 削除、`TestReferencePoint` を新スキーマへ書換（corner structure・既定 top_left・offset_point・不正 corner）。`Corner.board_position` 4隅。`BoardAlign` 既定値/TOML上書き/節欠落
  - tests/pcbasm/posctrl/test_board.py（**新設** — 現状 BoardTransformMeasurer は unit 未テスト）: `TestFitAffineTransform`（既知アフィン〔回転+スケール+並進、鏡映含む〕の厳密復元・5点 LSQ・3点未満 ValueError・縮退）
- **integration-with-fakes**（同ファイル `TestBoardTransformMeasurer`）: FakeCamera（tests/helpers.py:62）+ 合成コーナー画像（明背景/暗矩形、test_pad.py の流儀）+ mocker.Mock の Klipper/XYZStage（test_position.py:39-49 の確立パターン）。既知変換を仕込んだ画像列で: 4隅収束→フィット一致（pytest.approx）、1隅無エッジ→コーナー名入り RuntimeError で即中止、探索窓超過→誤マッチ棄却。**実写回帰**: ユーザー添付のコーナー実画像を data/testing/board_corner/ に置き、既定 board_align パラメータで Canny→照合が位置を当てることをピン
- **webui**: test_config_store.py（corner/offset/board_align round-trip・不正値、offsets.* テスト書換）、routers/test_settings_api.py（GET に新フィールド・不正 corner 400・コメント保持）、routers/test_pages.py（`/posctrl/contour_tuning` 200・ドロップダウン+両モードのモード定義データ・anchor 表示。copper_detection 参照の既存テストはリネーム追随）、test_preview.py（board オーバーレイ緑マーク・canny/blur_ksize override・不正 blur_ksize 422）。jobs/test_posctrl.py の `TestCatalog` は無変更（輪郭調整はページでありジョブではない、POSCTRL_JOBS 5本のまま）
- **e2e**（make test-e2e、pytest live_server 第一・常駐サーバー起動はしない）: overlay=board&blur_ksize=… の MJPEG デコード、corner select 変更→autosave（`wait_machine_field("reference_point.corner", ...)`）、輪郭調整ページでモード切替→スライダー→保存で選択モードの3キーが一括反映（コーナー検出→`board_align.*`、銅箔検出→`paste_dispenser.pad_align.*` を `wait_machine_field` で検証）。既存 `test_reference_point_offset_pair_autosave`（test_browser_ui.py:118）は `reference_point.offset` へ書換
- **hardware（書くだけ・実行はユーザー）**: test_board.py 内 `@mark_hardware class TestBoardCornerHardware`（setup_board_calibration 完走 + OrthogonalityMetrics 閾値、基板なしでの明確な失敗）。TestPosctrlHardware の docstring 前提を「マーカー1個 + 4コーナーが視野/可動域内」へ更新

## 実行手順（エージェントチーム / ブランチ）

ブランチは2段スタック（large-refactor-workflow 方針: ユーザーが最後にまとめる）:

```
main
 └─ feature/20260714/board-corner-calibration        # コア: config + posctrl + toml移行 + テスト
     └─ feature/20260714/board-corner-calibration-webui  # WebUI: preview/pages/settings/e2e
```

1. **コアブランチ**: 公開 IF はシグネチャ確定済みのため **spec-test-author ∥ plan-implementer 並列**（パターンA。tests/ と src/ は disjoint、helpers 更新は spec 側）。合流で `make format && make type && make test-no-hardware` → code-simplifier
2. **WebUI ブランチ**（コアに stack）: plan-implementer → e2e は `make test-e2e` で自己検証（webui-e2e skill）→ code-simplifier
3. **docs-keeper**: configs/README.md（`[reference_point]` corner/offset + `[board_align]` スキーマ、「アンカー1コーナーのみマーカー必須・board 変換は基板4隅輪郭で計測」）、posctrl/README.md の3点法記述更新
4. **MR 2本**（gitlab-mr skill）: コア MR → main、WebUI MR → コアブランチ。MR 説明に: 挙動変更要約（≥3→1・4隅必須・3点法削除）、**既存マシンの toml 移行手順**、実機確認チェックリスト（下記）
5. 各エージェントの中間メモは memory/agents/<agent>/board-corner-calibration*.md。コミットは 1関心事ずつ（feat(config)/feat(posctrl)/chore(configs)/feat(webui)×3/test(e2e)/docs）

## 検証

- 各フェーズで `make format && make type && make test-no-hardware`、WebUI ブランチで `make test-e2e`
- `make test`（ハードウェア含む）・`@mark_hardware`・実機ブラウザ確認は **Claude は実行しない**（実機が動くため）
- **ユーザー実機確認（MR 説明にチェックリスト化）**: ①アンカー1点サーボ→4隅巡回の完走（board_tour）②orthogonality ログの scale/axis が妥当 ③輪郭調整ページ（コーナー検出モード）で実基板エッジに対する canny/blur チューニング ④ベベル二重エッジ/クランプ近傍での照合安定性 ⑤kurousagi 実 toml の移行

## リスク（実装時に既知として扱う）

- T0 恒等回転の許容量: 初期ずれ ≈ L·θ + offset 誤差。search_window 1.5mm で L=100mm なら θ≤0.86°（クランプ基板は通常 ≪0.5°）。不足時は toml で search_window を広げる運用
- ベベル二重エッジへのロック → 一様スケール誤差として現れ OrthogonalityMetrics ログで発見可能。canny チューニング + 下流 pad align（tolerance 0.03）が最終精度を担保
- 非矩形外形: コーナー = `Outline.polygon.bounds` の4隅（既存 `_board_corners` と同規約）。bounds コーナー±edge_length に外形ジオメトリが無い基板は照合不能→明確なエラー（docstring/README に明記）
- 8式6未知数で残差自由度2 → 1隅の誤計測は残差に出にくい。閾値化は実機データを見てから（残差・orthogonality のログ出力に留める）

## 主要変更ファイル

- src/pcbasm/config.py — Corner 文字列値化+board_position / ReferencePoint 新スキーマ / CornerOffsets 削除 / BoardAlign
- src/pcbasm/posctrl/board.py — fit_affine_transform + 新 BoardTransformMeasurer（全面書き換え）
- src/pcbasm/posctrl/setup.py — 計測フェーズ差し替え
- configs/*/machine.toml, data/testing/machine*.toml — スキーマ移行 + [board_align]
- src/webui/preview.py, routers/(pages|preview).py, config_store.py, routers/common.py, templates/settings.html, templates/posctrl/(copper_detection→contour_tuning|reference_point_setup).html, static/js/(preview|settings).js, jobs/posctrl.py
- tests/pcbasm/test_config.py, tests/pcbasm/posctrl/test_board.py（新設）, tests/webui/{test_config_store,test_preview,routers/*}.py, tests/e2e/*, data/testing/board_corner/（実写フィクスチャ）
- configs/README.md, src/pcbasm/posctrl/README.md
