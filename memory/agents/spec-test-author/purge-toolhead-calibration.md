# purge-toolhead-calibration — spec-test-author メモ

仕様源: `memory/agents/implementation-planner/purge-toolhead-calibration.md`（唯一の仕様源）。
テスト実行時点で plan-implementer の src 実装が既に契約を満たしており、**全テスト green で完了**
（期待失敗フェーズは経由せず）。`make test-no-hardware`: 1520 passed / `make format`: 通過。

## 書いたテストと仕様根拠

### tests/pcbasm/pasting/test_toolhead_offset.py

- `TestValidateOffsetCorrection`（unit, parametrize）
  - 根拠: 計画書「公開 IF」`validate_offset_correction` — `(measured - current).norm > max_correction`
    でエラー文、境界値ちょうど（== max_correction）は None
  - 範囲内 4 ケース（差ゼロ / norm 0.5 / 境界 X / 境界 Y）→ None
  - 超過 3 ケース（X 1.5 / Y −1.2 / 対角 norm 1.5）→ 非 None
  - `test_error_message_contains_difference_and_limit`: エラー文に差 "1.5" と閾値 "0.75" を
    substring で要求。**実装側注意**: 差・閾値を小数点付きで文言に含めること
    （`{:g}` で閾値 1.0 → "1" のような整数化をすると substring が壊れるため、
    テスト側は閾値 0.75 を使い書式非依存にしてある。差の値は "1.5" を含む書式なら任意）
- `TestLocatePasteBlob`（integration-with-fakes）
  - 根拠: 計画書「公開 IF」`locate_paste_blob` 手順 1〜5
  - fake: FakeCamera（tests/helpers.py）+ 実 cv2 で描いた合成ブロブ
    （白 200x200・黒円 r=15px、10 px/mm で直径 3.0mm、diameter_min/max=2.0/4.0）。
    klipper / stage は mocker.Mock（tests/pcbasm/posctrl/test_position.py と同パターン、
    stage.get_position=(10,20,5)・move→GCode("G1")）。settle_time=0.0 で sleep 回避
    （time.sleep 自体はモックしない）
  - 中心一致 → ステージ現在位置 (10,20)±0.3 を返す
  - 手順 1 ピン: 初回 move が camera_position(50,60) + z=calibration.z_position(12.0)、
    収束時は move/send_gcode 各 1 回のみ
  - (2,3)mm ずれブロブ + tolerance=5.0 → (8,17)（戻り値 = pos − 観測オフセットの符号ピン）
  - ブロブなし → RuntimeError（"失敗" substring。OffsetObserver から伝播 = 要件 3 ジョブ中止）
  - frame_sink 配線: 成功時 1 枚配信

### tests/pcbasm/pasting/test_applicator.py

- `TestSetTransform`
  - 根拠: 計画書「公開 IF」`PasteApplicator.set_transform` — ラン内反映の唯一の入口
  - deposit_at が差し替え後 Shift を反映 / 構築時 transform と**合成でなく置換** /
    apply（pad 塗布経路）でも下降 move が Shift 分ずれる（要件 2「そのランの pad 塗布から新オフセット」）

### tests/webui/jobs/test_catalog.py

- `TestParamSpecPersist`: `ParamSpec.persist` の default False / opt-in True（公開 API 契約ピン。
  `api_contract` marker は --strict-markers 下で未登録のため付与しない）

### tests/webui/jobs/test_pasting.py

- `test_paste_solder_interactive_loading_is_bool_defaulting_false` の params 集合ピンに
  `calibrate_toolhead_offset` を追加（既存ピンの追従）
- `test_paste_solder_calibrate_toolhead_offset_is_persisted_bool_on_by_default`:
  bool / default True / persist=True / `persisted_params` に**含めない**
  （要件 4: localStorage 保存であり machine.toml・サーバ保存ではない）

### tests/webui/routers/test_pages.py

- `test_paste_solder_renders_persisted_calibrate_toolhead_offset_checkbox`:
  paste_solder ページに `id="param-calibrate_toolhead_offset"` の checkbox が
  `data-param-type="bool"` + `checked` + `data-persist="1"` で描画される
  （計画書 job_params.html 節のピン。regex は改行込み input タグに対応）

### tests/e2e/test_paste_solder_browser.py（**未実行** — 合流後に `make test-e2e`）

- `TestPasteSolderCalibrateToolheadOffsetCheckbox`
  - job-form 内表示・default checked・`data-persist="1"`
  - トグル → localStorage キー `jobParam:paste_solder:calibrate_toolhead_offset` に "0"/"1"
    保存 → リロードで復元（両方向）。browser_page はテストごとに新 context のため
    1 テスト内で往復を完結させてある
  - collect は 18 tests で成功（構文 / marker 自動付与 OK）

## plan-implementer への注意点

1. `validate_offset_correction` のエラー文: 差の値と閾値を数値で含めること（上記書式注意）
2. `locate_paste_blob` 収束時の移動は初回カメラ移動 1 回のみ（call_count==1 でピン済み）。
   補正移動の挿入や初回移動の分割はテストが割れる
3. `set_transform` は置換セマンティクス（Compose で既存に積むと
   `test_replaces_construction_transform_instead_of_composing` が割れる）
4. localStorage の bool 表現は "1"/"0"（計画書 job_console.js 節どおり。e2e がピン）

## 検証状況

- `make format` 通過 / `make test-no-hardware` 1520 passed（既存テスト破壊なし）
- e2e 2 件は記述のみ（実行は親が合流後に `make test-e2e`）
- `make test`（実機）は未実行（禁止事項）
- `</content>` 混入なしを grep で確認済み
