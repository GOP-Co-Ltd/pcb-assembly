# WebUI カメラキャリブレーションページ改修 レビュー

対象: `git diff main`（ブランチ feature/20260722/webui-camera-calib、未コミット全変更）
仕様: memory/agents/implementation-planner/webui-camera-calib.md +
orchestrator 裁定（要確認事項 1・2 とも採用）

## verdict: approve（再レビュー後）

初回レビューは request-changes（must-fix 1 件 = torn read、should-fix 1 件 =
混在 PUT 未ピン）。両方とも修正が確認できたため approve に更新（詳細は末尾
「再レビュー（2 回目）」節）。以下の初回指摘は記録として残す。

（初回 verdict: request-changes — must-fix 1 件・実測で再現した競合バグ。
それ以外の仕様準拠・テスト品質・規約は良好）

## must-fix

### 1. crop 保存と crosshair のフレーム毎 toml 読みの競合でストリームが切断される

- 対象: `src/webui/preview.py:253-255`（`_crop_size`）× `src/webui/config_store.py:293`
  （`write_machine_settings` の `path.write_text(...)` = 非アトミック上書き）
- 問題: `PUT /api/settings/machine` は `machine_lock` 下で machine.toml を
  truncate → write するが、`PreviewService._crop_size()` はロックを取らずフレーム毎に
  `Machine(path)` で読む。書き込み中（truncate 直後）に読むと空/部分 TOML となり
  `KeyError: "'camera' は設定ファイルに定義されていません"`。`mjpeg_stream`
  （preview.py:219 `renderer(frame)`）は例外を捕捉しないためジェネレータごと落ち、
  MJPEG 接続が終了する（preview.js の error ハンドラが再接続）。
- 根拠（仕様箇所）: ユーザー要件 4 =「クロップ変更時にストリーム**再接続なし**で即時反映」。
  crop 編集中（= crosshair を見ながら入力する本機能の主用途）にこそ発生する競合であり、
  要件の核心に対する反例経路。加えて e2e
  `test_crop_put_keeps_mjpeg_stream_open_and_reflects_in_toml` の flaky 源にもなる
  （PUT 中の `next(chunks)` が接続断で失敗し得る）。
- 再現手順（実測済み・スクリプトは scratchpad `race_check.py` / `race_check2.py`）:
  1. tight loop（読み書き全速）: 756 読み中 216 エラー（KeyError）
  2. 実運用相当（読み 15Hz / 実 `ConfigStore.write_machine_settings` 5Hz・30 秒）:
     reads=431 / writes=144 / errors=24 — **書き込みの約 17% が torn read と衝突**。
     debounce 自動保存では入力のたびに PUT が飛ぶため「たまに起きる」では済まない頻度
- 修正案（レビューのため未実施）: `ConfigStore.write_machine_settings` を
  アトミック書き込みへ（同一ディレクトリの一時ファイルに書いて `os.replace`）。
  POSIX の rename 原子性で読み手は常に旧値か新値のどちらかを見る。フレーム毎読みの
  設計（計画「設計判断 b」）自体は維持でき、ストリーム開始時読み・`_build_camera` など
  既存の全読み手も同時に直る。`_crop_size` 側で例外を握って前回値へフォールバックする案は
  他の読み手が残るうえ真正な設定エラーも隠すため非推奨
- 補足: 本競合の「読み側」自体は既存（ストリーム開始時の `machine()` 読み等）だが、
  旧挙動では camera.\* PUT が rebuild でストリームを切っていたため
  「ストリーム生存中に camera 設定を書く」ワークフローが存在しなかった。
  本 diff が per-frame 読み + 書き込み中の生存を新たに契約にしたことで顕在化した

## should-fix

### 2. 混在 PUT（camera.crop.\* + 他 camera.\*）の rebuild 挙動がテストでピンされていない

- 対象: `src/webui/routers/settings_api.py:40-44` /
  `tests/webui/routers/test_settings_api.py::TestCameraSettingsRebuild`
- 問題: 現行スイートは「camera.fps 単独 → rebuild する」「camera.crop 単独 → rebuild
  しない」のみ。誤変種（例: `any(...)` を `all(...)` に変える・
  `not any(key.startswith("camera.crop."))` へ条件を反転する）は両テストを素通りし、
  「fps + crop 混在 PUT で rebuild されない」退行を検出できない。境界そのものが
  今回変更した 1 行なので、ピンの価値が高い
