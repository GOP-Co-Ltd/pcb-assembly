# MR6 フィールドメタデータ / PFCB ミラー縮小 / docs — orchestrator 直列実装ノート

サブエージェントの permission 継承バグ（ユーザー指示で以降は委譲しない）のため、orchestrator が
solo-dev-cycle で実装した。計画の正典は `/home/gop/.claude/plans/claude-codex-src-pcbasm-pasting-pasting-sprightly-tome.md` の MR6 行。

## 実装

- `web/api/routers/pasting_view.py`: `PadConfigResponse.fields: list[ParamFieldInfo]`（`PASTE_PARAM_FIELDS` を
  `param_fields()` で写す。name / label / kind / unit / choices）。`PasteRouteRequest.layer` /
  `PasteFillPathRequest.layer` を `Layer` 型にして `check_layer` を削除（不正値は pydantic の 422）。
  `layer_pads(loaded, layer: Layer)`
- JS `pad_editor/{model,table,index}.js`: `FIELDS` / `FIELD_LABELS` / `FIELD_KINDS` / `DISPENSE_MODE_LABELS` /
  `LINE_DIRECTION_LABELS` を削除し `state.config.fields` 駆動に。列見出し（`th.pad-col-field`）も `renderTable`
  が fields から描く。select の testid は `pad-${name.replaceAll("_","-")}-select` で従来値
  （`pad-dispense-mode-select` / `pad-line-direction-select`）と一致させ e2e を無変更で通す。
  テンプレート `partials/pad_editor.html` の静的な列見出し 8 個を削除
- `web/api/attrs_models.py`: `mirror_model(attrs_cls, *, name=None) -> type[BaseModel]`。入れ子 attrs は再帰、
  `tuple[X, ...]` → `list`、`X | None` / `Literal` / スカラーはそのまま。スカラー既定値は保持し `attrs.Factory`
  既定は必須（リクエスト契約を緩めない）。`extra="forbid" / strict=True / from_attributes=True`
- `web/api/routers/paste_flow_calibration_board.py`: 手書き pydantic 23 → 明示 6（`FootprintSearchResponse` /
  `BoardOptionsResponse` / `BoardPreviewResponse` / `BoardImportRequest` / `PatternAdditionRequest` /
  `AddCustomPadRequest`）+ mirror（`BoardConfigModel` / `CustomPadDraftModel` / `ResolvedConfigResponse` /
  `PatternAdditionResponse` と `FootprintInfo` / `CustomPadShape` / `PadPattern` / `BoardSpec` / `Rect` /
  `LayerPolygon` / `PatternLayout` / `PadLayout`）。リクエスト → attrs は `cattrs.Converter().structure`。
  URL・JSON 形状・ダウンロードファイル名は不変（router / ui / e2e テストが無変更で緑）。
  `dependencies.PasteFlowCalibrationBoardGeneratorDep` → `BoardGeneratorDep`
- `tests/helpers.FakeKlipper.g1_moves()` を追加し、4 テストファイルの `_g1_moves` / `_G1_RE` 重複を削除
- docs: `src/pcbasm/pasting/README.md` を構成表・依存方向・命名/検証規約で全面更新、
  `docs/image-based-dispense-calibration-ml-plan.md` の `pcbasm.pasting.paste_dataset` → `pcbasm.pasting.dataset`

## テスト

- `tests/web/api/test_attrs_models.py` 新設
- `tests/web/api/routers/test_pasting.py`: JS `FIELDS` との同順契約テストを `fields` メタデータ契約
  （name 列 = `PASTE_PARAM_NAMES`、kind / unit / choices）に書き換え
- `tests/web/ui/test_pages.py`: SSR に列見出しラベルが含まれない契約に変更（ラベルは API 由来）

## 判断

- `PasteFlowCalibration*Model` の pydantic 名は router 内部名なので短縮名に改名。URL パス
  `/api/pasting/paste-flow-calibration-board` とページ id `paste_flow_calibration_board` は web-facing 契約として維持
- `mirror_model` は `Mapping` 型を対応外にした（PFCB router に出現しないため。必要になったら追加）
- `BoardPreviewResponse` は `BoardPreview` + `BoardLayout` の平坦化なので明示モデルのまま（フィールド型は mirror）
