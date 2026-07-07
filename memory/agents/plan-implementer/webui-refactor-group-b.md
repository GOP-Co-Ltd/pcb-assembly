# Group B: pcbasm への移送（refactor/20260707/pcbasm-paste-helpers）

計画: `fable-webui-pcbasm-webui-flake-webui-starry-quasar.md` Group B（B-1/B-2/B-3）。
3 コミットで完了。`make format` / `make type` / `make test-no-hardware`（1475 passed）/
`make test-e2e`（41 passed）全グリーン。`</content>` 混入なし。

## コミット

1. `2851a2b` refactor(pasting): 有効 pad 選択と順路計算を pcbasm へ一本化
2. `efb432c` refactor(pasting): fill-path 組み立て・initial-purge 検証・較正見積を pcbasm へ
3. `00bab97` refactor(pasting): toolhead_offset の吐出を applicator へ集約

## pcbasm の新公開 IF

- `pcbasm.pasting.settings.is_pad_enabled(pad, hierarchy, resolved) -> bool`
- `pcbasm.pasting.settings.select_enabled_pads(pads, hierarchy, model) -> list[Pad]`
- `pcbasm.pasting.route.routed_enabled_pads(pads, hierarchy, model) -> list[Pad]`
- `pcbasm.pasting.fill_path.build_pad_fill_plan_for(polygon, *, nozzle_diameter, auto_line_aspect_ratio, auto_area_short_side_factor, paste) -> PasteFillPlan`
- `pcbasm.pasting.initial_purge.validate_initial_purge(*, amount_ul, pad_id, hierarchy, routed_pads, layer=Layer.TOP) -> str | None`
- `pcbasm.pasting.calibration.MassFlowEstimate`（attrs frozen、4 値 `float | None`）
- `pcbasm.pasting.calibration.estimate_mass_flow(*, mass_mg, rotations, rate, accel, density_mg_per_ul) -> MassFlowEstimate`（round(x, 6) 済み）
- `pcbasm.pasting.toolhead_offset.ToolheadOffsetResult.measure(*, dispense_position, camera_position, tolerance, calibrated_at)`

いずれも `pcbasm.pasting.__init__` の `__all__` に追加済み。

## 計画からの逸脱・計画外判断

1. **`build_pad_fill_plan_for` のシグネチャ**（計画案は `(polygon, *, config, paste)`）
   - `PasteApplicator` は `PasteDispenser` 設定オブジェクトを保持しない
     （`__init__` はスカラー受け、`from_config` は変換して捨てる）ため、
     `config:` 引数では applicator 側から呼べない。「applicator._fill も
     新関数を使う」という単一ソース化の本目的を優先し、config 由来 3 値
     （nozzle_diameter / auto_line_aspect_ratio / auto_area_short_side_factor）
     を明示 keyword 引数に変更した。per-pad 側の束ねは `paste: ResolvedPaste`
     に集約されており、乖離リスクの本体はこちらで解消済み。
   - 付随して `PasteApplicator.apply` → `_fill` の受け渡しを 7 スカラーから
     `ResolvedPaste` 1 個に変更（private 内部、公開 `apply(**kwargs)` は不変）。
2. **router に `_layer_pads(loaded, layer)` を新設**
   - 設計メモは layer 絞り genexpr の 3 箇所 inline を想定していたが、同一
     genexpr の三重複製を避けて 2 行の私的ヘルパにした。ドメイン判断
     （enabled）は含まず、純粋な layer フィルタのみ。
3. **initial-purge 検証の微小な挙動差（意図された強化）**
   - 旧: amount==0 時の pad 存在検証は「pad_id を今回送信した場合」のみ。
   - 新: `validate_initial_purge` は保存済み pad_id にも存在・Top 面検証を
     適用（amount のみ 0 に PATCH し、保存済み pad が stale な場合 400 に
     なり得る）。既存テストは全て無変更で通過。
4. **loading calibration テストの期待値更新（計画で予告済み）**
   - サーバが round(x, 6) を返すため、`0.5/1.89` 系の期待値が旧 approx
     許容（rel 1e-6 ≒ 2.6e-7）を丸め誤差 2.645e-7 で僅かに超過。
     `tests/webui/routers/test_pasting.py::TestLoadingCalibration` の 2 テスト
     を丸め済み厳密一致に更新。
5. **toolhead_offset の挙動差（コミットメッセージにも明記）**
   - 接近経路: XY+Z 同時降下 → 上空 travel → 降下。
   - 吐出前に prime が入り実塗布 deposit と同一プロトコルに（正味吐出量・
     レート・加速度は同値）。retract 加速度の算式が実塗布と同一
     （`factor * rate² / retraction`）に変わる。
   - jobs のテストは gcode 列を pin しておらず、既存テスト無変更で通過。