- 根拠: orchestrator 指定の重点観点「camera.crop.\* と他 camera.\* キー混在 PUT 時の挙動」。
  現 UI（settings.js はキー単位 PUT、camera_calibration.js は crop のみ）からは
  発生しないが API 契約としては正当な入力
- 修正案: `test_put_camera_key_rebuilds_frame_hub` のパラメトライズ追加
  （`{"camera.fps": 10, "camera.crop.width": 300}` で hub が変わること）1 ケースで足りる

## nit

### 3. settings.js の汎用機構との重複（既知の論点への裁定: **別タスクで良い**）

- 対象: `src/webui/static/js/camera_calibration.js:9-44`（`bindCropAutoSave`）
- plan-implementer が挙げた論点。`settings.js` の `form[data-machine-settings]`
  汎用即保存機構（settings ページ・paste_solder の auto_threshold で使用中）と
  「debounce → PUT /api/settings/machine → 失敗 toast」が機能重複しており、
  テンプレートを `data-machine-settings` フォーム + `name="camera.crop.width"` +
  `data-type="int"` にすれば `bindCropAutoSave` は削除できた
- **別タスクとする理由**:
  1. 計画 IF 案 6/7（`data-machine-key` + 専用 JS）は orchestrator 裁定済みで、
     tests（test_pages.py / test_webui_e2e.py）が `data-machine-key` 属性を契約として
     ピン済み。統合はテスト契約の張り替えを伴い、code-simplifier の外科的範囲を超える
  2. 挙動差が意図的（成功 toast + サーバ truth の書き戻しは settings.js に無い）。
     統合には settings.js 側の opt-in 拡張か機能削減の判断が必要
- ただし統合タスクでは、同じ machine.toml 即保存なのにページ間で
  成功 toast の有無・エラー文言（`保存失敗: ...` vs 生 message）が不整合な点を揃えるべき

### 4. PUT 応答の書き戻しが入力途中の値を上書きし得る

- 対象: `src/webui/static/js/camera_calibration.js:30-37`
- debounce 発火 → PUT 送信 → 応答待ちの間にユーザーが続けて入力すると、応答到着時の
  `input.value = field.value` が新しい入力を旧値で上書きし、その後の debounce 保存も
  上書き後の値を読むため入力が失われる。LAN 内 RTT では窓が小さく実害は僅少。
  対応するなら `document.activeElement !== input`（または送信時値との比較）ガード。任意

## 仕様準拠の確認（要件 1〜5）

1. **square_size デフォルト 1.5mm** — OK。`jobs/posctrl.py` ParamSpec default 1.5。
   `TestCatalog.test_camera_calibration_params` と
   `test_camera_calibration_renders_square_size_form_with_default` でピン。
   空欄実行が「エラー→1.5 で実行」へ変わる挙動変更も
   `test_missing_square_size_uses_default_and_succeeds` で明示的にピン
2. **入力値の保存・復元** — OK。`persisted_params=("square_size",)` + 専用 JS の
   debounce POST param-defaults（裁定 1 採用どおり）。復元は
   `test_camera_calibration_renders_saved_square_size_default` でピン
3. **crop の machine.toml [camera.crop] リンク** — OK。ParamSpec から削除し
   `ctx.machine.camera.crop.size` 読み（`loaded.crop_size == (400, 400)` でピン）、
   初期値は `machine_settings_fields` 由来（fixture 600 が 2 箇所出ることをピン）、
   書込先は PUT /api/settings/machine（e2e で tmp toml 反映までピン）。
   旧 param 送信の拒否も `test_removed_crop_param_is_rejected_as_unknown` でピン
4. **十字線の即時反映（再接続なし）** — 実装・テストとも OK
   （`test_crosshair_crop_change_reflects_in_next_frame_without_reconnect`、
   crop PUT で FrameHub 維持、e2e でストリーム生存）。ただし must-fix 1 の競合窓が
   この要件の反例経路として残る
5. **kurousagi crop 600→300** — OK。diff は [camera.crop] の 2 行のみ

計画からの逸脱は spec-test-author の `tests/webui/routers/test_jobs.py` 追加修正
（計画のテスト更新リスト漏れ。旧 IF のままでは 400 で落ちるため必須）のみで、妥当。

## 規約・その他の確認

