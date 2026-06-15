# WebUI 塗布 pad-config API + 基板永続化（実装ログ）

計画書: `/home/gop/.claude/plans/claude-webui-1-pad-extract-eager-pine.md` Phase 3。
ブランチ: feature/20260615/paste-webui-api。Phase 1/2 は同ブランチに実装済み。

spec-test-author の engagement: なし（既存の webui-phase3 メモは別タスク=ジョブ基盤の Phase 3）。
本 agent が実装 + テスト両方を担当。

## 計画外の判断ログ

1. **pcbasm extraction の UTF8 バグを修正**（計画外だが必須）。
   `src/pcbasm/pcb/kicad.py` の `Component.designator/value/package` と
   `Pad.designator/pad_number/net_name` が `pcbnew.UTF8`（SWIG 型）を保持しており、
   型ヒントは `str` なのに実体が unhashable。`build_pad_hierarchy` が tuple キーに
   使うと `TypeError: unhashable type: 'UTF8'` で落ちる（実 PCB 全滅。Phase 1 の
   unit テストは合成 `Component/Pad` で str を使っていたため露見していなかった）。
   抽出境界で `str(...)` でラップして恒久修正。GET/PATCH が実 PCB を読む Phase 3
   完了条件・Phase 5 ジョブ統合の前提なので production を直すのが正。
   影響範囲は全 pcbasm 消費者だが `==` 比較は元々 str 等価で動いていたため挙動不変。
   全 pcbasm/webui テスト green を確認済み。

2. **reset エンドポイントは実装した**（計画では「任意」）。`load_or_init` は既存
   ファイルがあれば復元してしまうため、reset は明示的に新規 `PasteSettingsModel`
   を生成して save する。

3. **`_resolved_default` は `model.base` が全 7 項目非 None である契約に依存**して
   単純化（None フォールバック分岐を排除）。`base_override_from_config` が常に
   全項目を埋めるため安全。

## API 契約（Phase 4 フロントの前提・契約からの差異）

計画書のシグネチャ案どおり実装。**差異なし**。

- ルーター: `APIRouter(prefix="/api")`、`webui.app` に `pasting` 登録済み
  （pages キャッチオールの前）。
- node_id 規約: `":".join(key)` / `tuple(node.split(":"))`。
  L0 / L1:{package} / L2:{designator} / L3:{designator}:{shape_label} /
  L4:{designator}:{pad_number}。pad id = `{designator}.{pad_number}`。
- エンドポイント:
  - `GET  /api/pasting/pad-config` -> PadConfigResponse
    （pcb_file, machine, outline, width, height, defaults, tree, pads, overrides）
  - `PATCH /api/pasting/pad-config/node`（NodePatch）-> PatchResponse(affected_pads)
  - `PATCH /api/pasting/pad-config/pads`（PadEnablePatch）-> PatchResponse
  - `POST /api/pasting/pad-config/reset` -> PadConfigResponse
- 検証エラー: PCB 未選択 409 / 未知 values・clear キー 400 / 未知 node 400 /
  L0 の enabled=null 400。
- overrides は疎。L0 は常に存在（enabled=base_enabled + base の非 None 項目）。
  L1–L4 は明示設定があるノードのみ。clear 後に override 空 + enabled 継承(None)
  になったノードは overrides から消える（levels から pop）。
- pad.polygon は exterior 座標 `[[x, y], ...]`（mm 系、無変換）。
- DI: `app.state.board_store = BoardSettingsStore(settings.data_dir)`、
  `get_board_store` + `BoardStoreDep`（webui.app）追加。

## 既知の制約・残課題

- 永続化先は `data_dir/board_settings/<machine>/<board_id>.json`、version=1。
  未知 version は ValueError（v1 のみ対応）。
- Phase 4（フロント）/ Phase 5（paste_solder ジョブ統合）は未着手。Phase 5 で
  `BoardSettingsStore` を `JobContext` 経路に配線する（計画書 Phase 5 参照）。
- fill_coverage は pad_number/package が空文字の合成基板。router テストは
  実 designator/pad_number/package を持つ led_blinker（copper_pcb_path）を使用。

## 検証結果

- make format: pass
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（1082 passed, 40 deselected）
  - tests/webui/test_board_settings.py: 11 passed
  - tests/webui/routers/test_pasting.py: 17 passed
  - tests/pcbasm/pcb + pasting（kicad.py 影響確認）: 192 passed
- 実機を要する `make test`（hardware mark）は環境に装置が無いため未実行
  （memory 規約: Claude は実機テストを実行しない）。