- **thin-wrapper**: JS は fetch/DOM のみ。クライアント検証は空欄 skip +
  `Number.isFinite` の UX 最小限に留まり、1 以上のドメイン検証はサーバ
  （config_store `_coerce`、max_failures の前例準拠）に集約。書き戻しはサーバ truth。適合
- **カプセル化**: `_crop_size` / `_CAMERA_CROP_KEYS` / `_camera_calibration_context`
  すべて private。適合
- **外科的変更**: diff 全行が要件 1〜5・裁定 1〜2 にトレース可能。要件外変更なし
  （template の data-testid は既存慣行と整合）
- **テスト品質**: 3rd-party モックなし（実 ConfigStore / AppState / PreviewService /
  JobManager + fake camera）。private 直接テストなし。4 区分適合
  （unit=catalog、integration-with-fakes=preview/settings/pages/config_store、
  e2e=実 HTTP 通し）。例外メッセージは substring 検証。「書かない」リスト混入なし
  （param-defaults の型不一致は既存の汎用契約テストに委ねる判断も testing-strategy 準拠）
- **/settings ページ経由の crop 変更**: rebuild されなくなるが、crosshair は毎フレーム
  反映・circle/copper は UI 非露出導線のみ（計画「想定リスク」で明示済み・裁定済みの
  トレードオフ）。追加対応不要と判断
- **debounce の中間値送信**（"300" 入力途中の "3" が保存される等）: 即保存 UI の内在挙動。
  1 以上検証で事故値は遮断され、枠が一瞬動くのみ。ジョブは開始時読みで巻き込まれない。
  計画「設計判断 c」採用済みのため指摘対象外
- **成果物汚染**: `grep -rn "</content>" src/ tests/` → ヒットなし（自分でも再確認）

## 検証結果

- make format: pass（orchestrator 実行済み）
- make type: pass（orchestrator 実行済み）
- make test-no-hardware: pass（1577 passed、orchestrator 実行済み）
- make test-e2e: pass（51 passed、orchestrator 実行済み）
- 追加検証（レビュー時実施）: torn read 再現スクリプト 2 本
  （scratchpad `race_check.py` / `race_check2.py`）— must-fix 1 の根拠
- `@mark_hardware` / make test: 実行していない（実機確認はユーザー）

## 再レビュー（2 回目）: must-fix / should-fix 解消確認 → approve

修正差分は `src/webui/config_store.py`（+5 → +30 行）と
`tests/webui/routers/test_settings_api.py`（+42 → +57 行）の 2 ファイルのみで、
初回レビュー済みの他ファイルに変更なし（`git diff main --stat` で照合）。

### must-fix 1（torn read）→ 解消

- `write_machine_settings` が同一ディレクトリ `tempfile.NamedTemporaryFile`
  （delete=False）+ `Path.replace` の atomic replace になった。同一ディレクトリ
  = 同一ファイルシステムなので `os.replace` の原子性が成立し、読み手は常に
  旧値か新値の完全な TOML を見る。例外時は tmp を unlink（FileNotFoundError 握り）。
  既存 `src/webui/board_settings.py::_write_doc`（244-259 行）と同型で、
  新規パターンの持ち込みなし。docstring にも atomic replace を明記
- **再現条件で自ら再検証**: 初回レビューと同一スクリプト
  （scratchpad `race_check2.py`、読み 15Hz × 実 ConfigStore 書き 5Hz × 30 秒）で
  before errors=24/144 writes → **after errors=0**（reads=427 / writes=143）。
  plan-implementer の報告（reader 3 並列でも 0）と整合
- 留意（指摘ではない）: NamedTemporaryFile 由来でファイルモードが 0600 になるが、
  board_settings の既存 precedent と同一挙動・読み手は同一ユーザーの webui のみで
  実害なし。fsync なしの電源断耐性も従来水準のまま（今回のスコープ外）

### should-fix 2（混在 PUT 未ピン）→ 解消

- `TestCameraSettingsRebuild::test_put_camera_crop_and_other_camera_key_rebuilds_frame_hub`
  が追加され、`{"camera.fps": 20.0, "camera.crop.width": 300}` の PUT で
  `frame_hub() is not hub`（再構築）をピン。初回指摘した誤変種
  （`any()`→`all()`: 混在で all([True, False])=False → 再構築せず FAIL、
  条件反転 `not any(crop)`: 混在に crop が含まれ FAIL）をいずれも検出できる構成。
  fps 単独 / crop 単独 / 混在の 3 点で境界が完全にピンされた

### その他

- `grep -rn "</content>" src/ tests/` → ヒットなし（再確認）
- make format / make type / make test-no-hardware / make test-e2e は orchestrator
  実行済みグリーン（申告どおり・再実行せず）
- nit 3（settings.js との重複 → 別タスク裁定）・nit 4（PUT 応答書き戻しの
  上書き窓）は非ブロッキングのまま残置で良い

### verdict: approve

## 再レビュー（3 回目）: crop 編集 UI の settings ページ統一（MR !137 後の未コミット差分）

ユーザー追加指示「crop 値の編集 UI を settings ページのみに置く形に統一」への対応。
対象は `git diff`（HEAD 以降の未コミット分）のみ:
`src/webui/routers/pages.py` / `src/webui/static/js/camera_calibration.js` /
`src/webui/templates/posctrl/camera_calibration.html` /
`tests/webui/routers/test_pages.py` / `tests/e2e/test_webui_e2e.py`（+ memory ノート 3 本）。

### 撤去の取り残し → なし

- src/ 全体 grep で `crop_fields` / `data-machine-key` / `bindCropAutoSave` /
  `camera-crop-settings` / `_CAMERA_CROP_KEYS` / `_camera_calibration_context`
  すべてヒット 0。tests/ 側の残存は契約説明の docstring と否定アサーションのみ（意図どおり）
- `machine_settings_fields` の import は settings ページ（pages.py:263）と
  `_paste_solder_context`（:311）で引き続き使用されており orphan ではない
- JS は未使用になった `toast` を destructure から除去済み。`api` / `debounce` /
  `DEBOUNCE_MS` は `bindSquareSizePersist` で使用継続
- `camera_calibration.html` は job.html との差分が `js/camera_calibration.js`
  読込 1 行になったが、square_size 永続化 JS のために専用テンプレートは引き続き必要
  （FEATURE_TEMPLATES / _JOB_TEMPLATES の登録も維持で正しい）

### 要件「オーバーレイ即時反映」の契約ピン → 維持

- 実装側は今回 diff に含まれず不変: preview.py フレーム毎 `_crop_size` 読み /
  settings_api.py の camera.crop.\* rebuild 除外 / config_store.py の
  atomic replace + 1 以上検証 / jobs/posctrl.py の `ctx.machine.camera.crop.size`
- テストのピンも全て存置: `test_crosshair_crop_change_reflects_in_next_frame_without_reconnect`
  （test_preview.py）、crop 単独 PUT で FrameHub 維持 + 混在 PUT で再構築
  （test_settings_api.py）、e2e の crop PUT 中ストリーム生存 + tmp toml 反映
  （本文無変更のまま `TestCameraCropSettingsOverRealHttp` へ移動したことを diff で確認）
- 新しい UI 分担のピン: `test_settings_page_renders_camera_crop_fields`
  （`name="camera.crop.width"` / `"camera.crop.height"` + ラベル「クロップ幅/高さ」+
  セクション「カメラ / クロップ」。ラベルは config_store.py:134-135 の FieldSpec、
  セクションは common.py:152 の既存 SECTION_LABELS 由来で決定的）と
  `test_camera_calibration_has_no_crop_input` / e2e
  `test_page_is_served_without_crop_input`（否定側）で双方向にピンされている。
  settings ページの編集経路自体は既存の汎用機構（settings.js、paste_solder でも使用中）
  で、API 契約（PUT → 非 rebuild → フレーム毎反映）が上記でピン済みのため
  ブラウザ JS 経路の追加 e2e は不要という整理も妥当

### 前回 approve 範囲への回帰 → なし

- 変更は撤去 + テストの契約張り替えのみ。square_size 1.5・永続化のピン
  （renders_square_size_form_with_default へ JS 読込アサーション統合、
  saved default テスト存置）も維持
- 副次効果: 初回レビューの nit 3（settings.js との機構重複）と nit 4
  （PUT 応答書き戻しの上書き窓）は `bindCropAutoSave` 削除により両方消滅

### verdict: approve（3 回目・未コミット差分に指摘ゼロ）

make format / type / test-no-hardware / test-e2e は orchestrator 側で実行中
（両 agent 各自グリーン確認済み: 1579 passed / 51 passed の申告）。
`</content>` 混入 grep は自分でも再確認しヒットなし。
